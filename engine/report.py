# -*- coding: utf-8 -*-
"""一键HTML恢复报告: 体检+SMART+镜像+速度+分区+文件树+提取 全链证据单文件
数据来源: jobstore里的任务记录 + 可选的磁盘身份信息
"""
import os
import time
import html

CSS = """
body{font-family:"Segoe UI","Microsoft YaHei",sans-serif;background:#0d1117;color:#e6edf3;margin:0;padding:32px;line-height:1.7}
h1{font-size:26px;color:#2dd4bf;border-bottom:2px solid #2dd4bf;padding-bottom:10px}
h2{font-size:18px;color:#58a6ff;margin-top:32px}
.meta{color:#8b949e;font-size:13px}
table{border-collapse:collapse;width:100%;margin:12px 0;font-size:13px}
th{background:#161b22;color:#8b949e;text-align:left;padding:8px 12px;border:1px solid #2a3038}
td{padding:6px 12px;border:1px solid #2a3038}
.ok{color:#34d399;font-weight:600}
.bad{color:#f85149;font-weight:600}
.warn{color:#f59e0b;font-weight:600}
.bar{height:10px;background:#0e1319;border:1px solid #2a3038;border-radius:5px;overflow:hidden;display:inline-block;width:200px;vertical-align:middle}
.bar>span{display:block;height:100%;background:#2dd4bf}
.box{background:#161b22;border:1px solid #2a3038;border-radius:8px;padding:16px 20px;margin:14px 0}
.hash{font-family:monospace;font-size:11px;color:#8b949e;word-break:break-all}
"""


def esc(s):
    return html.escape(str(s if s is not None else ''))


def _kv(items):
    """[(k, v, class?)] → 单行小结: 标签 值"""
    return ' · '.join('%s <b class="%s">%s</b>' % (esc(k), c or '', esc(v))
                      for k, v, c in items if v is not None)


def _gb(n):
    return '%.1f GB' % (n / 1024 ** 3) if n else '-'


def _deep_channels(info):
    out = []
    # 勒索检测 — 置顶红框
    rs = info.get('ransomscan')
    if rs:
        cls = 'bad' if rs.get('suspicion') == 'high' else ('warn' if rs.get('suspicion') == 'medium' else 'ok')
        out.append('<h2>勒索现场检测</h2><div class="box"><span class="%s"><b>可疑度: %s</b></span>%s'
                   '<div class="meta">%s</div><div>%s</div></div>' %
                   (cls, esc(rs.get('suspicion', '?')),
                    (' · 疑似家族 <b>%s</b>' % esc(rs['family'])) if rs.get('family') else '',
                    '；'.join(esc(e) for e in (rs.get('evidence') or [])[:4]),
                    '<br>'.join('▸ ' + esc(a) for a in (rs.get('advice') or [])[:4])))
    mft = info.get('mftscan')
    if mft:
        out.append('<h2>NTFS纯MFT兜底</h2><div class="box">%s</div>' % _kv([
            ('记录', mft.get('records'), ''), ('文件', mft.get('files_total'), ''),
            ('已删', mft.get('deleted'), 'warn' if mft.get('deleted') else ''),
            ('模式', mft.get('mode'), '')]))
    cv = info.get('carve')
    if cv:
        out.append('<h2>签名雕刻</h2><div class="box">%s%s</div>' % (
            _kv([('恢复文件', cv.get('files_found'), 'ok'),
                 ('扫描范围', _gb(cv.get('bytes_scanned')) if cv.get('bytes_scanned') else None, '')]),
            (' · 未分配区间%d段' % cv['ranges']) if cv.get('ranges') else ''))
    h264 = info.get('h264carve')
    if h264:
        out.append('<h2>监控裸流雕刻</h2><div class="box">%s</div>' % _kv([
            ('段数', h264.get('found'), 'ok'), ('总量', _gb(h264.get('bytes')), ''),
            ('输出', h264.get('out'), '')]))
    xd = info.get('exfatdeep')
    if xd:
        out.append('<h2>exFAT深度挖掘</h2><div class="box">%s</div>' % _kv([
            ('已删条目', xd.get('deleted'), 'ok'), ('孤儿目录', xd.get('orphans'), 'ok'),
            ('落盘文件', xd.get('saved'), ''), ('扫描簇', xd.get('clusters_scanned'), '')]))
    e4 = info.get('ext4journal')
    if e4:
        tail = ' · <span class="warn">崩溃尾巴%d笔未提交</span>' % e4['uncommitted'] if e4.get('uncommitted') else ''
        out.append('<h2>ext4日志挖掘</h2><div class="box">卷 <b>%s</b> · 完整事务%s%s<br>%s</div>' %
                   (esc(e4.get('volume_name', '?')), e4.get('transactions', '?'), tail,
                    _kv([('挖到名字', e4.get('names'), ''), ('按inode恢复', e4.get('recovered'), 'ok')])))
    bl = info.get('blunlock')
    if bl:
        out.append('<h2>BitLocker解锁</h2><div class="box">%s<br>解密镜像: <span class="hash">%s</span></div>' % (
            _kv([('方式', bl.get('via'), 'ok'), ('算法', bl.get('algo'), ''),
                 ('卷大小', _gb(bl.get('volume_size')), '')]), esc(bl.get('raw'))))
    rd = info.get('raidrebuild')
    if rd:
        amb = ' · <span class="warn">配置存疑(多候选)</span>' if rd.get('ambiguous') else ''
        out.append('<h2>RAID重组</h2><div class="box">RAID%s · 条带%sKB%s · 虚拟盘%s<br>输出: <span class="hash">%s</span></div>' %
                   (rd.get('level'), rd.get('stripe_kb'), amb, _gb(rd.get('virtual_size')), esc(rd.get('raw'))))
    hf = info.get('hfslist')
    if hf:
        out.append('<h2>Mac HFS+</h2><div class="box">%s</div>' % _kv([
            ('文件', hf.get('count'), 'ok'), ('文件夹', hf.get('folders'), ''),
            ('已提取', hf.get('saved'), '')]))
    pt = info.get('pstcheck')
    if pt:
        rec = _kv([('兜底恢复邮件', pt.get('recovered_messages'), 'ok'),
                   ('恢复页', pt.get('recovered_pages'), '')]) if pt.get('recovered_messages') else ''
        out.append('<h2>Outlook PST/OST</h2><div class="box">格式: %s · %s%s<br><span class="meta">%s</span></div>' %
                   (esc(pt.get('index_type', '?')),
                    '<span class="ok">结构健康</span>' if pt.get('healthy') else '<span class="bad">结构损坏</span>',
                    rec, esc(pt.get('detail', ''))))
    vm = info.get('vmexpand')
    if vm:
        out.append('<h2>虚拟磁盘展开</h2><div class="box">%s → <span class="hash">%s</span></div>' %
                   (_kv([('格式', vm.get('kind'), ''), ('虚拟容量', _gb(vm.get('virtual_size')), '')]),
                    esc(vm.get('raw') or vm.get('usable_direct'))))
    return ''.join(out)


def _content_channels(info):
    out = []
    ex = info.get('exifscan')
    if ex:
        out.append('<h2>照片EXIF</h2><div class="box">%s</div>' % _kv([
            ('照片', ex.get('photos'), ''), ('含EXIF', ex.get('with_exif'), ''),
            ('按时间归档', ex.get('organized'), 'ok')]))
    sq = info.get('sqlscan')
    if sq:
        out.append('<h2>SQLite残留</h2><div class="box">%s</div>' % _kv([
            ('扫描文件', sq.get('scanned'), ''), ('发现库', sq.get('dbs'), ''),
            ('恢复库', sq.get('carved'), '')]))
    b6 = info.get('b64scan')
    if b6:
        out.append('<h2>Base64内嵌</h2><div class="box">%s</div>' % _kv([
            ('含内嵌文件', b6.get('files'), ''), ('提取', b6.get('found'), 'ok')]))
    ck = info.get('chkscan')
    if ck:
        out.append('<h2>CHK识别</h2><div class="box">%s</div>' % _kv([
            ('CHK文件', ck.get('total'), ''), ('识别出内容', ck.get('identified'), 'ok')]))
    ev = info.get('evlogscan')
    if ev:
        rows = []
        for l in (ev.get('logs') or [])[:6]:
            chips = ' · '.join('%s×%s' % (esc(e.get('name') or ('事件%s' % e.get('id'))), e.get('count'))
                               for e in (l.get('events') or [])[:5])
            rows.append('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' %
                        (esc(l.get('file')), l.get('records'),
                         (str(l.get('first'))[:19] if l.get('first') else '?') + ' → ' +
                         (str(l.get('last'))[:19] if l.get('last') else '?'), chips))
        out.append('<h2>事件日志取证</h2><table><tr><th>日志</th><th>记录</th><th>时间跨度</th><th>高频事件</th></tr>'
                   + ''.join(rows) + '</table>')
    mp = info.get('mp4repair')
    if mp:
        out.append('<h2>MP4修复</h2><div class="box">%s</div>' % _kv([
            ('修复成功', mp.get('ok'), 'ok'), ('总数', mp.get('total'), '')]))
    return ''.join(out)


def generate(outpath, info):
    """info keys: dev_model, dev_path, dev_size, serial, probe, smart,
    image(result dict), scan(partitions), files(result), extract(result),
    speed_bins(optional), wxdat(result, optional), notes
    ── 深度通道(可选, 缺省不渲染): mftscan, carve, h264carve, exfatdeep, ext4journal,
    blunlock, raidrebuild, hfslist, pstcheck, ransomscan, vmexpand,
    exifscan, sqlscan, b64scan, chkscan, evlogscan, mp4repair"""
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    try:
        import version
        vtag = 'v%s (构建 %s)' % (version.VERSION, version.build_time())
    except ImportError:
        vtag = ''
    rows = []
    rows.append('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">')
    rows.append('<title>数据抢救报告 %s</title><style>%s</style></head><body>' % (ts, CSS))
    rows.append('<h1>🛟 数据抢救报告</h1>')
    rows.append('<p class="meta">生成时间: %s · 工具: 数据抢救工作台 %s (sf-cli引擎+校验式镜像器)</p>' % (ts, vtag))

    # ── 设备 ──
    rows.append('<h2>磁盘信息</h2><table>')
    rows.append('<tr><th style="width:140px">型号</th><td>%s</td></tr>' % esc(info.get('dev_model', '未知')))
    rows.append('<tr><th>设备路径</th><td class="hash">%s</td></tr>' % esc(info.get('dev_path', '-')))
    rows.append('<tr><th>序列号</th><td class="hash">%s</td></tr>' % esc(info.get('serial', '-')))
    sz = info.get('dev_size')
    if sz:
        rows.append('<tr><th>容量</th><td>%.1f GB (%d 字节)</td></tr>' % (sz / 1024**3, sz))
    rows.append('</table>')

    # ── SMART ──
    sm = info.get('smart') or {}
    rows.append('<h2>SMART 健康</h2><div class="box">')
    if sm.get('ok'):
        cls = 'bad' if sm.get('warnings') else 'ok'
        txt = '；'.join(sm['warnings']) if sm.get('warnings') else sm.get('health', '?')
        rows.append('<span class="%s">%s</span>' % (cls, esc(txt)))
        if sm.get('temp'):
            rows.append(' · %d°C' % sm['temp'])
        if sm.get('attrs'):
            rows.append('<table><tr><th>ID</th><th>属性</th><th>值</th><th>阈值</th><th>原始</th></tr>')
            for a in sm['attrs'][:12]:
                rows.append('<tr><td>%d</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' %
                            (a.get('id', 0), esc(a.get('name', '')), a.get('value', '-'), a.get('thresh', '-'), esc(a.get('raw', ''))))
            rows.append('</table>')
    else:
        rows.append('<span class="meta">%s</span>' % esc(sm.get('reason', '不可读')))
    rows.append('</div>')

    # ── 体检 ──
    pr = info.get('probe') or {}
    rows.append('<h2>读稳定性体检</h2><div class="box">')
    if pr:
        stable = pr.get('stable')
        cls = 'ok' if stable else 'bad'
        verdict = '健康 — 读取稳定' if stable else '读数不稳'
        if pr.get('stream_drift'):
            verdict += ' · 检测到顺序流漂移（主控说谎）'
        if pr.get('repeat_unstable'):
            verdict += ' · %d/%d采样点异常' % (pr['repeat_unstable'], pr.get('repeat_points', '?'))
        rows.append('<span class="%s">%s</span>' % (cls, esc(verdict)))
        rows.append('<br><span class="meta">建议策略: %s</span>' % esc(pr.get('recommend', '-')))
    else:
        rows.append('<span class="meta">未做体检</span>')
    rows.append('</div>')

    # ── 镜像 ──
    im = info.get('image') or {}
    rows.append('<h2>整盘镜像</h2><div class="box">')
    if im:
        mode = '校验式（双句柄+二分坏区）' if im.get('verified') else '快速（ddrescue两遍）'
        fin = im.get('finished_bytes', 0)
        bad = im.get('bad_bytes', 0) or 0
        tot = im.get('total', 0) or fin + bad
        pct = fin * 100 // tot if tot else 0
        rows.append('<div>模式: <b>%s</b></div>' % esc(mode))
        rows.append('<div class="bar"><span style="width:%d%%"></span></div> %d%%<br>' % (pct, pct))
        rows.append('完成 <b>%s</b> / %s' % ('{:,}'.format(fin), '{:,}'.format(tot)))
        cls = 'bad' if bad else 'ok'
        rows.append(' · 坏区 <span class="%s">%s</span>' % (cls, '{:,}'.format(bad)))
        rows.append(' · 重读%d · IO重试%d' % (im.get('rereads', 0), im.get('io_retries', 0)))
        if im.get('sha256'):
            rows.append('<br>镜像SHA-256: <span class="hash">%s</span>' % esc(im['sha256']))
    else:
        rows.append('<span class="meta">未做镜像</span>')
    rows.append('</div>')

    # ── 分区 ──
    sc = info.get('scan') or []
    rows.append('<h2>分区扫描</h2>')
    if sc:
        rows.append('<table><tr><th>文件系统</th><th>置信度</th><th>起始LBA</th><th>大小</th></tr>')
        for p in sc:
            rows.append('<tr><td>%s</td><td>%s</td><td>%s</td><td>%.1f GB</td></tr>' %
                        (esc(p.get('filesystem', '?')), esc(p.get('confidence', '?')),
                         p.get('start_lba', '-'), (p.get('size_sectors') or 0) * 512 / 1024**3))
        rows.append('</table>')
    else:
        rows.append('<p class="meta">未扫描</p>')

    # ── 文件列表摘要 ──
    fl = info.get('files') or {}
    rows.append('<h2>文件列表</h2><div class="box">')
    if fl:
        rows.append('共 <b>%s</b> 项 · 已删 <b>%s</b> 项' % (fl.get('files_total', '?'), fl.get('deleted', '?')))
    else:
        rows.append('<span class="meta">未列出</span>')
    rows.append('</div>')

    # ── 提取 ──
    ex = info.get('extract') or {}
    rows.append('<h2>提取结果</h2><div class="box">')
    if ex:
        rows.append('成功 <b class="ok">%s</b> · 失败 <b class="bad">%s</b>' % (ex.get('extracted', '?'), ex.get('failed', '?')))
        if ex.get('out'):
            rows.append('<br>输出: <span class="hash">%s</span>' % esc(ex['out']))
    else:
        rows.append('<span class="meta">未提取</span>')
    rows.append('</div>')

    # ── 微信dat ──
    wx = info.get('wxdat')
    if wx:
        rows.append('<h2>微信dat解密</h2><div class="box">成功 <b class="ok">%s</b> · 跳过 %s · 失败 %s</div>' %
                    (wx.get('ok', '?'), wx.get('skip', '?'), wx.get('fail', '?')))

    # ── 深度恢复通道(Tier3/4) ──
    rows.append(_deep_channels(info))

    # ── 内容取证 ──
    rows.append(_content_channels(info))

    # ── 速度热力图(文字版) ──
    sb = info.get('speed_bins') or []
    if len(sb) > 5:
        mbps = sorted(s for _, s in sb)
        rows.append('<h2>读写速度分布</h2><div class="box">')
        rows.append('最快 %.0f MB/s · 中位 %.0f MB/s · 最慢 %.1f MB/s（%d块）' %
                    (mbps[-1], mbps[len(mbps)//2], mbps[0], len(sb)))
        slow = sorted(sb, key=lambda x: x[1])[:3]
        rows.append('<br><span class="meta">最慢区域: %s</span>' %
                    ' · '.join('@%.0fMB %.1fMB/s' % (p / 1024**2, s) for p, s in slow))
        rows.append('</div>')

    # ── 结论 ──
    rows.append('<h2>结论</h2><div class="box">')
    notes = info.get('notes', '')
    if not notes:
        im_ok = im and im.get('bad_bytes', 1) == 0
        ex_ok = ex and (ex.get('failed', 1) or 0) == 0
        if im_ok and ex_ok:
            notes = '镜像完整（零坏区），提取全部成功。建议对恢复文件做二次备份。'
        elif im and im.get('bad_bytes'):
            notes = '镜像有坏区（%s字节），对应文件可能不完整。建议用校验式重试坏区段。' % '{:,}'.format(im['bad_bytes'])
        else:
            notes = '操作部分完成，见上方各项详情。'
    rows.append(esc(notes))
    rows.append('</div>')

    rows.append('<p class="meta" style="margin-top:40px;border-top:1px solid #2a3038;padding-top:12px">'
                '本报告由数据抢救工作台自动生成 · 全程只读源盘 · ddrescue兼容mapfile留档</p>')
    rows.append('</body></html>')

    html_text = '\n'.join(rows)
    with open(outpath, 'w', encoding='utf-8') as f:
        f.write(html_text)
    return outpath
