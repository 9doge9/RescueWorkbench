/* 数据抢救工作台 v0.3 — 完整版(含全部两轮UI优化+虚拟滚动分流) */
'use strict';

const S = { dev:null, probe:null, imageJob:null, imageFile:null, filesJob:null, scanResult:null, files:[], byId:{}, curDir:5, sel:new Set(), extractOut:'' };
let dest_size_cache = 0;
let sortKey = 'name', sortAsc = true;

const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmtSize = n => { if (n == null) return '-'; const u=['B','KB','MB','GB','TB']; let i=0; while(n>=1024&&i<u.length-1){n/=1024;i++;} return (n>=100||i===0?n.toFixed(0):n.toFixed(1))+' '+u[i]; };
const fmtTime = t => (t ? t.replace('T',' ').replace('Z','').slice(0,16) : '-');
function toast(m){ const t=$('toast'); t.textContent=m; t.classList.remove('hidden'); setTimeout(()=>t.classList.add('hidden'),3000); }
async function api(path, opts){ const r=await fetch(path,opts); const j=await r.json().catch(()=>({})); if(!r.ok) throw new Error(j.error||r.status); return j; }
function setStatus(txt){ $('statusText').textContent=txt; }
function conLog(line){ $('conCount').textContent=+$('conCount').textContent+1; const b=$('consoleLog'); b.textContent+=line+'\n'; b.scrollTop=b.scrollHeight; }
function toggleConsole(){ $('consoleBox').classList.toggle('hidden'); }
function modeHint(src){ const isImg=src&&!/^\\\\/.test(src); $('modeHint').innerHTML=isImg?'模式：<b>镜像文件</b> — 操作的是副本，不是盘本身':(src?'模式：<b>物理盘</b> — 只读访问源盘':''); }
function go(page){ document.querySelectorAll('.page').forEach(p=>p.classList.toggle('hidden',p.id!=='page-'+page)); document.querySelectorAll('.nav-item').forEach(n=>n.classList.toggle('active',n.dataset.page===page)); renderNextSteps(); }
document.querySelectorAll('.nav-item').forEach(n=>n.addEventListener('click',()=>go(n.dataset.page)));
document.querySelectorAll('.nav-item[data-page=harvest]').forEach(n=>n.addEventListener('click',()=>loadHarvest()));

// SSE连接守卫: 浏览器对同源HTTP/1.1只有6条并发连接, 每个EventSource永久占一条
// 并行任务多时降级为轮询, 否则整个界面(所有fetch)会饿死
let sseActive=0;
function streamJob(jobId, onEv, onEnd){
  const finish=(ev)=>{ if(onEnd)onEnd(ev); };
  if(sseActive>=4){
    const timer=setInterval(async()=>{
      let j=null;
      try{ j=await api('/api/job/'+jobId); }catch(e){}
      if(!j||!j.id){clearInterval(timer);finish({_end:true});return;}   // 任务不存在: 别无限轮询
      if(j.state==='pending'||j.state==='running')return;
      if(j.last_event)onEv(j.last_event);
      if(j.state==='done'&&j.result){clearInterval(timer);onEv(j.result);finish({_end:true});}
      else if(j.state==='failed'||j.state==='cancelled'){clearInterval(timer);if(j.error)failUI(String(j.error).split('\n')[0]);finish({_end:true});}
    },1200);
    return;
  }
  sseActive++;
  const es=new EventSource('/api/events/'+jobId);
  let closed=false;
  const close=()=>{ if(!closed){closed=true;sseActive--;es.close();} };
  es.onmessage=e=>{ const ev=JSON.parse(e.data);
    if(ev.event==='_end'||ev.event==='_replay_state'){ close(); if(ev.event==='_replay_state'&&ev.state==='failed'&&ev.result==null){ api('/api/job/'+jobId).then(j=>{if(j&&j.error)failUI(j.error.split('\n')[0]);}).catch(()=>{}); } finish(ev); return; }
    if(ev.event==='error'){failUI(ev.message);} if(ev.event==='command'){conLog('$ '+ev.command);} onEv(ev); };
  es.onerror=()=>{close();};
}
function failUI(msg){ toast('❌ '+msg); setStatus('失败：'+msg); conLog('[错误] '+msg); const i=$('imgStat'); if(i&&!$('imgProg').classList.contains('hidden')){i.textContent='❌ '+msg;} const e=$('extStat'); if(e)e.textContent='❌ '+msg; }
async function cancelJob(id){ await api('/api/job/'+id+'/cancel',{method:'POST'}); setStatus('已请求停止…'); }

const FS_NAME={ntfs:'NTFS',fat32:'FAT32',exfat:'exFAT',ext:'ext',fat12:'FAT12',fat16:'FAT16',unknown:'?'};
async function loadDevices(){
  $('devList').innerHTML='<div class="phase">读取中…</div>';
  try{ const j=await api('/api/devices'); let sv={results:[]}; try{sv=await api('/api/survey');}catch(e){}
    const parts={}; (sv.results||[]).forEach(r=>parts[r.source]=r.partitions||[]);
    const box=$('devList'); box.innerHTML=''; const devs=j.devices||[];
    if(!devs.length){ box.innerHTML='<div class="empty-state"><div class="empty-ico">🔌</div><div>没找到可操作的设备</div><div class="empty-sub">插入U盘/移动硬盘后点刷新</div><div class="actions" style="justify-content:center;margin-top:12px"><button class="btn primary mini" onclick="loadDevices()">↻ 刷新设备</button></div></div>'; return; }
    $('driveStats').textContent=devs.length+' 块盘';
    for(const d of devs){ const ps=parts[d.path]||[]; const el=document.createElement('div');
      el.className='drive'+(S.dev&&S.dev.path===d.path?' sel':'');
      const chips=[d.system?'<span class="pill sys">系统盘</span>':'',d.removable?'<span class="pill rem">可移动</span>':'',...ps.map(p=>'<span class="pill fs">'+(FS_NAME[p.filesystem]||'?')+'</span>')].join('');
      // 真实占用
      var fillPct = 0, fillTxt = '';
      if (d.usage && d.usage.pct != null){
        fillPct = d.usage.pct;
        fillTxt = '<span style="font-weight:600;color:var(--acc)">'+fillPct+'%</span> 已用 · '+
          fmtSize(d.usage.used)+' / '+fmtSize(d.usage.total);
      }
      var volChips = (d.usage && d.usage.volumes) ? d.usage.volumes.map(function(v){
        return v.letter+': '+v.pct+'%';
      }).join(' · ') : '';
      el.innerHTML='<div class="d-name">'+(d.bus==='usb'?'🔌':'💽')+' '+esc(d.model||d.path)+'</div><div class="d-path">'+esc(d.path)+'</div><div class="d-badges">'+chips+'</div>'+
        '<div class="fillbar"><div style="width:'+fillPct+'%"></div></div>'+
        '<div class="d-meta" style="margin-top:4px;font-size:12px">'+fillTxt+'</div>'+
        (volChips?'<div class="d-meta" style="font-size:11px;color:var(--muted2);margin-top:2px">'+volChips+'</div>':'')+
        '<div class="d-meta" style="margin-top:2px">'+fmtSize(d.size)+' · '+esc(d.bus)+' · '+(ps.length?ps.length+'个分区':'未知分区')+'</div>';
      el.onclick=()=>{ document.querySelectorAll('.drive').forEach(x=>x.classList.remove('sel')); el.classList.add('sel'); S.dev=d; setSource(d); showDrive(d,ps); };
      box.appendChild(el); }
    if(!j.elevated) toast('当前非管理员：只能看到部分设备');
  }catch(e){ toast('设备读取失败: '+e.message); $('devList').innerHTML=''; }
}

function showDrive(d, ps){
  $('drvTitle').textContent=(d.model||d.path)+(d.system?'（系统盘）':'');
  $('drvInfo').innerHTML='<b>型号</b><div>'+esc(d.model||'未知')+'</div><b>序列号</b><div class="mono">'+esc(d.serial||'未知')+'</div><b>容量</b><div>'+fmtSize(d.size)+'（'+d.size.toLocaleString()+' 字节）</div><b>扇区大小</b><div>'+d.sector_size+' B</div><b>接口</b><div>'+esc(d.bus)+(d.removable?' · 可移动设备':'')+'</div><b>设备路径</b><div class="mono">'+esc(d.path)+'</div><b>系统盘</b><div>'+(d.system?'<span style="color:var(--bad)">是 — 对它做任何操作都请谨慎</span>':'否')+'</div>';
  $('drvParts').innerHTML=ps.length?ps.map(p=>'<span class="pill fs" style="margin:2px 4px 2px 0">'+(FS_NAME[p.filesystem]||'?')+' @LBA '+p.start_lba+(p.size_sectors?' · '+fmtSize(p.size_sectors*512):'')+'</span>').join(''):'（快扫未读到分区）';
  go('drive');
  const m=d.path.match(/PhysicalDrive(\d+)/); const sb=document.getElementById('drvSmart');
  if(m&&sb){ sb.classList.remove('hidden'); sb.querySelector('#smartBody').innerHTML='<span class="phase">SMART读取中…</span>';
    fetch('/api/smart?disk='+m[1]).then(r=>r.json()).then(v=>{ S.smart=v; const el=document.getElementById('smartBody');
      if(!v.ok){el.innerHTML='<div class="muted">'+esc(v.reason||'不可读')+'</div>';return;}
      let rows=v.attrs.slice(0,12).map(a=>'<tr><td class="muted">'+a.id+'</td><td>'+esc(a.name)+'</td><td>'+a.value+'</td><td class="muted">'+a.thresh+'</td><td class="mono">'+esc(a.raw)+'</td></tr>').join('');
      el.innerHTML='<div style="margin-bottom:8px;font-size:14px">'+(v.warnings.length?'<span class="st-bad">⚠️ '+esc(v.warnings.join('；'))+'</span>':'<span class="st-ok">● '+esc(v.health)+'</span>')+(v.temp!=null?' · '+v.temp+'°C':'')+'</div>'+(rows?'<table class="tbl"><tr><th>ID</th><th>属性</th><th>值</th><th>阈值</th><th>原始值</th></tr>'+rows+'</table>':'<div class="muted">无属性数据</div>');
    }).catch(e=>{document.getElementById('smartBody').innerHTML='<div class="muted">读取失败</div>';});
  }
}

function setSource(d){
  const label=(d.model||d.path)+' · '+d.path;
  $('srcInfo').textContent=label; $('probeSrc').textContent=label; $('imgSrc').textContent=label;
  const dn=(d.model||'disk').replace(/[^\w\u4e00-\u9fa5-]+/g,'_');
  $('imgDest').value='D:\\抢救镜像\\'+dn+'_'+new Date().toISOString().slice(0,10)+'.img';
  $('extractOut').value='D:\\恢复输出\\'+dn; $('hashTarget').value='';
  modeHint(d.path);
}

async function chooseFolder(inputId){
  if(window.pywebview&&window.pywebview.api&&window.pywebview.api.pick_folder){ try{const p=await window.pywebview.api.pick_folder(); if(p)$(inputId).value=p; return;}catch(e){} }
  toast('文件夹选择器需在桌面版中使用；当前请手动填写路径');
}

async function startProbe(){
  if(!S.dev) return toast('先在首页选择设备');
  $('probeVerdict').classList.add('hidden'); $('toImgBtn').classList.add('hidden');
  $('probePhase').textContent='探测中…';
  const job=await api('/api/job',{method:'POST',body:JSON.stringify({kind:'probe',params:{source:S.dev.path}})});
  streamJob(job.id,ev=>{
    if(ev.event==='phase')$('probePhase').textContent=ev.message;
    if(ev.event==='done'){ S.probe=ev;
      const box=$('probeVerdict'); box.className='verdict '+(ev.stable?'ok':'bad');
      let adv=''; if(ev.stream_drift)adv='<div><b>建议</b>：已自动选中「校验式镜像」+「SCSI直读」。</div>'; else if(!ev.stable)adv='<div><b>建议</b>：已自动选中「校验式镜像」。</div>';
      box.innerHTML='<div class="kv"><b>读取稳定性</b><div>'+(ev.stable?'<b style="color:var(--ok)">健康</b> — 读取稳定，可用快速镜像':'<b style="color:var(--bad)">读数不稳</b> — '+(ev.stream_drift?'检测到顺序流漂移（主控说谎）；':'')+(ev.repeat_unstable>0?ev.repeat_unstable+'/'+ev.repeat_points+'个采样点异常；':'')+'必须用校验式镜像')+'</div><b>定点重复读</b><div>'+ev.repeat_unstable+'/'+ev.repeat_points+' 点异常</div><b>流式 vs 定点</b><div>'+(ev.stream_drift==null?'未测':(ev.stream_drift?'不一致（漂移）':'一致'))+'</div>'+adv+'</div>';
      box.classList.remove('hidden'); $('toImgBtn').classList.remove('hidden');
      setStatus('体检完成：'+(ev.stable?'读取稳定':'读数不稳')); $('imgMode').value=ev.stable?'fast':'verified'; if(ev.stream_drift)$('imgAccess').value='scsi';
      // SMART并入体检结论: 读稳≠盘健康, 物理病灶(重映射/待映射)直接升级建议
      (async()=>{
        const m=(S.dev&&S.dev.path||'').match(/PhysicalDrive(\d+)/);
        if(!m)return;
        try{
          const sm=await api('/api/smart?disk='+m[1]);
          if(!sm.ok)return;
          S.smart=sm;                          // 报告页会自动带上
          const warn=sm.warnings&&sm.warnings.length;
          const temp=sm.temp?(' · '+sm.temp+'°C'):'';
          const line=warn?('⚠️ '+sm.warnings.join('；')+temp):('✓ '+sm.health+temp);
          const kv=box.querySelector('.kv');
          if(kv)kv.insertAdjacentHTML('beforeend','<b>SMART体检</b><div>'+(warn?'<b style="color:var(--bad)">'+line+'</b>':line)+'</div>'+(warn?'<div class="muted">盘有物理病灶——即使读取暂时稳定也建议<b>校验式镜像</b>并尽快抢救</div>':''));
          if(warn&&ev.stable){ box.className='verdict warn'; $('imgMode').value='verified'; }
        }catch(e){}
      })();
    }
  });
}

const MAP_COLORS={'+':'#2dd4bf','-':'#f85149','*':'#f59e0b','/':'#d29922','?':'#30363d'};
let mapTimer=null;
function drawMap(blocks,size,totals){ const cv=$('mapCanvas'); const dpr=window.devicePixelRatio||1; const w=cv.clientWidth||600; cv.width=w*dpr; cv.height=26*dpr; const ctx=cv.getContext('2d'); ctx.setTransform(dpr,0,0,dpr,0,0); ctx.clearRect(0,0,w,26); for(const b of blocks){ const x=b.pos/size*w,bw=Math.max(b.len/size*w,0.7); ctx.fillStyle=MAP_COLORS[b.status]||'#30363d'; ctx.fillRect(x,0,bw,26); } $('mapTotals').textContent='已救 '+fmtSize(totals['+']||0)+' · 坏区 '+fmtSize(totals['-']||0)+' · 处理中 '+fmtSize((totals['*']||0)+(totals['/']||0))+' · 未试 '+fmtSize(totals['?']||0); }
async function refreshMap(path){ try{ const j=await api('/api/mapfile?path='+encodeURIComponent(path)); if(j.ok){drawMap(j.blocks,j.size,j.totals); return true;} }catch(e){} return false; }
function startMapPoll(path){ stopMapPoll(); $('mapBox').classList.remove('hidden'); refreshMap(path); mapTimer=setInterval(()=>refreshMap(path),2000); }
function stopMapPoll(){ if(mapTimer){clearInterval(mapTimer);mapTimer=null;} }

function speedColor(mbps,min,max){ if(mbps<=min)return'#f85149'; const t=Math.min((mbps-min)/Math.max(max-min,0.1),1); const r=t<0.5?0xf8:Math.round(0xf8+(0x2d-0xf8)*(t-0.5)*2); const g=t<0.5?Math.round(0x51+(0xdc-0x51)*t*2):0xdc; return 'rgb('+r+','+g+',80)'; }
function drawSpeedMap(bins,size){ const cv=$('speedCanvas'); if(!cv)return; const dpr=window.devicePixelRatio||1; const w=cv.clientWidth||600; cv.width=w*dpr; cv.height=22*dpr; const ctx=cv.getContext('2d'); ctx.setTransform(dpr,0,0,dpr,0,0); ctx.clearRect(0,0,w,22); if(!bins||bins.length<2){$('speedBox').classList.add('hidden');return;} const mbps=bins.map(b=>b[1]); const max=Math.max(...mbps),min=0; for(const[pos,sp]of bins){ const x=pos/size*w; ctx.fillStyle=speedColor(sp,min,max); ctx.fillRect(x,0,Math.max(w/bins.length,1),22); } const sl=[...bins].sort((a,b)=>a[1]-b[1]).slice(0,3); $('speedStat').textContent='最快 '+max.toFixed(0)+' MB/s · 最慢 '+sl.map(([p,s])=>'@'+fmtSize(p)+' '+s.toFixed(1)+'MB/s').join(' · '); }

async function startImage(retryOnly){
  if(!S.dev) return toast('先在首页选择设备');
  const dest=$('imgDest').value.trim();
  let mode=retryOnly?'fast':$('imgMode').value;
  if(mode==='auto') mode=(S.probe&&!S.probe.stable)?'verified':'fast';
  S.imageFile=dest;
  $('imgProg').classList.remove('hidden'); $('imgCancel').classList.remove('hidden'); $('imgStart').disabled=true;
  $('imgStat').textContent=retryOnly?'启动重试坏区…':'启动中…'; modeHint(dest);
  const params={source:S.dev.path,dest,mapfile:dest+'.map',mode,access:$('imgAccess').value||null,size:S.dev.size};
  if(retryOnly)params.retry_only=true;
  if(!retryOnly&&mode==='fast'&&$('imgReverse')&&$('imgReverse').checked)params.reverse=true;
  const job=await api('/api/job',{method:'POST',body:JSON.stringify({kind:'image',params})});
  S.imageJob=job.id; dest_size_cache=S.dev?S.dev.size:0;
  $('speedBox').classList.add('hidden'); startMapPoll(dest+'.map');
  streamJob(job.id,ev=>{
    if(ev.event==='progress'){ if(ev.pct!=null){ $('imgBar').style.width=ev.pct+'%'; const l=[]; if(ev.speed_mbps)l.push(ev.speed_mbps+' MB/s'); if(ev.eta_min!=null)l.push('剩~'+ev.eta_min+'分'); $('imgBarLabel').textContent=ev.pct+'% · '+l.join(' · '); }
      const p=[]; if(ev.rereads)p.push('重读'+ev.rereads); if(ev.bad_bytes)p.push('坏区 '+fmtSize(ev.bad_bytes)); if(ev.pass)p.push('第'+ev.pass+'遍');
      $('imgStat').textContent=p.join(' · ')||' '; setStatus('镜像中 '+ev.pct+'%'); conLog('[镜像] '+ev.pct+'% '+ev.speed_mbps+'MB/s'); }
    if(ev.event==='phase'){$('imgStat').textContent=ev.message;}
    if(ev.event==='bad_region'){toast('⚠️ 坏区 @'+fmtSize(ev.offset)+'（已记录）');}
    if(ev.event==='done'){ $('imgCancel').classList.add('hidden'); $('imgStart').disabled=false; $('imgBar').style.width='100%';
      S.imageResult=ev; S.speedBins=ev.speed_bins||null;
      const bad=ev.bad_bytes;
      $('imgStat').textContent='✅ 镜像完成'+(bad?'（坏区 '+fmtSize(bad)+'，详见map）':' — 全部字节读取成功');
      setStatus('镜像完成'+(bad?'（有坏区）':'')); conLog('[镜像] 完成 finished='+fmtSize(ev.finished_bytes)+' bad='+fmtSize(ev.bad_bytes||0)); toast('镜像完成');
      $('hashTarget').value=dest; refreshMap(dest+'.map'); stopMapPoll();
      if(ev.speed_bins&&ev.speed_bins.length>1){$('speedBox').classList.remove('hidden'); drawSpeedMap(ev.speed_bins,ev.total||dest_size_cache);}
      go('partitions'); }
    if(ev.event==='cancelled'){ $('imgCancel').classList.add('hidden'); $('imgStart').disabled=false; $('imgStat').textContent='已停止（断点已保存）'; setStatus('镜像已停止'); refreshMap(dest+'.map'); stopMapPoll(); }
  });
}

function srcPath(){ return S.imageFile||(S.dev&&S.dev.path); }
async function startScan(){ const src=srcPath(); if(!src)return toast('先选设备或完成镜像');
  $('scanOut').innerHTML='<div class="phase">扫描中…</div>';
  const job=await api('/api/job',{method:'POST',body:JSON.stringify({kind:'scan',params:{source:src,deep:$('deepScan').checked}})});
  streamJob(job.id,ev=>{
    if(ev.event==='scan_progress'&&ev.bytes_total){$('scanOut').innerHTML='<div class="phase">深度签名扫描 '+Math.round(ev.bytes_scanned*100/ev.bytes_total)+'%</div>';}
    if(ev.event==='done'){ S.scanResult=ev; renderScan(ev); fillPartSelect(); setStatus('扫描完成：'+(ev.partitions||[]).length+'个分区'); }
  });
}
function renderScan(ev){ const ps=ev.partitions||[];
  if(!ps.length){$('scanOut').innerHTML='<div class="empty-state"><div class="empty-ico">🗂️</div><div>未找到分区</div><div class="empty-sub">分区表可能损坏，试试勾选「深度签名扫描」</div></div>';return;}
  let html='<div class="head-meta" style="margin-bottom:8px">'+ps.length+' 个分区 · 表类型 '+esc(ev.table_kind||'?')+'</div><table class="tbl"><tr><th>文件系统</th><th>置信度</th><th>起始LBA</th><th>大小</th><th>来源</th><th>卷标</th></tr>';
  for(const p of ps){ const conf=p.confidence==='confident'?'<span class="pill rec-full">可信</span>':(p.confidence==='rebuilt'?'<span class="pill rec-partial">签名重建</span>':'<span class="pill rec-none">低置信</span>');
    html+='<tr><td><b>'+(FS_NAME[p.filesystem]||esc(p.filesystem)||'?')+'</b></td><td>'+conf+'</td><td class="ttime">'+(p.start_lba??'-')+'</td><td class="tsize">'+(p.size_sectors?fmtSize(p.size_sectors*512):'?')+'</td><td class="muted">'+esc(p.source||'')+'</td><td class="muted">'+esc(p.label||'')+'</td></tr>'; }
  html+='</table>'; $('scanOut').innerHTML=html;
}
function fillPartSelect(){ const sel=$('partSel'); sel.innerHTML=''; for(const p of(S.scanResult&&S.scanResult.partitions)||[]){ const o=document.createElement('option'); o.value=p.start_lba; o.textContent=(FS_NAME[p.filesystem]||'?')+', '+(p.size_sectors?fmtSize(p.size_sectors*512):'?')+', 起始扇区 '+p.start_lba; sel.appendChild(o); } }

const IMG_EXT=new Set(['jpg','jpeg','png','gif','webp','bmp']);
function isImg(name){ const i=(name||'').lastIndexOf('.'); return i>0&&IMG_EXT.has(name.slice(i+1).toLowerCase()); }
function previewUrl(f){ return '/api/preview?src='+encodeURIComponent(srcPath())+'&lba='+encodeURIComponent(S.curLba||'')+'&id='+f.id; }
function sortBy(key){ if(sortKey===key){sortAsc=!sortAsc;}else{sortKey=key;sortAsc=true;} renderTable(); }
function showLightbox(url,name,size){
  let lb=$('lightbox');
  if(!lb){ lb=document.createElement('div'); lb.id='lightbox'; lb.className='lightbox hidden';
    lb.innerHTML='<div class="lb-meta"></div><div class="lb-nav"><button class="btn mini" id="lbPrev">‹</button><span class="lb-count mono"></span><button class="btn mini" id="lbNext">›</button></div><img>';
    lb.addEventListener('click',e=>{if(e.target===lb)lb.classList.add('hidden');});
    lb.querySelector('#lbPrev').addEventListener('click',()=>lbStep(-1)); lb.querySelector('#lbNext').addEventListener('click',()=>lbStep(1));
    document.addEventListener('keydown',e=>{ if(!lb.classList.contains('hidden')){ if(e.key==='ArrowLeft')lbStep(-1); else if(e.key==='ArrowRight')lbStep(1); else if(e.key==='Escape')lb.classList.add('hidden'); } });
    document.body.appendChild(lb); }
  if(!lb._imgs||lb._dirId!==S.curDir){ const node=S.byId[S.curDir]; lb._imgs=node?node.children.filter(f=>!f.is_dir&&isImg(f.name)):[]; lb._dirId=S.curDir; }
  const idx=lb._imgs.findIndex(f=>f.name===name); lb._cur=idx>=0?idx:-1;
  if(lb._imgs.length&&idx>=0)lbStep(0); else{ lb.querySelector('img').src=url; lb.querySelector('.lb-meta').textContent=name+' · '+fmtSize(size); lb.querySelector('.lb-count').textContent=''; lb.classList.remove('hidden'); }
}
function lbStep(dir){ const lb=$('lightbox'); if(!lb||!lb._imgs||!lb._imgs.length)return; lb._cur=Math.max(0,Math.min(lb._imgs.length-1,lb._cur+dir)); const f=lb._imgs[lb._cur];
  lb.querySelector('img').src=previewUrl(f); lb.querySelector('.lb-meta').textContent=f.name+' · '+fmtSize(f.size); lb.querySelector('.lb-count').textContent=(lb._cur+1)+'/'+lb._imgs.length; lb.classList.remove('hidden'); }

async function startFiles(){ const src=srcPath(); if(!src)return toast('先选设备或完成镜像');
  const ps=(S.scanResult&&S.scanResult.partitions)||[]; if(!ps.length)return toast('请先在「分区」页扫描');
  const lba=$('partSel').value||ps[0].start_lba; S.curLba=lba;
  $('filesPhase').classList.remove('hidden'); $('filesPhase').textContent='读取文件列表…'; $('browserPanel').classList.add('hidden');
  const job=await api('/api/job',{method:'POST',body:JSON.stringify({kind:'files',params:{source:src,partition_lba:lba,mapfile:(S.imageFile||'')+'.map',deleted_only:$('onlyDeleted').checked}})});
  S.filesJob=job.id; $('filesStop').classList.remove('hidden');
  streamJob(job.id,ev=>{
    if(ev.event==='files_so_far')$('filesPhase').textContent='已读取 '+ev.count+' 条…';
    if(ev.event==='done'){ $('filesStop').classList.add('hidden'); $('filesPhase').classList.add('hidden');
      S.files=ev.files||[]; S.filesSummary={files_total:ev.files_total,deleted:ev.deleted};
      buildIndex(); S.curDir=5; S.sel.clear(); $('browserPanel').classList.remove('hidden');
      setStatus('文件列表就绪：'+(ev.files_total??S.files.length)+' 项'); renderTable(); }
    if(ev.event==='cancelled'){$('filesStop').classList.add('hidden'); setStatus('已停止');}
  });
}
function buildIndex(){ S.byId={}; S.files.forEach(f=>S.byId[f.id]=Object.assign({children:[]},f));
  if(!S.byId[5])S.byId[5]={id:5,parent_id:0,name:'卷根',is_dir:true,children:[]};
  Object.values(S.byId).forEach(f=>{const p=S.byId[f.parent_id]; if(p&&p.id!==f.id)p.children.push(f);});
  Object.values(S.byId).forEach(f=>f.children.sort((a,b)=>(b.is_dir-a.is_dir)||a.name.localeCompare(b.name,'zh')));
}
function pathOf(id){ const chain=[]; let n=S.byId[id]; while(n){chain.unshift(n); n=S.byId[n.parent_id]&&S.byId[n.parent_id].id!==n.id?S.byId[n.parent_id]:null;} return chain; }
function renderTable(){
  const node=S.byId[S.curDir]; if(!node)return;
  const chain=pathOf(S.curDir);
  $('crumb').innerHTML=chain.map((n,i)=>(i?'<span class="sep">›</span>':'')+'<span onclick="navTo('+n.id+')">'+(i?esc(n.name):'卷根')+'</span>').join('');
  let rows=node.children;
  const q=($('fileSearch').value||'').toLowerCase();
  if(q)rows=rows.filter(r=>(r.name||'').toLowerCase().includes(q));
  if($('fDeleted').checked)rows=rows.filter(r=>r.deleted);
  if($('fRecoverable').checked)rows=rows.filter(r=>r.recoverable==='full');
  rows=[...rows].sort((a,b)=>{ if(a.is_dir!==b.is_dir)return b.is_dir-a.is_dir; let v=0;
    switch(sortKey){case'name':v=(a.name||'').localeCompare(b.name||'','zh');break;case'size':v=(a.size||0)-(b.size||0);break;case'time':v=(a.modified||'').localeCompare(b.modified||'');break;case'rec':v=(a.recoverable||'').localeCompare(b.recoverable||'');break;} return sortAsc?v:-v; });
  $('itemsMeta').textContent=rows.length+' / '+node.children.length+' 项';
  if(rows.length>500&&typeof _virtAll!=='undefined'){ _virtAll=rows; _virtStart=-1; _virtEnd=-1; renderTableVirtual(); return; }
  _virtAll=[];
  const recName={full:'完整',partial:'部分',none:'无数据'}; const recCls={full:'rec-full',partial:'rec-partial',none:'rec-none'};
  const arrow=k=>k===sortKey?(sortAsc?' ▲':' ▼'):''; const sortable=(key,label)=>'<th style="cursor:pointer;user-select:none" onclick="sortBy(\''+key+'\')">'+label+arrow(key)+'</th>';
  if(!rows.length){ $('fileTable').innerHTML='<tr><td colspan="6" style="text-align:center;padding:30px 0;color:var(--muted2)">此目录为空'+(q?'（搜索无结果）':'')+'</td></tr>'; updateSelInfo(); return; }
  let html='<tr><th style="width:28px"><input type="checkbox" onchange="toggleAll(this.checked)"></th>'+sortable('name','名称')+sortable('time','修改时间 (UTC)')+sortable('size','大小')+'<th>状态</th>'+sortable('rec','可恢复性')+'</tr>';
  for(const f of rows){ const orphan=!f.is_dir&&f.parent_id!=null&&!S.byId[f.parent_id]; const img=!f.is_dir&&isImg(f.name);
    const nm=f.is_dir?'<span class="fname-dir" onclick="navTo('+f.id+')">📁 '+esc(f.name)+'</span>'
      :img?'<span class="fimg" onclick="showLightbox(\''+previewUrl(f)+'\',\''+esc(f.name)+'\','+(f.size||0)+')"><img class="thumb" loading="lazy" src="'+previewUrl(f)+'" onerror="this.parentElement.classList.add(\'imgfail\');this.remove()"><span class="fn-txt">'+esc(f.name)+'</span></span>'
      :'📄 '+esc(f.name);
    html+='<tr><td><input type="checkbox" class="fck" data-id="'+f.id+'" '+(S.sel.has(f.id)?'checked':'')+' onchange="selOne('+f.id+',this.checked)"></td><td>'+nm+'</td><td class="ttime">'+fmtTime(f.modified)+'</td><td class="tsize">'+(f.is_dir?'文件夹':fmtSize(f.size))+'</td><td>'+(f.deleted?'<span class="pill st-del">已删</span>':'')+(orphan?'<span class="pill orphan">孤立</span>':'')+'</td><td>'+(f.is_dir?'':'<span class="pill '+(recCls[f.recoverable]||'rec-none')+'">'+(recName[f.recoverable]||esc(f.recoverable)||'?')+'</span>')+'</td></tr>'; }
  $('fileTable').innerHTML=html; updateSelInfo();
}
function navTo(id){ S.curDir=id; _virtAll=[]; renderTable(); }
function toggleAll(v){ const node=S.byId[S.curDir]; if(!node)return; node.children.forEach(c=>{if(v)S.sel.add(c.id);else S.sel.delete(c.id);}); renderTable(); }
function selOne(id,v){ v?S.sel.add(id):S.sel.delete(id); updateSelInfo(); }
function clearSel(){ S.sel.clear(); renderTable(); }
function updateSelInfo(){ const n=S.sel.size; $('selInfo').textContent=n?('已勾选 '+n+' 项（选文件夹=提取其全部内容）'):'未勾选。勾选要恢复的文件或文件夹。'; }

async function startExtract(){ if(!S.sel.size)return toast('先勾选文件/文件夹');
  const src=srcPath(); const lba=$('partSel').value||S.scanResult.partitions[0].start_lba;
  const out=$('extractOut').value.trim(); S.extractOut=out;
  const job=await api('/api/job',{method:'POST',body:JSON.stringify({kind:'extract',params:{source:src,partition_lba:lba,ids:[...S.sel].join(','),out,mapfile:(S.imageFile||'')+'.map'}})});
  go('result'); $('resultBox').innerHTML='<div class="phase">提取中…</div><div class="stat mono" id="extStat"></div>';
  streamJob(job.id,ev=>{
    if(ev.event==='extract_progress'){$('extStat').textContent='文件 '+ev.files_done+'/'+(ev.files_total||'?')+' · '+fmtSize(ev.bytes_done); setStatus('提取中 '+ev.files_done+'/'+(ev.files_total||'?'));}
    if(ev.event==='phase')$('extStat').textContent=ev.message;
    if(ev.event==='done'){ const r=ev.result||{}; S.extractResult={extracted:r.extracted,failed:r.failed,out:S.extractOut}; S.verifyTarget=S.extractOut;
      $('resultBox').innerHTML='<div class="verdict ok"><div class="kv"><b>提取完成</b><div>成功 '+(r.extracted??'?')+' 项 · 失败 '+(r.failed??0)+' 项</div><b>输出目录</b><div><code>'+esc(S.extractOut)+'</code></div></div></div><div class="actions"><button class="btn" onclick="startVerifyTarget()">🔬 验证分类恢复的文件</button></div><div id="verifyOut" class="stat"></div>';
      setStatus('提取完成'); }
  });
}
async function startVerifyDir(dir){ $('verifyOut').textContent='验证中…';
  const job=await api('/api/job',{method:'POST',body:JSON.stringify({kind:'verify',params:{dir}})});
  streamJob(job.id,ev=>{ if(ev.event==='done')$('verifyOut').innerHTML='<pre class="vres">'+esc(ev.summary||'')+'</pre>'; });
}
function startVerifyTarget(){ if(S.verifyTarget)startVerifyDir(S.verifyTarget); }

function carvePartStart(){ const ps=(S.scanResult&&S.scanResult.partitions)||[]; const lba=+($('partSel').value||(ps[0]&&ps[0].start_lba)||0); const p=ps.find(x=>x.start_lba===lba); return p?p.start_lba*512:0; }
function carvePartRange(){ const ps=(S.scanResult&&S.scanResult.partitions)||[]; const lba=+($('partSel').value||(ps[0]&&ps[0].start_lba)||0); const p=ps.find(x=>x.start_lba===lba); return p&&p.size_sectors?{start:p.start_lba*512,end:(p.start_lba+p.size_sectors)*512}:null; }
async function startH264Carve(){ const src=srcPath(); if(!src)return toast('先选设备或完成镜像');
  const out=($('extractOut').value.trim()||'D:\\恢复输出\\carve')+'_监控流';
  const params={source:src,out,mapfile:(S.imageFile||'')+'.map',min_kb:+$('h264MinKb').value||64};
  if($('carveRange')&&$('carveRange').checked){ const r=carvePartRange(); if(r){params.start=r.start; params.end=r.end;} }
  const job=await api('/api/job',{method:'POST',body:JSON.stringify({kind:'h264carve',params})});
  setStatus('监控裸流扫描中…');
  let items=[];
  streamJob(job.id,ev=>{
    if(ev.event==='scan_progress')$('h264Out').textContent=Math.round(ev.bytes_scanned*100/ev.bytes_total)+'% · 已找到 '+(ev.found||0)+' 段';
    if(ev.event==='found'){ const dim=ev.width?ev.width+'x'+ev.height:'?'; items.push('<div class="kv"><b>'+esc(ev.name)+'</b><div>'+fmtSize(ev.size)+' · '+dim+' · '+ev.frames+'帧 · '+ev.fps+'fps · '+(ev.mp4?'已重封装MP4':'仅裸流(缺参数集)')+'</div></div>'); $('h264Out').innerHTML=items.join(''); }
    if(ev.event==='done'){ $('h264Out').innerHTML=(items.length?items.join(''):'<div class="muted">未找到合法裸流</div>')+'<div class="verdict ok"><b>完成</b><div>共 '+ev.found+' 段 · '+fmtSize(ev.bytes||0)+' · 输出 <code>'+esc(out)+'</code></div></div>'; setStatus('裸流雕刻完成'); }
  });
}
async function allocPreview(){ const src=srcPath(); if(!src)return toast('先选设备或完成镜像');
  $('carveOut').textContent='分析分配图中…';
  try{ const r=await api('/api/allocmap?src='+encodeURIComponent(src)+'&lba='+carvePartStart());
    if(!r.ok){$('carveOut').innerHTML='<span style="color:var(--bad)">解析失败：'+esc(r.error||'?')+' — 该盘需用全范围雕刻</span>';return;}
    const used=r.cluster_size*r.total_clusters;
    $('carveOut').innerHTML='<b>'+esc(r.fs.toUpperCase())+'</b> · 簇 '+fmtSize(r.cluster_size)+' · 未分配 <b>'+fmtSize(r.bytes_free)+'</b> / '+fmtSize(used)+'（空闲 '+(r.free_clusters*100/(r.total_clusters||1)).toFixed(1)+'%） · '+r.extents.length+' 段可扫（合并前 '+r.extents_raw+' 段）';
  }catch(e){$('carveOut').textContent='分析失败: '+e.message;}
}
async function startCarve(){ const src=srcPath(); if(!src)return toast('先选设备或完成镜像');
  const out=($('extractOut').value.trim()||'D:\\恢复输出\\carve')+'_雕刻';
  const params={source:src,out,mapfile:(S.imageFile||'')+'.map'};
  let kind='carve';
  if($('carveFree')&&$('carveFree').checked){ kind='carve_free'; params.partition_lba=carvePartStart(); if(!params.partition_lba)return toast('仅未分配模式需要先扫描分区并选中一个分区'); }
  else if($('carveRange')&&$('carveRange').checked){ const ps=(S.scanResult&&S.scanResult.partitions)||[]; const lba=+($('partSel').value||(ps[0]&&ps[0].start_lba)||0); const p=ps.find(x=>x.start_lba===lba); if(p&&p.size_sectors){params.start=p.start_lba*512; params.end=params.start+p.size_sectors*512;} }
  const job=await api('/api/job',{method:'POST',body:JSON.stringify({kind,params})});
  setStatus(kind==='carve_free'?'未分配空间雕刻中…':'签名雕刻中…');
  streamJob(job.id,ev=>{
    if(ev.event==='carve_progress')$('carveOut').textContent=Math.round(ev.bytes_scanned*100/ev.bytes_total)+'% · 已找到 '+ev.files_found+(ev.ranges?' · 段 '+ev.range+'/'+ev.ranges:'');
    if(ev.event==='allocmap')$('carveOut').textContent=ev.fs.toUpperCase()+' · 未分配 '+fmtSize(ev.bytes_free)+' · 簇 '+fmtSize(ev.cluster_size);
    if(ev.event==='done'){const f=ev.files_found!=null?' · 共找到 '+ev.files_found+' 个文件':'';$('carveOut').innerHTML='雕刻完成'+f+'，输出：<code>'+esc(out)+'</code>'; setStatus('雕刻完成');}
  });
}

// ---------- 虚拟磁盘 ----------
async function chooseFile(inputId){
  if(window.pywebview&&window.pywebview.api&&window.pywebview.api.pick_file){ try{const p=await window.pywebview.api.pick_file(); if(p)$(inputId).value=p; return;}catch(e){} }
  toast('浏览器模式下请手动输入路径');
}
async function vmDetect(){
  const src=$('vmFile').value.trim(); if(!src)return toast('先填虚拟盘文件路径');
  $('vmOut').textContent='检测中…';
  try{ const r=await api('/api/vmdetect?src='+encodeURIComponent(src));
    if(!r.kind)return $('vmOut').innerHTML='<span style="color:var(--bad)">'+esc(r.detail||'未识别')+'</span>';
    $('vmOut').innerHTML='<b>'+esc(r.kind)+'</b>'+(r.virtual_size?' · 虚拟容量 '+fmtSize(r.virtual_size):'')+' · '+
      (r.needs_expand?'<span style="color:var(--warn)">需展开</span>':'<span style="color:var(--ok,#4caf50)">可直接当镜像用</span>')+
      '<div class="muted">'+esc(r.detail||'')+'</div>';
  }catch(e){$('vmOut').textContent='检测失败: '+e.message;}
}
async function startVmExpand(){
  const src=$('vmFile').value.trim(); if(!src)return toast('先填虚拟盘文件路径');
  const out=($('extractOut').value.trim()||'D:\\恢复输出')+'_虚拟盘展开';
  const params={source:src,out};
  $('vmOut').textContent='识别+展开中…';
  const job=await api('/api/job',{method:'POST',body:JSON.stringify({kind:'vmexpand',params})});
  setStatus('虚拟盘展开中…');
  streamJob(job.id,ev=>{
    if(ev.event==='phase')$('vmOut').textContent=ev.message;
    if(ev.event==='detected')$('vmOut').innerHTML='<b>'+esc(ev.kind||'?')+'</b> · '+(ev.detail||'');
    if(ev.event==='progress')$('vmOut').textContent='展开中… '+JSON.stringify(ev).slice(0,80);
    if(ev.event==='done'){
      const use=ev.raw||ev.usable_direct;
      S.imageFile=use; S.imageFile&&(S.scanResult=null);
      const stat=ev.stat?('('+(ev.stat.blocks!=null?ev.stat.blocks+'块':ev.stat.grains+'grain')+')'):'';
      $('vmOut').innerHTML='<div class="verdict ok"><b>'+esc(ev.kind)+' 展开/识别完成'+stat+'</b><div>已设为当前镜像：<code>'+esc(use)+'</code>'+(ev.virtual_size?' · 虚拟容量 '+fmtSize(ev.virtual_size):'')+'</div><div class="muted">下一步：点上方"扫描源"跑分区扫描</div></div>';
      setStatus('虚拟盘就绪');
    }
  });
}

// ---------- RAID重组 ----------
function raidTryCfg(cstr){ try{ startRaidRebuild(JSON.parse(cstr)); }catch(e){ toast("候选参数无效"); } }
async function startRaidRebuild(cfgOverride){
  const lines=$('raidMembers').value.split('\n').map(s=>s.trim()).filter(s=>s.length>0);
  if(lines.length<2 && !cfgOverride)return toast('至少填两块成员盘镜像路径(每行一个)');
  const out=($('extractOut').value.trim()||'D:\\恢复输出')+'_RAID重组';
  const params={members:lines,out};
  if(cfgOverride)params.cfg=cfgOverride;
  $('raidOut').textContent='检测RAID配置中…';
  const job=await api('/api/job',{method:'POST',body:JSON.stringify({kind:'raidrebuild',params})});
  setStatus('RAID重组中…');
  streamJob(job.id,ev=>{
    if(ev.event==='phase')$('raidOut').textContent=ev.message;
    if(ev.event==='progress')$('raidOut').textContent='候选尝试 '+ev.tried+'/'+ev.total;
    if(ev.event==='detected'){
      const lvl=ev.level===1?'RAID1镜像':(ev.level===5?'RAID5':'RAID0');
      $('raidOut').innerHTML='<b>'+lvl+'</b>'+(ev.rotation?' · '+esc(ev.rotation):'')+
        (ev.stripe_kb?' · 条带 '+ev.stripe_kb+'KB':'')+' · 盘序 ['+ev.order.join(',')+']'+
        (ev.virtual_size?' · 虚拟容量 '+fmtSize(ev.virtual_size):'')+
        (ev.ambiguous?'<div class="muted" style="color:var(--warn)">'+esc(ev.detail||'')+'</div>':'');
    }
    if(ev.event==='progress'&&ev.bytes_done)$('raidOut').textContent='重建 '+Math.round(ev.bytes_done*100/ev.bytes_total)+'%';
    if(ev.event==='done'){
      S.imageFile=ev.raw; S.scanResult=null;
      const lvl=ev.level===1?'RAID1':(ev.level===5?'RAID5':'RAID0');
      const cands=(ev.candidates&&ev.candidates.length>1)?('<div class="muted">拼得不对? 试其他候选: '+ev.candidates.slice(1).map(c=>'<a href="javascript:void(0)" onclick="raidTryCfg(this.dataset.c)" data-c="'+esc(JSON.stringify({level:c.level,stripe_kb:c.stripe_kb,order:c.order,rotation:c.rotation}))+'">L'+c.level+'/'+c.stripe_kb+'KB/分'+c.score+'</a>').join(' · ')+'</div>'):'';
      $('raidOut').innerHTML='<div class="verdict ok"><b>'+lvl+' 重组完成</b><div>已设为当前镜像：<code>'+esc(ev.raw)+'</code></div>'+cands+'<div class="muted">下一步：点上方"扫描源"跑分区扫描</div></div>';
      setStatus('RAID重组完成');
    }
  });
}


// ---------- Mac盘 HFS+ ----------
async function startHfsList(){ const src=srcPath(); if(!src)return toast('先选设备或完成镜像');
  const part=carvePartStart() || 0;      // 无分区信息时=0(整镜像即HFS+卷)
  const params={source:src,partition_lba:part};
  if($('hfsExtract')&&$('hfsExtract').checked){params.extract=true; params.out=($('extractOut').value.trim()||'D:\恢复输出')+'_HFS提取';}
  $('hfsOut').textContent='遍历catalog…';
  const job=await api('/api/job',{method:'POST',body:JSON.stringify({kind:'hfslist',params})});
  setStatus('HFS+列表中…');
  let items=[];
  streamJob(job.id,ev=>{
    if(ev.event==='phase')$('hfsOut').textContent=ev.message;
    if(ev.event==='progress')$('hfsOut').textContent='提取 '+ev.extracted+'/'+ev.total;
    if(ev.event==='done'){
      if(ev.kind==='apfs'){$('hfsOut').innerHTML='<div class="verdict warn"><b>APFS容器</b><div>'+esc(ev.detail||'')+'</div></div>';return;}
      (ev.files||[]).forEach(f=>{ items.push('<div class="kv"><b>'+esc(f.path)+'</b><div>'+fmtSize(f.size)+' · '+f.extents+'段extent</div></div>'); });
      $('hfsOut').innerHTML='<div class="verdict ok"><b>'+ev.count+' 个文件 · '+ev.folders+' 个目录</b>'+(ev.saved&&ev.saved.length?(' · 已提取'+ev.saved.length+'个'):'')+'</div>'+(items.join('')||'<div class="muted">catalog为空</div>');
      setStatus('HFS+列表完成');
    }
  });
}

async function autoPart(want){
  // 已选分区优先; 没有则静默按签名自动定位(秒级)
  const ps=(S.scanResult&&S.scanResult.partitions)||[];
  if(ps.length){ const lba=+($('partSel').value||ps[0].start_lba||0); if(lba)return lba*512; }
  try{ const r=await api('/api/findpart?src='+encodeURIComponent(srcPath())+'&want='+want);
    if(r.ok&&r.part)return r.part.offset;
  }catch(e){}
  return 0;
}

// ---------- exFAT深度挖掘 ----------
async function startExfatDeep(){ const src=srcPath(); if(!src)return toast('先选设备或完成镜像');
  const part=await autoPart('exfat');
  if(!part)return toast('未在源中找到exFAT分区(可先跑分区扫描确认)');
  const params={source:src,partition_lba:part};
  if($('exfatExtract')&&$('exfatExtract').checked){params.extract=true; params.out=($('extractOut').value.trim()||'D:\\恢复输出')+'_exFAT深挖';}
  $('exfatOut').textContent='全簇扫描中…';
  const job=await api('/api/job',{method:'POST',body:JSON.stringify({kind:'exfatdeep',params})});
  setStatus('exFAT深度挖掘中…');
  let items=[];
  streamJob(job.id,ev=>{
    if(ev.event==='scan_progress')$('exfatOut').textContent=Math.round(ev.clusters*100/ev.clusters_total)+'% · 已发现 '+ev.found+' 项';
    if(ev.event==='progress')$('exfatOut').textContent='提取中 '+ev.extracted+'/'+ev.total;
    if(ev.event==='done'){
      (ev.records||[]).forEach(r=>{
        const tag=r.deleted?'<span style="color:var(--warn)">已删</span>':'<span style="color:var(--acc)">孤儿</span>';
        const chk=(r.hash_ok&&r.set_checksum_ok)?'双校验通过':(r.hash_ok?'hash通过':'<span class="muted">校验未过</span>');
        let ex='';
        if(r.extract_mode){ ex=' · 提取'+(r.extracted_size||0)+'B';
          if(r.extract_mode==='contig_fallback') ex+='（链已清·连续兜底'+(r.truncated?'·截断':'')+'）'; }
        items.push('<div class="kv"><b>'+esc(r.name||'?')+'</b><div>'+tag+' · '+fmtSize(r.size)+' · 修改'+(r.modified||'?')+' · '+chk+ex+'</div></div>');
      });
      const head='已删 '+ev.deleted+' · 孤儿 '+ev.orphans+((ev.saved&&ev.saved.length)?(' · 已提取 '+ev.saved.length+' 个'):'');
      $('exfatOut').innerHTML='<div class="verdict ok"><b>'+head+'</b></div>'+(items.join('')||'<div class="muted">未发现已删/孤儿条目</div>');
      setStatus('exFAT深挖完成');
    }
  });
}


// ---------- PST/OST检查 ----------
async function startPstCheck(){
  const src=$('pstFile').value.trim(); if(!src)return toast('先填PST/OST文件路径');
  $('pstOut').textContent='检查中…';
  const job=await api('/api/job',{method:'POST',body:JSON.stringify({kind:'pstcheck',params:{source:src}})});
  setStatus('PST检查中…');
  streamJob(job.id,ev=>{
    if(ev.event==='phase')$('pstOut').textContent=ev.message;
    if(ev.event==='done'){
      if(ev.healthy){
        $('pstOut').innerHTML='<div class="verdict ok"><b>结构健康</b><div>'+esc(ev.index_type||'')+' · '+ev.folder_count+'目录 · '+ev.message_count+'邮件 · 孤儿'+ev.orphan_messages+' · 缺块'+ev.missing_blocks+'</div><div class="muted">desc '+ev.desc_count+'条 · 块 '+ev.block_count+'个 · 加密:'+ev.encryption+'</div></div>';
      } else if(ev.recovered_pages!=null){
        $('pstOut').innerHTML='<div class="verdict warn"><b>头损 — 兜底扫描找回 '+ev.recovered_messages+' 封邮件清单</b><div>扫到 '+ev.recovered_pages+' 个描述叶页'+(ev.detail?' · '+esc(ev.detail):'')+'</div></div>';
      } else {
        $('pstOut').innerHTML='<div class="verdict warn"><b>'+esc(ev.detail||'无法解析')+'</b></div>';
      }
      setStatus('PST检查完成');
    }
  });
}

// ---------- BitLocker解锁 ----------
async function startBlUnlock(){
  const src=srcPath(); if(!src)return toast('先选设备或完成镜像');
  const pw=$('blPass').value, rk=$('blRecovery').value.trim();
  if(!pw&&!rk)return toast('填密码或48位恢复口令(留空密码则试清除密钥+恢复口令)');
  const out=($('extractOut').value.trim()||'D:\\恢复输出')+'_解锁';
  const params={source:src,out};
  { const r=carvePartRange(); if(r)params.partition_lba=r.start;
    else { const p=await autoPart('bitlocker'); if(p)params.partition_lba=p; } }
  if(pw)params.password=pw;
  if(rk)params.recovery=rk;
  $('blOut').textContent='解析元数据+密钥拉伸中(约数秒)…';
  const job=await api('/api/job',{method:'POST',body:JSON.stringify({kind:'blunlock',params})});
  setStatus('BitLocker解锁中…');
  streamJob(job.id,ev=>{
    if(ev.event==='phase')$('blOut').textContent=ev.message;
    if(ev.event==='detected')$('blOut').textContent='保护器: '+(ev.protectors||[]).join('/')+' · 算法0x'+(ev.algorithm||0).toString(16);
    if(ev.event==='unlocked')$('blOut').textContent='已通过'+ev.via+'解锁 · 解密中…';
    if(ev.event==='progress')$('blOut').textContent='解密 '+Math.round(ev.bytes_done*100/ev.bytes_total)+'%';
    if(ev.event==='done'){
      S.imageFile=ev.raw; S.scanResult=null;
      $('blOut').innerHTML='<div class="verdict ok"><b>'+esc(ev.via)+'解锁+解密完成</b><div>已设为当前镜像：<code>'+esc(ev.raw)+'</code>'+(ev.volume_size?(' · 卷 '+fmtSize(ev.volume_size)):'')+'</div><div class="muted">下一步：分区页"扫描源"或恢复页直接挖</div></div>';
      setStatus('BitLocker已解锁');
    }
  });
}

// ---------- ext4日志挖掘 ----------
async function startExt4Journal(){ const src=srcPath(); if(!src)return toast('先选设备或完成镜像');
  const part=await autoPart('ext');
  if(!part)return toast('未在源中找到ext分区(可先跑分区扫描确认)');
  const params={source:src,partition_lba:part};
  if($('ext4Recover')&&$('ext4Recover').checked){
    params.out=($('extractOut').value.trim()||'D:\\恢复输出')+'_ext4日志';
    params.recover='*';
  }
  $('ext4Out').textContent='解析JBD2日志中…';
  const job=await api('/api/job',{method:'POST',body:JSON.stringify({kind:'ext4journal',params})});
  setStatus('ext4日志挖掘中…');
  let items=[];
  streamJob(job.id,ev=>{
    if(ev.event==='phase')$('ext4Out').textContent=ev.message;
    if(ev.event==='progress')$('ext4Out').textContent='恢复中 '+ev.recovered+'/'+ev.total;
    if(ev.event==='done'){
      (ev.names||[]).forEach(n=>{
        const st=n.main_inode==='存活'?'存活':(n.main_inode==='已删'?'<span style="color:var(--warn)">已删</span>':'<span class="muted">未知</span>');
        const jc=n.journal_copy?(' · 日志副本'+fmtSize(n.journal_copy.size)):'';
        const rc=n.recover?' · <b>已恢复</b>'+(n.complete===false?'(不完整)':''):'';
        items.push('<div class="kv"><b>'+esc(n.name||('inode'+n.journal_ino))+'</b><div>ino '+(n.journal_ino||'?')+' · '+st+(n.size?(' · '+fmtSize(n.size)):'')+jc+rc+'</div></div>');
      });
      const tail=ev.uncommitted?(' · <span style="color:var(--warn)">崩溃尾巴'+ev.uncommitted+'笔未提交</span>'):'';
      $('ext4Out').innerHTML='<div class="verdict ok"><b>'+esc(ev.volume_name||'ext4')+' · '+ev.transactions+'笔事务'+tail+(ev.recovered&&ev.recovered.length?(' · 恢复'+ev.recovered.length+'个文件'):'')+'</b></div>'+(items.join('')||'<div class="muted">日志里没有可识别的目录块</div>');
      S.ext4Names=ev.names||[]; S.ext4Params=params;
      setStatus('ext4日志挖掘完成');
    }
  });
}
async function ext4RecoverIno(ino){
  if(!S.ext4Params)return toast('先跑一次日志挖掘');
  const params=Object.assign({},S.ext4Params,{out:($('extractOut').value.trim()||'D:\\恢复输出')+'_ext4日志',recover:[ino]});
  const job=await api('/api/job',{method:'POST',body:JSON.stringify({kind:'ext4journal',params})});
  $('ext4Out').textContent='按inode '+ino+'恢复中…';
  streamJob(job.id,ev=>{ if(ev.event==='done'){const r=(ev.recovered||[])[0]||{};
    $('ext4Out').innerHTML='<div class="verdict '+(r.error?'bad':'ok')+'"><b>'+(r.error?('失败: '+esc(r.error)):('已恢复 '+fmtSize(r.size||0)+(r.complete===false?' · 不完整':'')+' → <code>'+esc(r.file||'')+'</code>'))+'</b></div>'; } });
}

// ---------- 内容特征分析页 ----------
async function startContent(kind){
  const outBox={'exifscan':'ctExif','sqlscan':'ctSql','sqlscan_img':'ctSql','b64scan':'ctB64','evlogscan':'ctEv','aesdetect':'ctAes','ransomscan':'ctRansom','chkscan':'ctChk'}[kind];
  const box=$(outBox); box.textContent='运行中…';
  let params={}, k=kind;
  if(kind==='aesdetect'){ const src=srcPath(); if(!src)return toast('先选设备或完成镜像');
    params={source:src}; const r=carvePartRange(); if(r)params.end=r.end;
  } else if(kind==='sqlscan_img'){ const src=srcPath(); if(!src)return toast('先选设备或完成镜像');
    k='sqlscan'; params={source:src,out:($('extractOut').value.trim()||'D:\\恢复输出')+'_SQLite',mapfile:''};
    const r=carvePartRange(); if(r){params.start=r.start; params.end=r.end;}
  } else { const dir=$('ctDir').value.trim(); if(!dir)return toast('先填目录（浏览选择已恢复的输出目录）');
    k=kind; params={dir};
    if(kind==='b64scan')params.out=($('extractOut').value.trim()||'D:\\恢复输出')+'_Base64';
    if(kind==='chkscan'&&$('chkExtract')&&$('chkExtract').checked){params.extract=true; params.out=($('extractOut').value.trim()||'D:\\恢复输出')+'_CHK拆分';}
  }
  const job=await api('/api/job',{method:'POST',body:JSON.stringify({kind:k,params})});
  setStatus('内容分析中…');
  streamJob(job.id,ev=>{
    if(ev.event==='phase')box.textContent=ev.message;
    if(ev.event==='progress')box.textContent='进行中… '+JSON.stringify(ev).slice(0,120);
    if(ev.event==='done'){renderContent(kind,box,ev);setStatus('内容分析完成');}
  });
}
function renderContent(kind,box,ev){
  if(kind==='exifscan'){
    const rows=(ev.results||[]).filter(r=>r.exif).slice(0,80).map(r=>{
      const e=r.exif; const gps=e.gps?e.gps[0].toFixed(4)+','+e.gps[1].toFixed(4):'';
      return '<div class="kv"><b>'+esc(r.file.split('\\').pop())+'</b><div>'+
        (e.datetime_original||e.datetime||'无时间')+' · '+esc([e.make,e.model].filter(Boolean).join(' ')||'?')+
        (gps?' · 📍'+gps:'')+'</div></div>';}).join('');
    box.innerHTML='<div class="verdict ok"><b>'+ev.photos+' 张照片 · '+ev.with_exif+' 张含EXIF'+
      (ev.organized?' · 已归档 '+ev.organized+' 张到 '+esc(ev.out||''):'')+'</b></div>'+(rows||'<div class="muted">没有含EXIF的照片</div>');
  } else if(kind==='sqlscan'||kind==='sqlscan_img'){
    if(ev.dbs){ const rows=(ev.dbs||[]).map(d=>{
      const fr=d.free_fragments?('<div class="muted">自由页片段: '+d.free_fragments.slice(0,3).map(esc).join(' / ')+'</div>'):'';
      return '<div class="kv"><b>'+esc(d.path.split('\\').pop())+'</b><div>'+
        (d.encrypted?'<span style="color:var(--warn)">已加密(新版微信WCDB?)</span>':
        (d.tables||[]).map(t=>esc(t.name)+'('+t.rows+')').join(' · ')+(d.error?' · '+esc(d.error):''))+
        (d.freelist_pages?' · 自由页'+d.freelist_pages:'')+'</div>'+fr+'</div>';}).join('');
      box.innerHTML='<div class="verdict ok"><b>扫描 '+ev.scanned+' 个文件 · '+ev.dbs.length+' 个SQLite库</b></div>'+(rows||'<div class="muted">未发现SQLite库</div>');
    } else { const rows=(ev.saved||[]).map(s=>'<div>'+esc(s)+'</div>').join('');
      box.innerHTML='<div class="verdict ok"><b>镜像内找到 '+ev.found.length+' 个库</b></div>'+rows; }
  } else if(kind==='b64scan'){
    const rows=(ev.files||[]).slice(0,60).map(r=>'<div class="kv"><b>'+esc(r.file.split('\\').pop())+'</b><div>'+
      r.hits.map(h=>h.ext.toUpperCase()+' '+fmtSize(h.size)+(h.depth>0?'(嵌套'+h.depth+'层)':'')).join(' · ')+'</div></div>').join('');
    box.innerHTML='<div class="verdict ok"><b>'+ev.files.length+' 个文件含内嵌数据 · 共 '+ev.found+' 个</b></div>'+(rows||'<div class="muted">未发现内嵌文件</div>');
  } else if(kind==='evlogscan'){
    const TAG_CLS={bad:'rec-none',warn:'rec-partial',usb:'rec-full'};
    const rows=(ev.logs||[]).map(l=>{
      const chips=(l.events||[]).slice(0,12).map(e=>'<span class="pill '+(TAG_CLS[e.tag]||'')+'" title="事件ID '+e.id+' ×'+e.count+'">'+esc(e.name||('事件'+e.id))+' ×'+e.count+'</span>').join(' ');
      const notable=(l.notable||[]).slice(-24).reverse().map(n=>
        '<div class="kv"><b>'+(n[0]?n[0].slice(0,19).replace('T',' '):'?')+'</b><div>事件'+n[2]+' · 记录#'+n[1]+(n[3]?' · '+esc(n[3]):'')+'</div></div>').join('');
      return '<div class="kv"><b>'+esc(l.file)+'</b><div>'+l.records+' 条记录 · '+
        (l.first?String(l.first).slice(0,19).replace('T',' '):'?')+' → '+(l.last?String(l.last).slice(0,19).replace('T',' '):'?')+
        (l.bad_chunks?' · 坏块'+l.bad_chunks:'')+'</div>'+
        (chips?'<div style="margin-top:4px;display:flex;flex-wrap:wrap;gap:4px">'+chips+'</div>':'')+
        (notable?'<div style="margin-top:6px"><b>高价值事件时间线</b>'+notable+'</div>':'')+'</div>';}).join('');
    box.innerHTML='<div class="verdict ok"><b>'+(ev.logs||[]).length+' 个日志 · 事件ID已解析</b></div>'+(rows||'<div class="muted">未找到evtx</div>');
  } else if(kind==='aesdetect'){
    const v=ev.volume||{};
    const regions=(ev.high_regions||[]).slice(0,10).map(r=>fmtSize(r[0])+' ~ '+fmtSize(r[1])).join(' · ');
    box.innerHTML='<div class="verdict '+(v.kind?'warn':'ok')+'"><b>'+(v.kind?v.kind.toUpperCase()+' — 数据被加密锁定':v.detail||'未检出加密卷')+'</b><div>高熵区 '+ev.high_pct+'% · 峰值熵 '+ev.max_entropy+'</div>'+
      (regions?'<div class="muted">高熵段: '+regions+'</div>':'')+'</div>';
  } else if(kind==='ransomscan'){
    const cls=ev.suspicion==='high'?'bad':(ev.suspicion==='medium'?'warn':'ok');
    const fam=ev.family?(' · 疑似家族 <b>'+esc(ev.family)+'</b>'+(ev.family_decryptor?' — '+esc(ev.family_decryptor):'')):'(未匹配到已知家族)';
    const evd=(ev.evidence||[]).map(e=>'<div class="muted">· '+esc(e)+'</div>').join('');
    const adv=(ev.advice||[]).map(a=>'<div>▸ '+esc(a)+'</div>').join('');
    box.innerHTML='<div class="verdict '+cls+'"><b>勒索痕迹: '+esc(ev.suspicion)+'</b>'+fam+'<div>共扫 '+(ev.stats?ev.stats.files:0)+' 个文件</div>'+evd+'</div>'+(adv?('<div class="actions" style="flex-direction:column;align-items:stretch">'+adv+'</div>'):'');
  } else if(kind==='chkscan'){
    const rows=(ev.results||[]).slice(0,60).map(r=>{
      const ps=(r.parts||[]).map(p=>esc(p.type)+' '+fmtSize(p.size)).join(' + ')||'<span class="muted">未识别</span>';
      return '<div class="kv"><b>'+esc(r.file.split('\\').pop())+'</b><div>'+ps+' · 覆盖率'+Math.round(r.coverage*100)+'%'+(r.extracted?(' · 已提取'+r.extracted.length+'个'):'')+'</div></div>';}).join('');
    box.innerHTML='<div class="verdict '+(ev.identified?'ok':'')+'"><b>'+ev.total+' 个CHK · '+ev.identified+' 个含可识别内容</b>'+(ev.out?(' · 已提取到 '+esc(ev.out)):'')+'</div>'+(rows||'<div class="muted">没有CHK文件</div>');
  }
}
async function organizeExif(){
  const dir=$('ctDir').value.trim(); if(!dir)return toast('先填目录');
  const out=($('extractOut').value.trim()||'D:\\恢复输出')+'_照片归档';
  $('ctExif').textContent='扫描并归档中…';
  const job=await api('/api/job',{method:'POST',body:JSON.stringify({kind:'exifscan',params:{dir,out,mode:'organize'}})});
  streamJob(job.id,ev=>{ if(ev.event==='done')renderContent('exifscan',$('ctExif'),ev); });
}

async function startWxdat(){ const dir=$('wxIn').value.trim(); const out=$('wxOut').value.trim();
  if(!dir||!out)return toast('先填 dat 目录和输出目录');
  $('wxStart').disabled=true; $('wxStat').textContent='启动中…'; $('wxResult').innerHTML='';
  const job=await api('/api/job',{method:'POST',body:JSON.stringify({kind:'wxdat',params:{dir,out}})});
  streamJob(job.id,ev=>{
    if(ev.event==='phase'){$('wxStat').textContent=ev.message;}
    if(ev.event==='progress'){ const pct=ev.files_total?Math.round(ev.files_done*100/ev.files_total):null;
      $('wxStat').textContent=(pct!=null?pct+'% · ':'')+ev.files_done+'/'+ev.files_total+' · 解密'+ev.ok+' · 跳过'+ev.skip+' · 失败'+ev.fail; setStatus('dat解密 '+(pct??'')+'%'); }
    if(ev.event==='done'){ $('wxStart').disabled=false; S.wxdatResult={ok:ev.ok,skip:ev.skip,fail:ev.fail};
      const exts=Object.entries(ev.exts||{}).map(([k,v])=>k+'×'+v).join(' · ')||'无';
      $('wxResult').innerHTML='<div class="verdict ok"><div class="kv"><b>解密完成</b><div>成功 '+ev.ok+' / 共 '+ev.total+' 个dat · 跳过 '+ev.skip+' · 失败 '+ev.fail+'</div><b>类型分布</b><div>'+esc(exts)+'</div><b>输出目录</b><div><code>'+esc(out)+'</code></div>'+(ev.key!=null?'<b>探测密钥</b><div class="mono">0x'+ev.key.toString(16).toUpperCase()+'</div>':'')+'</div></div>';
      setStatus('dat解密完成：'+ev.ok+'张'); }
  },()=>{$('wxStart').disabled=false;});
}

// 报告自动汇总: 从任务历史里取各通道最近一次成功结果(瘦身后传给后端)
function slimReportResult(kind, r){
  if(!r)return null;
  const pick=(...ks)=>{const o={};for(const k of ks){if(r[k]!=null)o[k]=r[k];}return o;};
  switch(kind){
    case 'mftscan': return Object.assign(pick('mode','records'),
      {files_total:(r.files||[]).length, deleted:(r.files||[]).filter(f=>f.deleted).length});
    case 'carve': case 'carve_free': return pick('files_found','ranges','bytes_scanned','fs','free_clusters');
    case 'h264carve': return pick('found','bytes','out');
    case 'exfatdeep': return Object.assign(pick('deleted','orphans','clusters_scanned'),{saved:(r.saved||[]).length});
    case 'ext4journal': return Object.assign(pick('volume_name','transactions','uncommitted','block_size'),
      {names:(r.names||[]).length, recovered:(r.recovered||[]).length});
    case 'blunlock': return pick('raw','via','algo','volume_size');
    case 'raidrebuild': return pick('level','stripe_kb','raw','virtual_size','ambiguous','candidates');
    case 'hfslist': return Object.assign(pick('count','folders','block_size'),{saved:(r.saved||[]).length});
    case 'pstcheck': return pick('index_type','healthy','detail','recovered_pages','recovered_messages','messages_total');
    case 'ransomscan': return pick('suspicion','family','family_decryptor','evidence','advice','stats');
    case 'vmexpand': return pick('kind','raw','usable_direct','virtual_size');
    case 'evlogscan': return {logs:(r.logs||[]).map(l=>({file:l.file,records:l.records,first:l.first,last:l.last,
      events:(l.events||[]).slice(0,8), notable:(l.notable||[]).slice(0,20)}))};
    case 'exifscan': return pick('photos','with_exif','organized','out');
    case 'sqlscan': return Object.assign(pick('scanned'),{dbs:(r.dbs||[]).length, carved:(r.saved||[]).length});
    case 'b64scan': return Object.assign(pick('found'),{files:(r.files||[]).length});
    case 'chkscan': return pick('total','identified','out');
    case 'mp4repair': return pick('ok','total');
    default: return pick();
  }
}
const REPORT_KINDS=['mftscan','carve','carve_free','h264carve','exfatdeep','ext4journal','blunlock',
  'raidrebuild','hfslist','pstcheck','ransomscan','vmexpand','evlogscan','exifscan','sqlscan','b64scan','chkscan','mp4repair'];

async function startReport(){ let out=$('reportOut').value.trim();
  if(!out){out=($('extractOut').value.trim()||'D:\\恢复输出')+'\\数据抢救报告.html'; $('reportOut').value=out;}
  if(!out.toLowerCase().endsWith('.html')){out+='.html'; $('reportOut').value=out;}
  $('reportStat').textContent='汇总任务历史并生成中…';
  const params={out};
  if(S.dev)Object.assign(params,{dev_model:S.dev.model,dev_path:S.dev.path,dev_size:S.dev.size,serial:S.dev.serial});
  if(S.probe)params.probe=S.probe; if(S.smart)params.smart=S.smart; if(S.imageResult)params.image=S.imageResult;
  if(S.scanResult&&S.scanResult.partitions)params.scan=S.scanResult.partitions;
  if(S.filesSummary)params.files=S.filesSummary; if(S.extractResult)params.extract=S.extractResult;
  if(S.wxdatResult)params.wxdat=S.wxdatResult; if(S.speedBins)params.speed_bins=S.speedBins;
  try{
    const jobs=(await api('/api/jobs')).jobs||[];
    for(const kind of REPORT_KINDS){
      const j=jobs.find(x=>x.kind===kind&&x.state==='done'&&x.result);
      if(!j)continue;
      const key=(kind==='carve_free')?'carve':kind;
      if(!params[key])params[key]=slimReportResult(kind, j.result);
    }
  }catch(e){}
  const job=await api('/api/job',{method:'POST',body:JSON.stringify({kind:'report',params})});
  streamJob(job.id,ev=>{ if(ev.event==='done'){$('reportStat').innerHTML='✅ 报告已生成：<code>'+esc(ev.out)+'</code>'; toast('HTML报告生成完成'); setStatus('报告生成完成');} });
}

async function startHash(){ const t=$('hashTarget').value.trim(); if(!t)return toast('填入镜像文件路径');
  const job=await api('/api/job',{method:'POST',body:JSON.stringify({kind:'hash',params:{source:t}})});
  $('hashOut').textContent='计算中…';
  streamJob(job.id,ev=>{
    if(ev.event==='hash_progress')$('hashOut').textContent=Math.round(ev.bytes_done*100/ev.bytes_total)+'%';
    if(ev.event==='done'){$('hashOut').textContent='SHA-256: '+(ev.sha256||'(见结果)'); setStatus('哈希完成');}
  });
}

const KIND_INFO={probe:{name:'体检',icon:'🩺'},image:{name:'镜像',icon:'💾'},scan:{name:'分区扫描',icon:'🗂️'},files:{name:'文件列表',icon:'📁'},extract:{name:'提取',icon:'📤'},carve:{name:'雕刻',icon:'🧩'},carve_free:{name:'雕刻·未分配',icon:'🧩'},h264carve:{name:'监控流雕刻',icon:'📹'},mftscan:{name:'MFT扫描',icon:'🧷'},mp4repair:{name:'视频修复',icon:'🎬'},davstrip:{name:'dav剥离',icon:'📹'},exfatdeep:{name:'exFAT深挖',icon:'🗂️'},vmexpand:{name:'虚拟盘展开',icon:'📦'},ext4journal:{name:'ext4日志',icon:'🐧'},blunlock:{name:'BitLocker解锁',icon:'🔓'},ransomscan:{name:'勒索检测',icon:'🦠'},chkscan:{name:'CHK识别',icon:'🧩'},raidrebuild:{name:'RAID重组',icon:'🧱'},hfslist:{name:'HFS+列表',icon:'🍎'},pstcheck:{name:'PST检查',icon:'📧'},exifscan:{name:'EXIF整理',icon:'📸'},sqlscan:{name:'SQLite残留',icon:'💾'},b64scan:{name:'Base64提取',icon:'🍞'},evlogscan:{name:'事件日志',icon:'📋'},aesdetect:{name:'加密检测',icon:'🔐'},hash:{name:'校验',icon:'#️⃣'},wxdat:{name:'dat解密',icon:'💬'},report:{name:'报告',icon:'📄'},verify:{name:'验证',icon:'🔬'}};
// ---------- 成果总览页 ----------
async function loadHarvest(){
  const box=$('hvList'); if(!box)return; box.innerHTML='<div class="phase">盘点中…</div>';
  try{
    const r=await api('/api/harvest'); const es=r.entries||[];
    if(!es.length){ $('hvSummary').textContent=''; $('hvMeta').textContent='0 项产出';
      box.innerHTML='<div class="empty-state"><div class="empty-ico">📦</div><div>还没有产出 — 先跑提取/雕刻/深挖任务</div></div>'; return; }
    $('hvMeta').textContent=es.length+' 项产出';
    $('hvSummary').innerHTML='<div class="verdict ok"><b>共 '+es.length+' 个产出位置 · '+fmtSize(r.total_bytes||0)+' · '+String(r.total_files||0).toLocaleString()+' 个文件</b>'+
      (r.missing?'<div class="muted">另有 '+r.missing+' 个历史输出路径已不存在(被移动或删除)</div>':'')+'</div>';
    let html='<table class="tbl"><tr><th>类型</th><th>路径</th><th>文件</th><th>大小</th><th>最近写入</th><th></th></tr>';
    for(const e of es){
      const t=new Date(e.mtime*1000);
      const ki=KIND_INFO[e.kinds[0]];
      html+='<tr><td>'+(ki?ki.icon+' ':'')+esc(e.kind)+'</td>'+
        '<td class="mono" style="max-width:420px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="'+esc(e.path)+'">'+(e.dir?'📁 ':'📄 ')+esc(e.path)+'</td>'+
        '<td>'+(e.dir?e.files.toLocaleString():'-')+'</td><td>'+fmtSize(e.bytes)+'</td>'+
        '<td class="ttime">'+t.toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})+'</td>'+
        '<td><button class="btn mini" data-p="'+esc(e.path)+'" onclick="openPath(this.dataset.p)">打开</button></td></tr>';
    }
    box.innerHTML=html+'</table>';
  }catch(err){ box.innerHTML='<div class="phase">加载失败: '+esc(String(err))+'</div>'; }
}
async function openPath(p){ try{ const r=await api('/api/open',{method:'POST',body:JSON.stringify({path:p})});
  if(!r.ok)toast('打开失败: '+(r.error||'')); }catch(e){ toast('打开失败'); } }

async function loadHistory(){ const box=$('histList'); box.innerHTML='<div class="phase">加载中…</div>';
  try{ const j=await api('/api/jobs'); const jobs=j.jobs||[];
    $('histCount').textContent=jobs.length+' 个任务';
    if(!jobs.length){box.innerHTML='<div class="empty-state"><div class="empty-ico">📋</div><div>暂无任务记录</div></div>';return;}
    let html='<table class="tbl"><tr><th>时间</th><th>类型</th><th>状态</th><th>关键信息</th></tr>';
    for(const job of jobs){ const info=KIND_INFO[job.kind]||{name:job.kind,icon:'❓'};
      const st=job.state==='done'?'<span class="pill rec-full">完成</span>':job.state==='failed'?'<span class="pill rec-none">失败</span>':job.state==='running'?'<span class="pill rec-partial">运行中</span>':'<span class="pill">等待</span>';
      let detail=''; const r=job.result||{};
      switch(job.kind){case'probe':detail=r.stable!=null?(r.stable?'读取稳定':'读数不稳'):'';break;case'image':detail=r.finished_bytes?fmtSize(r.finished_bytes)+(r.bad_bytes?' 坏'+fmtSize(r.bad_bytes):''):'';break;case'scan':detail=r.partitions?r.partitions.length+'个分区':'';break;case'files':detail=(r.files_total||'?')+' 项 / 已删'+(r.deleted||'?');break;case'extract':detail=(r.extracted!=null?'成功'+r.extracted:'')+(r.failed?' 失败'+r.failed:'');break;case'hash':detail=r.sha256?r.sha256.slice(0,16)+'…':'';break;case'wxdat':detail=r.ok!=null?'解密'+r.ok+' 跳过'+(r.skip||0):'';break;}
      if(!detail&&job.error){detail='<span style="color:var(--bad);font-size:12px">'+esc(String(job.error).split(String.fromCharCode(10))[0].slice(0,60))+'</span>';}
      const t=new Date(job.created*1000);
      html+='<tr><td class="ttime">'+t.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'})+'</td><td>'+info.icon+' '+info.name+'</td><td>'+st+'</td><td style="font-size:12.5px">'+detail+'</td></tr>';}
    html+='</table>'; box.innerHTML=html;
  }catch(e){box.innerHTML='<div class="phase">加载失败: '+esc(e.message)+'</div>';}
}

const ICONS={home:'M3 12l9-9 9 9M5 10v10a1 1 0 001 1h3v-6h6v6h3a1 1 0 001-1V10',probe:'M12 21s-7-4.5-9.5-9C.8 8.5 2.5 4 7 4c2.5 0 4 1.5 5 3 1-1.5 2.5-3 5-3 4.5 0 6.2 4.5 4.5 8-2.5 4.5-9.5 9-9.5 9z',image:'M19 21H5a2 2 0 01-2-2V5a2 2 0 012-2h11l5 5v11a2 2 0 01-2 2zM8 21v-8h8v8M8 3v5M2 13h5M17 13h5',partitions:'M22 19a2 2 0 01-2 2H4a2 2 0 01-2-2V5a2 2 0 012-2h5l2 3h9a2 2 0 012 2z',verify:'M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10zM9 12l2 2 4-4',recover:'M22 19a2 2 0 01-2 2H4a2 2 0 01-2-2V5a2 2 0 012-2h5l2 3h9a2 2 0 012 2v1M12 8v6M9 13l3 3 3-3',wxdat:'M21 11.5a8.38 8.38 0 01-.9 3.8 8.5 8.5 0 01-7.6 4.7 8.38 8.38 0 01-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 01-.9-3.8 8.5 8.5 0 014.7-7.6 8.38 8.38 0 013.8-.9h.5a8.48 8.48 0 018 8z',history:'M12 8v4l3 3M3.05 11a9 9 0 101.7-6.5L3 5M3 3v5h5',about:'M12 8v4l3 3M3.05 11a9 9 0 101.7-6.5L3 5M3 3v5h5'};
function ico(name,size=18){ const d=ICONS[name]; if(!d)return''; return '<svg width="'+size+'" height="'+size+'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="'+d+'"/></svg>'; }
document.querySelectorAll('.nav-item .nav-ico').forEach(el=>{ const page=el.closest('.nav-item').dataset.page; const map={home:'home',probe:'probe',image:'image',partitions:'partitions',verify:'verify',recover:'recover',wxdat:'wxdat',history:'history',about:'history'}; const name=map[page]; if(name){el.innerHTML=ico(name,17); el.style.display='flex'; el.style.alignItems='center'; el.style.justifyContent='center';} });

document.addEventListener('paste',e=>{ const text=(e.clipboardData||window.clipboardData).getData('text').trim(); if(!text)return; const active=document.querySelector('.page:not(.hidden)'); if(!active)return; const inputs=active.querySelectorAll('input.txt.mono'); if(inputs.length){inputs[0].value=text; toast('已粘贴路径');} });

(function initZoom(){ const saved=parseFloat(localStorage.getItem('uiZoom')||'1'); applyZoom(saved,false);
  function applyZoom(z,notify){ z=Math.min(1.6,Math.max(0.65,z)); document.documentElement.style.zoom=z; localStorage.setItem('uiZoom',z); if(notify)toast('界面缩放 '+Math.round(z*100)+'%（Ctrl+0 复原）'); }
  window.applyZoom=applyZoom;
  window.addEventListener('wheel',e=>{ if(!e.ctrlKey)return; e.preventDefault(); applyZoom(parseFloat(localStorage.getItem('uiZoom')||'1')+(e.deltaY<0?0.05:-0.05),true); },{passive:false});
  window.addEventListener('keydown',e=>{ if(e.ctrlKey&&(e.key==='0'||e.code==='Numpad0')){e.preventDefault(); applyZoom(1,true);} });
})();

async function checkResume(){ try{ const j=await api('/api/jobs'); const running=(j.jobs||[]).filter(x=>x.state==='running'||x.state==='pending'); if(running.length){toast('检测到未完成任务，可从控制台查看'); conLog('[系统] 有'+running.length+'个未完成任务');} }catch(e){} }


// ── 多主题系统 ──
const THEMES = [
  {id:'deepspace', name:'深空',     sw:'#2dd4bf', dark:true},
  {id:'mocha',     name:'猫布丁',   sw:'#cba6f7', dark:true},
  {id:'nord',      name:'北海',     sw:'#88c0d0', dark:true},
  {id:'dracula',   name:'吸血鬼',   sw:'#bd93f9', dark:true},
  {id:'gruvbox',   name:'暖阳',     sw:'#b57614', dark:false},
  {id:'tokyo',     name:'东京夜',   sw:'#7aa2f7', dark:true},
];
function applyTheme(id){
  const th = THEMES.find(t => t.id === id) || THEMES[0];
  document.documentElement.setAttribute('data-theme', th.id);
  localStorage.setItem('uiTheme', th.id);
  const sw = document.getElementById('curThemeSwatch');
  const nm = document.getElementById('curThemeName');
  if (sw) sw.style.background = th.sw;
  if (nm) nm.textContent = th.name;
  document.querySelectorAll('.theme-opt').forEach(el => {
    el.classList.toggle('active', el.dataset.theme === th.id);
  });
}
function toggleThemeMenu(){
  const menu = document.getElementById('themeMenu');
  if (!menu.children.length){
    const cur = document.documentElement.getAttribute('data-theme');
    menu.innerHTML = THEMES.map(function(t){
      var active = t.id === cur ? ' active' : '';
      var light = t.dark ? '' : ' <span style="font-size:10px;color:var(--muted2)">亮色</span>';
      return '<div class="theme-opt' + active + '" data-tid="' + t.id + '">' +
        '<span class="swatch" style="background:' + t.sw + '"></span>' + t.name + light + '</div>';
    }).join('');
    menu.addEventListener('click', function(e){
      var opt = e.target.closest('.theme-opt');
      if (opt){ applyTheme(opt.dataset.tid); toggleThemeMenu(); }
    });
  }
  menu.classList.toggle('hidden');
}
// 点击外部关闭
document.addEventListener('click', e => {
  const picker = document.getElementById('themePicker');
  const menu = document.getElementById('themeMenu');
  if (picker && menu && !picker.contains(e.target)) menu.classList.add('hidden');
});
// 启动时恢复主题
applyTheme(localStorage.getItem('uiTheme') || 'deepspace');

checkResume();
// ── MP4视频修复 ──
async function startDavStrip(){
  const src = $('davSrc').value.trim();
  if (!src) return toast('先填 .dav 文件路径');
  $('davStat').textContent = '剥离中…';
  const params = {source: src, out: ($('extractOut').value.trim()||'D:\\恢复输出') + '_dav剥离'};
  const job = await api('/api/job', {method:'POST', body: JSON.stringify({kind:'davstrip', params})});
  streamJob(job.id, ev=>{
    if (ev.event==='phase') $('davStat').textContent = ev.message;
    if (ev.event==='progress') $('davStat').textContent = Math.round(ev.bytes_done*100/Math.max(ev.bytes_total,1))+'% · 已剥'+ev.pes+'个视频PES';
    if (ev.event==='done'){
      const res = ev.width ? ' ('+ev.width+'×'+ev.height+(ev.fps?(' @'+Math.round(ev.fps)+'fps'):'')+')' : '';
      $('davStat').innerHTML = '<div class="verdict ok"><div class="kv"><b>'+
        (ev.codec||'').toUpperCase()+' 裸流 '+fmtSize(ev.bytes_out||0)+res+'</b><div>'+
        'pack头 '+ev.packs+' · 视频PES '+ev.video_pes+' · 音频PES '+(ev.audio_pes||0)+' · 帧数 '+(ev.vcl_frames||'?')+'</div>'+
        (ev.mp4?'<b>MP4已生成</b><div class="mono" style="font-size:11px">'+esc(ev.mp4)+'</div>':'<div class="muted">MP4未生成(裸流原件保留, 可再走雕刻面板)</div>')+
        '<b>裸流原件</b><div class="mono" style="font-size:11px">'+esc(ev.raw)+'</div></div></div>';
      setStatus('dav剥离完成');
    }
  });
}

async function startMp4Repair(){
  const ref = $('mp4Ref').value.trim();
  const target = $('mp4Corrupt').value.trim();
  if (!ref || !target) return toast('先填参考视频和损坏视频/目录');
  $('mp4Start').disabled = true;
  $('mp4Stat').textContent = '启动中…';
  $('mp4Results').innerHTML = '';
  const isDir = !target.toLowerCase().match(/\.(mp4|mov|3gp)$/);
  const params = isDir ? {reference: ref, directory: target} : {reference: ref, corrupt: target};
  const job = await api('/api/job', {method:'POST', body: JSON.stringify({kind:'mp4repair', params})});
  streamJob(job.id, ev=>{
    if (ev.event==='phase'){$('mp4Stat').textContent = ev.message;}
    if (ev.event==='progress'){$('mp4Stat').textContent = '修复 '+ev.files_done+'/'+ev.files_total;}
    if (ev.event==='done'){
      $('mp4Start').disabled = false;
      const results = ev.results || [];
      let html = '<div class="verdict '+(ev.ok>0?'ok':'bad')+'"><div class="kv"><b>修复结果</b><div>成功 '+ev.ok+' / '+ev.total+'</div></div></div>';
      if (results.length){
        html += '<table class="tbl" style="margin-top:10px"><tr><th>文件</th><th>状态</th><th>输出/错误</th></tr>';
        for (const r of results.slice(0,20)){
          html += '<tr><td>'+esc(r.file)+'</td><td>'+(r.ok?'<span class="pill rec-full">成功</span>':'<span class="pill rec-none">失败</span>')+'</td><td class="mono" style="font-size:11px">'+esc(r.ok?r.output:(r.error||''))+'</td></tr>';
        }
        html += '</table>';
      }
      $('mp4Results').innerHTML = html;
      $('mp4Stat').textContent = '';
      setStatus('视频修复完成：'+ev.ok+'/'+ev.total);
    }
  }, ()=>{$('mp4Start').disabled = false;});
}

// ---------- 首页场景入口(按事故直达; 熟手照旧走侧栏) ----------
const SCENARIOS=[
 {t:'U盘/移动硬盘打不开',d:'提示要格式化 / 显示0字节 / 目录乱码',steps:[
   ['1·先体检','probe',null],['2·镜像保现场','image',null],['3·分区扫描','partitions',null],['4·恢复文件','recover',null]]},
 {t:'文件被误删/清空',d:'删除、清空回收站、快速格式化过',steps:[
   ['1·分区扫描','partitions',null],['2·列文件(勾已删)','recover',null],['3·没名字就雕刻兜底','recover','carvePanel'],
   ['·exFAT盘深挖','recover','exfatPanel']]},
 {t:'监控硬盘/录像看不了',d:'海康等DVR/NVR盘、.dav文件、断电MP4',steps:[
   ['1·.dav剥离转MP4','mp4repair','davPanel'],['2·断电MP4修复','mp4repair',null],['3·无容器裸流雕刻','recover','h264Panel']]},
 {t:'盘被BitLocker锁住',d:'插上要密码/恢复口令的加密盘',steps:[
   ['1·密码或口令解锁','content','blPanel'],['2·解锁后按普通盘恢复','partitions',null]]},
 {t:'Mac电脑的盘',d:'HFS+文件系统(APFS仅识别)',steps:[
   ['1·列出并提取文件','recover','hfsPanel'],['2·没结果走雕刻','recover','carvePanel']]},
 {t:'文件全变乱码·疑似勒索',d:'扩展名被改/到处勒索信/被锁死',steps:[
   ['1·勒索现场检测','content','ransomPanel'],['2·先看免费解密器线索','content','ransomPanel'],['3·加密卷检测','content','blPanel']]},
 {t:'虚拟机盘',d:'VMDK/VHD/VHDX文件里的数据',steps:[
   ['1·虚拟盘展开','partitions','vmPanel'],['2·展开后按普通盘走','partitions',null]]},
 {t:'NAS/服务器阵列',d:'多块成员盘(RAID)、ext4/NAS系统',steps:[
   ['1·RAID重组','partitions','raidPanel'],['2·ext4日志挖掘','recover','ext4Panel'],['3·重组后按普通盘走','partitions',null]]},
];
function renderScenarios(){
  const grid=document.getElementById('sceneGrid');
  if(!grid)return;
  grid.innerHTML=SCENARIOS.map((sc,i)=>
    '<div class="tool-card" onclick="pickScenario('+i+')"><div class="tool-tag rescue">'+esc(sc.t.split('·')[0].slice(0,12))+'</div>'+
    '<div class="tool-head">'+esc(sc.t)+'</div><p>'+esc(sc.d)+'</p></div>').join('');
}
function goAnchor(page,anchor){
  go(page);
  if(anchor){ setTimeout(()=>{ const el=document.getElementById(anchor); if(el)el.scrollIntoView({behavior:'smooth',block:'start'}); },60); }
}
function pickScenario(i){
  const sc=SCENARIOS[i];
  S.scenario=sc;
  goAnchor(sc.steps[0][1], sc.steps[0][2]);
  renderNextSteps();
  toast('已按「'+sc.t+'」给出步骤，按建议条走');
}

// ---------- 首页下一步建议(场景模式下=场景步骤链) + 关于页版本 ----------
function renderNextSteps(){
  const panel=$('nextStepsPanel'), box=$('nextSteps');
  if(!panel||!box)return;
  // 场景模式: 建议条跟随当前页(插到页标题下); 默认模式: 留在首页场景卡下方
  const home=document.getElementById('page-home');
  if(home){
    const active=document.querySelector('.page:not(.hidden)');
    if(S.scenario){
      if(active!==home && panel.parentElement!==active){
        const h1=active.querySelector('h1');
        if(h1)active.insertBefore(panel, h1.nextSibling);
      }
    } else if(panel.parentElement!==home){
      const scene=document.getElementById('scenePanel');
      home.insertBefore(panel, scene.nextSibling);
    }
  }
  const steps=[];
  if(S.scenario){
    S.scenario.steps.forEach(st=>steps.push([st[0], st[1], st[2]]));
    steps.push(['✕ 退出场景模式','__exit__',null]);
  } else {
    const hasDev=!!S.dev, hasImg=!!S.imageFile;
    if(!hasDev&&!hasImg){
      steps.push(['🩺 选一块设备先体检','probe',null]);
      steps.push(['💾 已有镜像文件? 直接用','image',null]);
    } else {
      if(hasDev&&!S.probe) steps.push(['🩺 先体检(读稳定性+SMART)','probe',null]);
      if(S.probe&&!S.imageResult&&!hasImg) steps.push([S.probe&&S.probe.stable?'💾 做快速镜像保现场':'💾 校验式镜像(读不稳/有病灶)','image',null]);
      if(hasImg&&!S.scanResult) steps.push(['🗂️ 对镜像跑分区扫描','partitions',null]);
      if(S.scanResult) steps.push(['📁 浏览/提取文件','recover',null]);
    }
    if(S.extractResult){ steps.push(['📦 查看成果总览','harvest',null]); steps.push(['📄 生成HTML报告','result',null]); }
  }
  if(!steps.length){ panel.style.display='none'; return; }
  panel.style.display='';
  panel.querySelector('.micro').textContent=S.scenario?('🧭 场景：'+S.scenario.t):'🧭 下一步建议';
  panel.querySelector('.head-meta').textContent=S.scenario?'点步骤直达对应面板 · ✕恢复默认建议':'按当前进度动态推荐';
  box.innerHTML=steps.map(s=>s[1]==='__exit__'
    ? '<button class="btn ghost mini" onclick="S.scenario=null;renderNextSteps()">'+s[0]+'</button>'
    : '<button class="btn mini" onclick="goAnchor(\''+s[1]+'\','+(s[2]?('\''+s[2]+'\''):'null')+')">'+s[0]+'</button>').join('');
}

// ---------- B. 「?何时用」三行说明(默认收起, 点开才占地方) ----------
const HOWTO={
 '镜像任务':['怀疑盘要坏/有坏区，或想让后续操作绝对安全时。先复制一份，一切恢复都在镜像上做。',
   '首页选设备。读不稳或SMART有病灶会自动选中校验式。',
   '坏区=0最好；有坏区说明对应文件可能不完整，可勾「只重试坏区」再跑一遍。'],
 '兜底 · 签名雕刻':['文件系统已烂(列不出文件)，或只想找特定类型(照片/视频/文档)时。按文件头尾特征直接捞，捞到的没有原文件名。',
   '选好数据源(镜像或分区)。勾「仅未分配空间」更快更准。',
   '结果按类型落盘、hash去重；文件名是编号——需要人工翻看认领。'],
 '兜底 · 监控裸流雕刻':['监控盘/行车记录仪的录像没有容器(或容器碎了)时，直接扫H.264/H.265裸码流。',
   '选好数据源。分辨率/帧率会从码流里解析出来。',
   '每个结果两份：裸流原件(保底)+重封装MP4(方便播放)。MP4播放不完美以裸流为准。'],
 '深度 · Mac盘 HFS+':['Mac电脑拆下来的盘(HFS+文件系统)，要按原文件名原路径恢复。',
   '选好数据源，HFS+面板自动定位分区。',
   '正常列出「文件夹N/文件N」即可提取；APFS(新款Mac)目前仅识别、不能恢复。'],
 '深度 · exFAT已删挖掘':['U盘/SD卡(exFAT)删掉的文件、删掉的整个文件夹，普通列文件看不到时。',
   '选好数据源，自动定位exFAT分区。',
   '「已删」=删过还留痕；「孤儿」=整目录删除后仍完整躺着(常能整目录找回)。双校验不过的会如实标注。'],
 '深度 · ext4日志挖掘':['Linux/NAS(ext4)盘删了文件、或盘掉过电，想找删除前的名字和数据。',
   '选好数据源，自动定位ext分区。',
   '「崩溃尾巴N笔」=掉电现场证据；标「已删+日志副本」的名字最有希望，点恢复按inode捞回。'],
 '虚拟磁盘 · VMDK/VHD/VHDX':['数据在虚拟机硬盘文件里(VirtualBox/VMware/Hyper-V)，不开机直接恢复。',
   '填虚拟盘文件路径→展开→自动设为当前镜像，之后照普通盘流程走。',
   '动态盘展开会占等量磁盘空间；fixed/flat格式免展开直接用。'],
 'RAID重组 · NAS/服务器阵列':['NAS或服务器多块盘组了阵列，单块盘读不出数据，需要拼回虚拟盘。',
   '每行填一块成员盘的镜像路径(缺的盘留空行也能试RAID5降级重建)。',
   '拼对了会自动识别出分区/文件系统；「其他候选」存在且拼得不对时，点候选重拼。'],
 '修复模式':['相机/手机/行车记录仪录到一半断电，MP4有大小但打不开。',
   '必须有一段同设备的完好视频当参考(编码参数一致)。',
   '输出 *_fixed.mp4；参考视频不匹配会失败，换一段同设备拍的重试。'],
 '📹 海康 .dav 剥离':['海康DVR/NVR导出的.dav(或.mpg)录像，普通播放器打不开。',
   '填单个.dav文件路径，输出目录自动生成。',
   '产出裸流(.h264/.h265)+同名MP4；分辨率/帧率是从码流读出的真实值。'],
 '📸 照片整理 · EXIF':['一堆恢复出来的照片没时间没名字，想按拍摄时间整理归档。',
   '填已恢复文件的目录。',
   '含EXIF的照片按「年/月」归档、拍摄时间重命名——EXIF本身也是真伪佐证。'],
 '💾 SQLite残留':['恢复聊天记录/浏览器历史等SQLite库，删掉的内容还躺在库的空闲页里。',
   '填已恢复文件的目录(自动找.db)，或对镜像整段扫描。',
   '「自由页N」=库里有已删数据可挖，片段预览直接可读；加密库(新版微信)如实标注。'],
 '🍞 Base64内嵌':['文本/日志/导出文件里以Base64编码嵌着的图片、压缩包。',
   '填已恢复文件的目录。',
   '命中的按真实类型落盘，多层嵌套也能剥。'],
 '📋 事件日志 evtx':['取证：什么时候登录过、插过什么U盘、装过什么服务(勒索常用7045)、日志有没有被清除。',
   '先把 C:\\Windows\\System32\\winevt\\Logs 里的.evtx拷出来，填该目录。',
   '「高价值事件时间线」按时间倒序点名关键事件；彩色标签=风险等级。'],
 '🦠 勒索现场检测':['文件全变乱码/扩展名被改/有勒索信，先判断家族、查有没有免费解密器，别急着花钱。',
   '填受害文件所在目录。',
   '「可疑度+家族+免费解密器线索」；有免费解密器的(GandCrab等)先试官方解密器；重装前先检测留证。'],
 '📧 PST/OST 检查':['Outlook邮件库文件打不开/报错，或想确认结构是否完好。',
   '填单个.pst/.ost文件路径。',
   '结构健康=可直接导入；头损时自动兜底扫描直接捞邮件内容。'],
 '🧩 CHK文件识别':['CHKDSK把丢的簇存成了FOUND.000里的FILE0000.CHK，不知道里面是什么。',
   '填FOUND.00X目录。',
   '按文件魔数识别真身并拆分提取(JPG/ZIP/PDF/MP4…)；识别不出的如实说。'],
 '🔐 加密检测/解锁':['盘/分区不确定是否被加密，或明确是BitLocker且有密码/恢复口令。',
   '选好数据源；解锁需密码或48位恢复口令(横杠可有可无)。',
   '解锁成功生成解密后的raw并自动设为当前镜像，之后照普通盘走。'],
};
function injectHowtos(){
  document.querySelectorAll('.panel .panel-head .micro').forEach(m=>{
    const key=m.textContent.trim();
    const h=HOWTO[key];
    if(!h)return;
    const head=m.parentElement;
    if(head.querySelector('.howto-btn'))return;
    const btn=document.createElement('button');
    btn.className='btn ghost mini howto-btn'; btn.textContent='? 何时用'; btn.title='何时用/怎么准备/怎么看结果';
    btn.onclick=()=>{ const d=head.parentElement.querySelector('.howto-box'); if(d)d.classList.toggle('hidden'); };
    head.appendChild(btn);
    const d=document.createElement('div');
    d.className='tips howto-box hidden';
    d.innerHTML='<div><b>何时用</b>：'+h[0]+'</div>'+
      (h[1]?'<div style="margin-top:4px"><b>怎么准备</b>：'+h[1]+'</div>':'')+
      (h[2]?'<div style="margin-top:4px"><b>怎么看结果</b>：'+h[2]+'</div>':'');
    head.insertAdjacentElement('afterend', d);
  });
}

(function fillAboutVer(){
  const el=document.getElementById('aboutVer');
  if(!el)return;
  api('/api/version').then(v=>{ if(v&&v.ok)el.textContent='版本 v'+v.version+' · 构建 '+v.build; })
    .catch(()=>{ el.textContent=''; });
})();

loadHistory();
loadDevices();
renderNextSteps();
renderScenarios();
injectHowtos();
