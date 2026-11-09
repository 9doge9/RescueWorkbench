/* 虚拟滚动(文件表格) — 只渲染可见区±15行 */
var VIRT_ROW_H = 34;
var VIRT_BUFFER = 15;
var _virtAll = [];
var _virtStart = 0, _virtEnd = 0;

function renderTableVirtual(){
  const wrap = document.querySelector('.tbl-wrap');
  if (!wrap || !_virtAll.length) return;
  const total = _virtAll.length;
  const scrollTop = wrap.scrollTop;
  const viewH = wrap.clientHeight || 400;
  const first = Math.max(0, Math.floor(scrollTop / VIRT_ROW_H) - VIRT_BUFFER);
  const last = Math.min(total, Math.ceil((scrollTop + viewH) / VIRT_ROW_H) + VIRT_BUFFER);
  if (first === _virtStart && last === _virtEnd) return;
  _virtStart = first; _virtEnd = last;
  const visible = _virtAll.slice(first, last);
  const recName = {full:'完整', partial:'部分', none:'无数据'};
  const recCls = {full:'rec-full', partial:'rec-partial', none:'rec-none'};
  const arrow = k => k===sortKey ? (sortAsc?' ▲':' ▼') : '';
  const sortable = (key, label) =>
    `<th style="cursor:pointer;user-select:none" onclick="sortBy('${key}')">${label}${arrow(key)}</th>`;
  let html = `<tr><th style="width:28px"><input type="checkbox" onchange="toggleAll(this.checked)"></th>` +
    `${sortable('name','名称')}${sortable('time','修改时间 (UTC)')}${sortable('size','大小')}<th>状态</th>${sortable('rec','可恢复性')}</tr>`;
  if (first > 0) html += `<tr style="height:${first * VIRT_ROW_H}px"><td colspan="6"></td></tr>`;
  for (const f of visible){
    const orphan = !f.is_dir && f.parent_id != null && !S.byId[f.parent_id];
    const img = !f.is_dir && isImg(f.name);
    const nm = f.is_dir
      ? `<span class="fname-dir" onclick="navTo(${f.id})">📁 ${esc(f.name)}</span>`
      : img
        ? `<span class="fimg" onclick="showLightbox('${previewUrl(f)}','${esc(f.name)}',${f.size||0})">` +
          `<img class="thumb" loading="lazy" src="${previewUrl(f)}" onerror="this.parentElement.classList.add('imgfail');this.remove()">` +
          `<span class="fn-txt">${esc(f.name)}</span></span>`
        : `📄 ${esc(f.name)}`;
    html += `<tr style="height:${VIRT_ROW_H}px">` +
      `<td><input type="checkbox" class="fck" data-id="${f.id}" ${S.sel.has(f.id)?'checked':''} onchange="selOne(${f.id},this.checked)"></td>` +
      `<td>${nm}</td><td class="ttime">${fmtTime(f.modified)}</td>` +
      `<td class="tsize">${f.is_dir?'文件夹':fmtSize(f.size)}</td>` +
      `<td>${f.deleted?'<span class="pill st-del">已删</span>':''}${orphan?'<span class="pill orphan">孤立</span>':''}</td>` +
      `<td>${f.is_dir?'':`<span class="pill ${recCls[f.recoverable]||'rec-none'}">${recName[f.recoverable]||esc(f.recoverable)||'?'}</span>`}</td></tr>`;
  }
  if (last < total) html += `<tr style="height:${(total - last) * VIRT_ROW_H}px"><td colspan="6"></td></tr>`;
  document.getElementById('fileTable').innerHTML = html;
  updateSelInfo();
}

// 滚动事件(rAF节流)
var _virtTick = false;
document.addEventListener('DOMContentLoaded', () => {
  const wrap = document.querySelector('.tbl-wrap');
  if (wrap){
    wrap.addEventListener('scroll', () => {
      if (_virtTick) return;
      _virtTick = true;
      requestAnimationFrame(() => { renderTableVirtual(); _virtTick = false; });
    }, {passive: true});
  }
});
