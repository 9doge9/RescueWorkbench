# -*- coding: utf-8 -*-
"""策略中枢: 串联 探测→镜像→分区→文件恢复/雕刻→后处理
每个任务类型对应一个执行器; 统一通过 emit(job, event_dict) 上报进度。
"""
import os
import sys
import time
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sfadapter
import stability
import jobstore
from imager import VerifiedImager


class EventBus:
    """每个任务的进度事件总线: 队列监听 + 最近事件环形缓冲(SSE重连重放)"""

    def __init__(self, job):
        self.job = job
        self.lock = threading.Lock()
        self.listeners = []
        self.recent = []       # 最近64条, 供SSE重连补发

    def emit(self, ev):
        ev = dict(ev)
        ev['ts'] = time.time()
        with self.lock:
            self.recent.append(ev)
            if len(self.recent) > 64:
                del self.recent[:-64]
            for q in list(self.listeners):
                try:
                    q.put_nowait(ev)
                except Exception:
                    pass
            job = self.job
            if ev.get('event') in ('progress', 'scan_progress', 'carve_progress'):
                job['last_event'] = ev
            elif ev.get('event') == 'done':
                job['state'], job['result'] = 'done', ev
            elif ev.get('event') == 'cancelled':
                job['state'] = 'cancelled'
            elif ev.get('event') in ('bad_region', 'io_retry', 'error'):
                job.setdefault('warnings', []).append(ev)
            jobstore.save(job)

    def attach(self, q):
        with self.lock:
            for ev in self.recent[-32:]:
                try:
                    q.put_nowait(ev)
                except Exception:
                    pass
            self.listeners.append(q)

    def detach(self, q):
        with self.lock:
            if q in self.listeners:
                self.listeners.remove(q)


BUSSES = {}
RUNNERS = {}
CANCEL = {}


def runner(kind):
    def deco(fn):
        RUNNERS[kind] = fn
        return fn
    return deco


def start_job(kind, params):
    job = jobstore.new_job(kind, params)
    job['state'] = 'running'
    jobstore.save(job)
    bus = EventBus(job)
    BUSSES[job['id']] = bus
    cancel_evt = threading.Event()
    CANCEL[job['id']] = cancel_evt

    def work():
        try:
            fn = RUNNERS.get(kind)
            if not fn:
                raise ValueError('未知任务类型: ' + kind)
            fn(bus, cancel_evt, params)
            if job['state'] == 'running':
                job['state'] = 'done'
                jobstore.save(job)
        except Exception as e:
            import traceback
            job['state'] = 'failed'
            job['error'] = str(e) + '\n' + traceback.format_exc()
            jobstore.save(job)
            bus.emit({'event': 'error', 'message': str(e)})
        finally:
            bus.emit({'event': '_end'})   # 通知SSE流关闭
            CANCEL.pop(job['id'], None)
            BUSSES.pop(job['id'], None)

    threading.Thread(target=work, daemon=True).start()
    return job


# ---------- 任务执行器 ----------

def _src_size(source):
    if source.startswith('\\\\.\\'):
        ev, _ = sfadapter.devices()
        for d in ev.get('devices', []):
            if d['path'].lower() == source.lower():
                return d['size']
        raise ValueError('设备不在线: ' + source)
    return os.path.getsize(source)


@runner('probe')
def _probe(bus, cancel, p):
    src = p['source']
    size = _src_size(src)
    bus.emit({'event': 'phase', 'message': '定点重复读探测(弱单元检测)...'})
    f = stability.open_source(src)
    bad, total, details = stability.probe_repeat(f, size)
    f.close()
    if cancel.is_set():
        return
    bus.emit({'event': 'phase', 'message': '流式vs定点比对(主控漂移检测)...'})
    f = stability.open_source(src)
    drift, drift_detail = stability.probe_stream_vs_seek(f, size)
    f.close()
    verdict = {
        'event': 'done',
        'repeat_unstable': bad, 'repeat_points': total,
        'stream_drift': drift,
        'stable': bad == 0 and drift is False,
        'recommend': 'verified_imager' if (drift or bad > 0) else 'fast_imager',
        'details': {'repeat': details, 'stream': drift_detail},
    }
    bus.emit(verdict)


@runner('image')
def _image(bus, cancel, p):
    """p: source, dest, mapfile, mode(fast|verified)"""
    mode = p.get('mode', 'fast')
    if mode == 'verified':
        if not p.get('size'):
            p['size'] = _src_size(p['source'])
        img = VerifiedImager(p['source'], p['dest'], total_size=p.get('size'),
                             on_progress=lambda d: bus.emit(d))

        def watch():
            cancel.wait()
            img.cancel()
        threading.Thread(target=watch, daemon=True).start()
        img.run()
        return
    # fast模式: sf-cli image (两遍ddrescue式), 契约v2字段
    def on_ev(ev):
        e = ev.get('event')
        if e == 'progress':
            total = ev.get('bytes_total') or 0
            done = ev.get('bytes_rescued') or 0
            bus.emit({'event': 'progress',
                      'offset': done, 'total': total,
                      'pct': round(done * 100.0 / total, 1) if total else None,
                      'pass': ev.get('pass'), 'bad_bytes': ev.get('bytes_bad'),
                      'speed_mbps': None, 'eta_min': None})
        else:
            bus.emit(ev)
    ev, rc, killed = sfadapter.run_killable(
        ['image', '--source', p['source'], '--dest', p['dest'],
         '--map', p.get('mapfile', p['dest'] + '.map')] +
        (['--access', p['access']] if p.get('access') else []) +
        (['--reverse'] if p.get('reverse') else []) +
        (['--retry-only'] if p.get('retry_only') else []),
        cancel, on_event=on_ev)
    if killed:
        bus.emit({'event': 'cancelled'})
        return
    if rc != 0:
        raise RuntimeError('sf-cli image 失败 rc=%s: %s' % (rc, (ev or {}).get('message')))


@runner('scan')
def _scan(bus, cancel, p):
    ev, rc, killed = sfadapter.run_killable(
        ['scan', '--source', p['source']] + (['--deep'] if p.get('deep', True) else []),
        cancel, on_event=bus.emit)
    if killed:
        bus.emit({'event': 'cancelled'})
        return
    if rc != 0:
        raise RuntimeError('sf-cli scan 失败 rc=%s: %s' % (rc, (ev or {}).get('message')))
    bus.emit({'event': 'done', 'partitions': (ev or {}).get('partitions', []),
              'table_kind': (ev or {}).get('table_kind')})


@runner('files')
def _files(bus, cancel, p):
    """files按契约是流式file事件; 累积后随done打包(上限2万条防巨卷撑爆)"""
    collected = []

    def on_ev(ev):
        if ev.get('event') == 'file':
            if len(collected) < 20000:
                collected.append(ev)
            if len(collected) % 200 == 0:
                bus.emit({'event': 'files_so_far', 'count': len(collected)})
        else:
            bus.emit(ev)
    args = ['files', '--source', p['source'], '--partition-lba', p['partition_lba']]
    if p.get('deleted_only'):
        args += ['--deleted-only']
    # map缺失时静默跳过(源可能不是本工具镜像的, 没有map)
    if p.get('mapfile') and os.path.exists(p['mapfile']):
        args += ['--map', p['mapfile']]
    ev, rc, killed = sfadapter.run_killable(args, cancel, on_event=on_ev)
    if killed:
        bus.emit({'event': 'cancelled'})
        return
    if rc != 0:
        raise RuntimeError('sf-cli files 失败 rc=%s: %s' % (rc, (ev or {}).get('message')))
    bus.emit({'event': 'done', 'files': collected,
              'files_total': (ev or {}).get('files'), 'deleted': (ev or {}).get('deleted')})


@runner('extract')
def _extract(bus, cancel, p):
    args = ['extract', '--source', p['source'], '--partition-lba', p['partition_lba'],
            '--ids', p['ids'], '--out', p['out']]
    if p.get('mapfile') and os.path.exists(p['mapfile']):
        args += ['--map', p['mapfile']]
    ev, rc, killed = sfadapter.run_killable(args, cancel, on_event=bus.emit)
    if killed:
        bus.emit({'event': 'cancelled'})
        return
    if rc != 0:
        raise RuntimeError('sf-cli extract 失败 rc=%s: %s' % (rc, (ev or {}).get('message')))
    try:
        renamed = sfadapter.strip_orphan_suffix(p['out'])
        if renamed:
            bus.emit({'event': 'phase', 'message': '已剥离%d个NTFS孤儿后缀' % renamed})
    except OSError:
        pass
    bus.emit({'event': 'done', 'result': ev})


@runner('wxdat')
def _wxdat(bus, cancel, p):
    """微信图片dat批量解密: p={dir, out}"""
    import wxdat
    bus.emit({'event': 'phase', 'message': '扫描 .dat 文件中…'})
    res = wxdat.decrypt_dir(p['dir'], p['out'], on_progress=lambda e: bus.emit(e))
    bus.emit({'event': 'done', **res})


@runner('mftscan')
def _mftscan(bus, cancel, p):
    """纯MFT扫描: p={source} → $Boot损坏兜底, 返回文件树"""
    import mft_scan
    import struct
    bus.emit({'event': 'phase', 'message': '探测NTFS健康度…'})
    mode = mft_scan.probe_ntfs(p['source'])
    if mode == 'not_ntfs':
        bus.emit({'event': 'done', 'mode': mode, 'records': 0, 'files': []})
        return
    bus.emit({'event': 'phase', 'message': '扫描MFT记录…'})
    offset, records = mft_scan.scan_mft(p['source'], on_progress=lambda e: bus.emit(e))
    if not records:
        bus.emit({'event': 'done', 'mode': 'no_mft', 'records': 0, 'files': []})
        return
    tree, roots = mft_scan.build_tree(records)
    # 扁平化输出(兼容files格式)
    files = []
    def walk(nodes, path):
        for n in nodes:
            fp = path + '/' + n['name'] if path else n['name']
            files.append({
                'id': n['id'], 'parent_id': n['parent_id'],
                'name': n['name'], 'path': fp,
                'is_dir': n['is_dir'], 'size': n['size'],
                'deleted': n.get('deleted', False),
                'recoverable': 'none' if n.get('encrypted') else ('partial' if n.get('compressed') else 'full'),
            })
            if n.get('children'):
                walk(n['children'], fp)
    walk(roots, '')
    bus.emit({'event': 'done', 'mode': mode, 'records': len(records), 'files': files})


@runner('mp4repair')
def _mp4repair(bus, cancel, p):
    """MP4视频修复: p={reference, corrupt} 或 p={directory}(批量找损坏MP4)"""
    import mp4repair
    if not mp4repair.is_available():
        bus.emit({'event': 'error', 'message': 'untrunc.exe未找到'})
        return
    if p.get('directory'):
        bus.emit({'event': 'phase', 'message': '扫描损坏MP4…'})
        corrupts = mp4repair.find_corrupt_mp4s(p['directory'])
        bus.emit({'event': 'phase', 'message': '找到%d个疑似损坏MP4' % len(corrupts)})
        results = []
        for i, c in enumerate(corrupts):
            bus.emit({'event': 'progress', 'files_done': i, 'files_total': len(corrupts)})
            ok, msg = mp4repair.repair(p['reference'], c)
            results.append({'file': c, 'ok': ok, 'output': msg if ok else '', 'error': '' if ok else msg})
        ok_count = sum(1 for r in results if r['ok'])
        bus.emit({'event': 'done', 'results': results, 'ok': ok_count, 'total': len(corrupts)})
    elif p.get('reference') and p.get('corrupt'):
        bus.emit({'event': 'phase', 'message': '修复中…'})
        ok, msg = mp4repair.repair(p['reference'], p['corrupt'], p.get('output'))
        if ok:
            bus.emit({'event': 'done', 'results': [{'file': p['corrupt'], 'ok': True, 'output': msg}], 'ok': 1, 'total': 1})
        else:
            bus.emit({'event': 'done', 'results': [{'file': p['corrupt'], 'ok': False, 'error': msg}], 'ok': 0, 'total': 1})


@runner('h264carve')
def _h264carve(bus, cancel, p):
    """H.264/H.265裸流雕刻(监控场景): p={source, out, start?, end?, min_kb?}
    逐段: 裸流原件 + 重封装MP4(可封则封)"""
    import h264carve
    out = p['out']
    os.makedirs(out, exist_ok=True)
    min_bytes = max(1024, int(p.get('min_kb', 64)) * 1024)
    prog = {'found': 0, 'bytes': 0}

    def on_prog(ev):
        bus.emit(ev)

    with open(p['source'], 'rb') as f:
        for seg in h264carve.scan(p['source'], int(p.get('start') or 0),
                                  int(p.get('end') or 0) if p.get('end') else None,
                                  min_bytes=min_bytes,
                                  on_progress=on_prog, cancel=cancel):
            if cancel.is_set():
                bus.emit({'event': 'cancelled'})
                return
            raw_name, mp4_name = h264carve.seg_names(seg)
            raw = h264carve.read_segment(f, seg)
            with open(os.path.join(out, raw_name), 'wb') as w:
                w.write(raw)
            mp4 = None
            try:
                mp4 = h264carve.remux_mp4(seg)
            except Exception:
                mp4 = None
            if mp4:
                with open(os.path.join(out, mp4_name), 'wb') as w:
                    w.write(mp4)
            prog['found'] += 1
            prog['bytes'] += seg['size']
            fps_note = ('%.0f' % seg['fps']) if seg.get('fps') else '25(假定)'
            bus.emit({'event': 'found', 'name': raw_name,
                      'codec': seg['codec'],
                      'width': seg.get('width'), 'height': seg.get('height'),
                      'frames': seg['vcl_frames'], 'size': seg['size'],
                      'fps': fps_note, 'mp4': bool(mp4),
                      'offset': seg['start']})
    if cancel.is_set():
        bus.emit({'event': 'cancelled'})
        return
    bus.emit({'event': 'done', 'found': prog['found'],
              'bytes': prog['bytes'], 'out': out})


@runner('exifscan')
def _exifscan(bus, cancel, p):
    """照片EXIF: p={dir, out?, mode:'scan'|'organize'}"""
    import exif
    bus.emit({'event': 'phase', 'message': '扫描JPEG EXIF…'})
    results = exif.scan_dir(p['dir'], on_progress=lambda e: bus.emit(e))
    with_exif = sum(1 for r in results if r['exif'])
    org = None
    if p.get('mode') == 'organize' and p.get('out'):
        bus.emit({'event': 'phase', 'message': '按拍摄时间归档…'})
        org = exif.organize(results, p['out'], mode='copy',
                            on_progress=lambda e: bus.emit(e))
    summary = [{'file': r['file'], 'exif': r['exif']} for r in results[:5000]]
    bus.emit({'event': 'done', 'photos': len(results), 'with_exif': with_exif,
              'organized': (org or {}).get('organized', 0), 'out': p.get('out'),
              'results': summary})


@runner('b64scan')
def _b64scan(bus, cancel, p):
    """Base64内嵌提取: p={dir, out?}"""
    import b64
    bus.emit({'event': 'phase', 'message': '扫描Base64内嵌文件…'})
    res = b64.scan_dir(p['dir'], out_dir=p.get('out'), on_progress=lambda e: bus.emit(e))
    total = sum(len(r['hits']) for r in res)
    bus.emit({'event': 'done', 'files': res[:2000], 'found': total})


@runner('sqlscan')
def _sqlscan(bus, cancel, p):
    """SQLite残留: p={dir} → 库体检+自由页文本; p={source,start,end} → 镜像雕刻"""
    import sqlres
    if p.get('dir'):
        bus.emit({'event': 'phase', 'message': '扫描SQLite库…'})
        dbs = []
        n = 0
        for dirpath, _, files in os.walk(p['dir']):
            for fn in files:
                if cancel.is_set():
                    bus.emit({'event': 'cancelled'})
                    return
                fp = os.path.join(dirpath, fn)
                try:
                    if os.path.getsize(fp) < 512:
                        continue
                except OSError:
                    continue
                inv = sqlres.inventory(fp)
                n += 1
                if inv['is_sqlite'] or inv['encrypted']:
                    if inv['is_sqlite'] and inv['freelist_pages']:
                        bus.emit({'event': 'phase',
                                  'message': '%s: 挖自由页(%d页)…' % (fn, inv['freelist_pages'])})
                        inv['free_fragments'] = sqlres.free_pages_text(fp)[:500]
                    dbs.append(inv)
                if n % 20 == 0:
                    bus.emit({'event': 'progress', 'files': n})
        bus.emit({'event': 'done', 'scanned': n, 'dbs': dbs[:500]})
    elif p.get('source'):
        bus.emit({'event': 'phase', 'message': '镜像内雕刻SQLite库…'})
        found = sqlres.carve_from_image(p['source'], int(p.get('start') or 0),
                                        int(p.get('end') or 0) or None,
                                        on_progress=lambda e: bus.emit(e))
        saved = []
        out = p.get('out')
        if out:
            os.makedirs(out, exist_ok=True)
            with open(p['source'], 'rb') as f:
                for i, c in enumerate(found):
                    if cancel.is_set():
                        bus.emit({'event': 'cancelled'})
                        return
                    dest = os.path.join(out, 'sqlite_%012d.db' % c['offset'])
                    f.seek(c['offset'])
                    with open(dest, 'wb') as w:
                        w.write(f.read(c['est_size']))
                    saved.append(dest)
        bus.emit({'event': 'done', 'found': found, 'saved': saved})


@runner('evlogscan')
def _evlogscan(bus, cancel, p):
    """evtx解析: p={dir} → 每库记录数/时间线"""
    import evlog
    bus.emit({'event': 'phase', 'message': '扫描evtx…'})
    results = []
    for dirpath, _, files in os.walk(p['dir']):
        for fn in files:
            if not fn.lower().endswith('.evtx'):
                continue
            if cancel.is_set():
                bus.emit({'event': 'cancelled'})
                return
            fp = os.path.join(dirpath, fn)
            if not evlog.is_evtx(fp):
                continue
            r = evlog.parse(fp)
            r['file'] = fn
            r['first'] = r['first'].isoformat() if r.get('first') else None
            r['last'] = r['last'].isoformat() if r.get('last') else None
            r['timeline'] = [(dt.isoformat() if dt else '', rid, eid)
                             for dt, rid, eid in r['timeline'][:200]]
            r['notable'] = [(dt.isoformat() if dt else '', rid, eid,
                             evlog.EVENT_NAMES.get(eid, ('', ''))[0])
                            for dt, rid, eid in r['notable'][:200]]
            results.append(r)
            bus.emit({'event': 'progress', 'logs': len(results)})
    bus.emit({'event': 'done', 'logs': results})


@runner('aesdetect')
def _aesdetect(bus, cancel, p):
    """加密检测: p={source, offset?} → 卷头检测 + 高熵扫描"""
    import aesdet
    off = int(p.get('offset') or 0)
    vol = aesdet.detect_volume(p['source'], off)
    bus.emit({'event': 'phase', 'message': '卷头检测: %s' % vol.get('detail')})
    bus.emit({'event': 'volume', **vol})
    end = int(p.get('end') or 0) or None
    em = aesdet.entropy_map(p['source'], off, end, on_progress=lambda e: bus.emit(e))
    pct = round(em['high_bytes'] * 100 / max(em['total_bytes'], 1), 1)
    bus.emit({'event': 'done', 'volume': vol, 'high_pct': pct,
              'high_regions': em['high_regions'][:50], 'max_entropy': em['max']})


@runner('exfatdeep')
def _exfatdeep(bus, cancel, p):
    """exFAT深度挖掘: p={source, partition_lba, out?, extract?}
    全簇扫描已删条目集+孤儿目录 → 提取(默认只列, extract=true则全部落盘)"""
    import exfat_deep
    bus.emit({'event': 'phase', 'message': '全簇扫描exFAT目录项…'})
    res = exfat_deep.scan(p['source'], int(p.get('partition_lba') or 0),
                          on_progress=lambda e: bus.emit(e), cancel=cancel)
    if res.get('cancelled'):
        bus.emit({'event': 'cancelled'})
        return
    records = [r for r in res['records'] if not r['is_dir']][:2000]
    saved = []
    if p.get('extract') and p.get('out'):
        os.makedirs(p['out'], exist_ok=True)
        with open(p['source'], 'rb') as f:
            vol = exfat_deep.Vol(f, int(p.get('partition_lba') or 0))
            vol.fat_load()
            for i, r in enumerate(records):
                if cancel.is_set():
                    bus.emit({'event': 'cancelled'})
                    return
                try:
                    data, mode, trunc = exfat_deep.extract(vol, r)
                except OSError:
                    data, mode, trunc = b'', 'error', True
                safe = fsutil.safe_name(r['name'], 'unnamed')
                dest = os.path.join(p['out'], safe)
                k = 1
                while os.path.exists(dest):
                    dest = os.path.join(p['out'], '%s(%d)' % (safe, k))
                    k += 1
                with open(dest, 'wb') as w:
                    w.write(data)
                saved.append({'file': dest, 'size': len(data), 'mode': mode,
                              'truncated': trunc})
                r['extract_mode'] = mode
                r['extracted_size'] = len(data)
                bus.emit({'event': 'progress', 'extracted': i + 1,
                          'total': len(records)})
    n_del = sum(1 for r in records if r['deleted'])
    n_orph = len(records) - n_del
    bus.emit({'event': 'done', 'records': records, 'saved': saved,
              'deleted': n_del, 'orphans': n_orph,
              'clusters_scanned': res['clusters_scanned']})


@runner('vmexpand')
def _vmexpand(bus, cancel, p):
    """虚拟盘展开: p={source, out} dynamic VHD/sparse VMDK → raw; fixed/flat直接告知可用"""
    import vmdisk
    bus.emit({'event': 'phase', 'message': '识别虚拟磁盘格式…'})
    info = vmdisk.detect(p['source'])
    bus.emit({'event': 'detected', **{k: info.get(k) for k in
                                      ('kind', 'virtual_size', 'detail', 'needs_expand')}})
    if not info.get('kind'):
        raise RuntimeError('非VMDK/VHD/VHDX虚拟盘')
    if not info.get('needs_expand'):
        bus.emit({'event': 'done', 'kind': info['kind'], 'raw': None,
                  'virtual_size': info.get('virtual_size'),
                  'detail': info.get('detail'),
                  'usable_direct': p['source']})
        return
    os.makedirs(p['out'], exist_ok=True)
    raw = os.path.join(p['out'],
                       os.path.splitext(os.path.basename(p['source']))[0] + '.raw')
    bus.emit({'event': 'phase', 'message': '%s → 展开raw(洞=零, 稀疏写)…' % info['kind']})
    stat = vmdisk.expand(p['source'], raw, info,
                         on_progress=lambda e: bus.emit(e), cancel=cancel)
    if stat is None:
        bus.emit({'event': 'cancelled'})
        return
    bus.emit({'event': 'done', 'kind': info['kind'], 'raw': raw,
              'virtual_size': info.get('virtual_size'),
              'stat': stat, 'detail': '展开完成, 已可直接跑分区扫描'})


@runner('ext4journal')
def _ext4journal(bus, cancel, p):
    """ext4日志挖掘: p={source, partition_lba, out?, recover?}
    recover=[ino...] → 按inode提取(主表优先, journal副本兜底)"""
    import ext4
    res = ext4.mine_journal(p['source'], int(p.get('partition_lba') or 0),
                            on_progress=lambda e: bus.emit(e), cancel=cancel)
    if not res.get('ok'):
        raise RuntimeError(res.get('error', 'journal解析失败'))
    bus.emit({'event': 'phase', 'message': '卷 %s: %d笔完整事务 · %d笔崩溃尾巴未提交 · 挖到%d个名字'
              % (res['volume_name'], res['transactions'], res['uncommitted'],
                 len(res['names']))})
    recovered = []
    inos = p.get('recover') or []
    if inos == '*':
        inos = [n['journal_ino'] for n in res['names']
                if n.get('journal_ino') and n.get('main_inode') == '已删'
                and n.get('journal_copy')]
    if inos and p.get('out'):
        os.makedirs(p['out'], exist_ok=True)
        for ino in inos:
            if cancel.is_set():
                bus.emit({'event': 'cancelled'})
                return
            nm = next((n for n in res['names'] if n['journal_ino'] == ino), None)
            base = (nm['name'] if nm and nm.get('name') else 'inode_%s' % ino)
            safe = fsutil.safe_name(base)
            dest = os.path.join(p['out'], safe)
            k = 1
            while os.path.exists(dest):
                dest = os.path.join(p['out'], '%s(%d)' % (safe, k))
                k += 1
            try:
                got, want = ext4.recover_by_ino(p['source'],
                                                int(p.get('partition_lba') or 0), ino, dest)
                recovered.append({'ino': ino, 'file': dest, 'size': got, 'complete': got == want})
                bus.emit({'event': 'progress', 'recovered': len(recovered),
                          'total': len(inos)})
            except (ValueError, OSError) as e:
                recovered.append({'ino': ino, 'error': str(e)[:120]})
    bus.emit({'event': 'done', **{k: res[k] for k in
                                  ('volume_name', 'block_size', 'journal_blocks',
                                   'transactions', 'uncommitted', 'tx_detail')},
              'names': res['names'][:2000], 'recovered': recovered})


@runner('blunlock')
def _blunlock(bus, cancel, p):
    """BitLocker解锁: p={source, partition_lba?, password?, recovery?, out}
    解锁(密码/恢复口令/清除密钥) → AES-XTS整卷解密 → raw"""
    import bitlocker
    part = int(p.get('partition_lba') or 0)
    bus.emit({'event': 'phase', 'message': '解析FVE元数据…'})
    info = bitlocker.load(p['source'], part)
    if not info.get('ok'):
        raise RuntimeError(info.get('error', '非BitLocker卷'))
    bus.emit({'event': 'detected',
              'protectors': [x['kind'] for x in info['protectors']],
              'algorithm': info['algorithm'],
              'volume_size': info['volume_size']})
    bus.emit({'event': 'phase', 'message': '解锁(密钥拉伸需数秒)…'})
    r = bitlocker.unlock(p['source'], part,
                         password=p.get('password'), recovery=p.get('recovery'))
    if not r.get('ok'):
        raise RuntimeError(r.get('error', '解锁失败'))
    bus.emit({'event': 'unlocked', 'via': r['via'], 'algo': r['algo']})
    os.makedirs(p['out'], exist_ok=True)
    raw = os.path.join(os.path.dirname(p['out']) or '.',
                       os.path.basename(p['out']) + '.raw')
    bus.emit({'event': 'phase',
              'message': '%s解锁成功 → 解密镜像(约%.0fMB)…'
              % (r['via'], (r.get('volume_size') or 0) / 1048576)})
    path = bitlocker.decrypt_range(p['source'], part, r['fvek_key'], r['algo'],
                                   raw, on_progress=lambda e: bus.emit(e),
                                   cancel=cancel)
    if path is None:
        bus.emit({'event': 'cancelled'})
        return
    bus.emit({'event': 'done', 'raw': path, 'via': r['via'],
              'algo': r['algo'], 'volume_size': r.get('volume_size')})


@runner('davstrip')
def _davstrip(bus, cancel, p):
    """海康.dav剥离: p={source, out} → PS剥裸流 + MP4重封装(复用h264carve)"""
    import dav
    src = p['source']
    if not dav.is_dav(src):
        raise RuntimeError('未识别到PS流(00 00 01 BA pack头) — 不是海康式.dav/.mpg')
    os.makedirs(p['out'], exist_ok=True)
    import fsutil
    base = fsutil.safe_base(src)
    raw = os.path.join(p['out'], base + '.h264')
    bus.emit({'event': 'phase', 'message': '剥离PS包装(视频PES→裸流)…'})
    st = dav.extract_video(src, raw, on_progress=lambda e: bus.emit(e), cancel=cancel)
    if st.get('cancelled'):
        bus.emit({'event': 'cancelled'})
        return
    if not st['video_pes']:
        try:
            os.remove(raw)
        except OSError:
            pass
        raise RuntimeError('未找到视频PES包(E0-EF) — 可能是音频轨或私有格式')
    bus.emit({'event': 'phase', 'message': '裸流 %.1fMB → 走查+重封装…' % (st['bytes_out'] / 1048576)})
    info, _ = dav.remux(raw, os.path.join(p['out'], base + '.mp4'))
    if info and info['codec'] == 'h265':
        raw2 = os.path.join(p['out'], base + '.h265')
        try:
            os.rename(raw, raw2)
            raw = raw2
        except OSError:
            pass
    bus.emit({'event': 'done', 'raw': raw,
              'mp4': (os.path.join(p['out'], base + '.mp4') if info and info.get('mp4') else None),
              'packs': st['packs'], 'video_pes': st['video_pes'],
              'audio_pes': st['audio_pes'], 'bytes_out': st['bytes_out'],
              **(info or {})})


@runner('ransomscan')
def _ransomscan(bus, cancel, p):
    """勒索现场检测: p={dir}"""
    import scenecheck as ransom
    bus.emit({'event': 'phase', 'message': '扫描目录特征(扩展名/勒索信/熵)…'})
    res = ransom.scan_dir(p['dir'], on_progress=lambda e: bus.emit(e))
    if cancel.is_set():
        bus.emit({'event': 'cancelled'})
        return
    verdict = ransom.analyze(res)
    bus.emit({'event': 'done', **verdict})


@runner('chkscan')
def _chkscan(bus, cancel, p):
    """CHK识别提取: p={dir, out?}"""
    import chkfile
    bus.emit({'event': 'phase', 'message': '识别 .CHK 内容…'})
    out = p.get('out') if p.get('extract') else None
    res = chkfile.scan_dir(p['dir'], out_dir=out, on_progress=lambda e: bus.emit(e))
    if cancel.is_set():
        bus.emit({'event': 'cancelled'})
        return
    identified = [r for r in res if r['parts']]
    bus.emit({'event': 'done', 'results': res[:2000],
              'total': len(res), 'identified': len(identified),
              'out': out})


@runner('raidrebuild')
def _raidrebuild(bus, cancel, p):
    """RAID重组: p={members: [路径|null...], out, cfg?{level,stripe_kb,order,rotation}}
    无cfg时自动检测"""
    import raid
    members = p.get('members') or []
    if len(members) < 2:
        raise RuntimeError('至少两块成员盘镜像(缺失盘用空行占位)')
    cfg = p.get('cfg')
    if not cfg:
        bus.emit({'event': 'phase', 'message': '候选配置检测(级别×条带×盘序×布局)…'})
        cfg = raid.detect(members, on_progress=lambda e: bus.emit(e))
        if cfg.get('error') and cfg.get('score', 0) < 10:
            raise RuntimeError(cfg['error'])
    os.makedirs(p['out'], exist_ok=True)
    raw = os.path.join(p['out'], 'raid_rebuilt.raw')
    lvl = cfg['level']
    rot = cfg.get('rotation')
    bus.emit({'event': 'detected', 'level': lvl, 'stripe_kb': cfg['stripe_kb'],
              'order': cfg['order'], 'rotation': rot,
              'ambiguous': bool(cfg.get('ambiguous')),
              'detail': cfg.get('detail', ''),
              'virtual_size': cfg.get('virtual_size')})
    bus.emit({'event': 'phase', 'message': '重建虚拟盘raw…'})
    path = raid.rebuild(members, cfg, raw,
                        on_progress=lambda e: bus.emit(e), cancel=cancel)
    if path is None:
        bus.emit({'event': 'cancelled'})
        return
    bus.emit({'event': 'done', 'raw': path, 'level': lvl,
              'stripe_kb': cfg['stripe_kb'], 'rotation': rot,
              'candidates': cfg.get('candidates', [])[:3],
              'virtual_size': cfg.get('virtual_size')})


@runner('hfslist')
def _hfslist(bus, cancel, p):
    """Mac文件系统: p={source, partition_lba, extract?, out?}"""
    import macfs
    src = p['source']
    base = int(p.get('partition_lba') or 0)
    d = macfs.detect(src, base)
    if d.get('kind') == 'apfs':
        bus.emit({'event': 'done', 'kind': 'apfs', 'detail': d['detail'],
                  'block_size': d.get('block_size')})
        return
    if d.get('kind') != 'hfs+':
        raise RuntimeError(d.get('detail') or '非HFS+卷')
    bus.emit({'event': 'phase', 'message': '遍历catalog B树…'})
    with open(src, 'rb') as f:
        vol = macfs.Vol(f, base)
        files, folders = vol.load()
    saved = []
    if p.get('extract') and p.get('out'):
        os.makedirs(p['out'], exist_ok=True)
        with open(src, 'rb') as f:
            vol = macfs.Vol(f, base)
            files2, _ = vol.load()
            for i, fl in enumerate(files2):
                if cancel.is_set():
                    bus.emit({'event': 'cancelled'})
                    return
                data = vol.read_file(fl)
                safe = fsutil.safe_name(fl['path'], 'file_%d' % i)
                dest = os.path.join(p['out'], safe)
                k = 1
                while os.path.exists(dest):
                    dest = os.path.join(p['out'], '%s(%d)' % (safe, k))
                    k += 1
                with open(dest, 'wb') as w:
                    w.write(data or b'')
                saved.append({'file': dest, 'size': len(data or b''),
                              'overflow': data is None})
                if i % 20 == 0:
                    bus.emit({'event': 'progress', 'extracted': i + 1,
                              'total': len(files2)})
    listing = [{'id': fl['id'], 'path': fl['path'], 'size': fl['size'],
                'extents': len(fl['extents'])} for fl in files[:5000]]
    bus.emit({'event': 'done', 'kind': 'hfs+', 'files': listing,
              'count': len(files), 'folders': len(folders) - 1,
              'block_size': vol.bs, 'saved': saved,
              'node_info': getattr(vol, 'leaf_stats', {})})


@runner('pstcheck')
def _pstcheck(bus, cancel, p):
    """PST/OST检查: p={source}; 头损自动落兜底扫描"""
    import pst as pstmod
    bus.emit({'event': 'phase', 'message': '读取PST头…'})
    d = pstmod.analyze(p['source'])
    if d.get('kind') != 'pst':
        raise RuntimeError(d.get('detail') or d.get('error') or '非PST/OST')
    bus.emit({'event': 'phase', 'message': '格式: %s' % d.get('index_type', '?')})
    if d.get('healthy'):
        d.pop('histogram', None)
        bus.emit({'event': 'done', **d})
        return
    if d.get('bits') in (32, 64):
        bus.emit({'event': 'phase', 'message': '头损 → 全盘兜底扫描desc叶页…'})
        r = pstmod.recover_scan(p['source'],
                                on_progress=lambda e: bus.emit(e),
                                cancel=cancel)
        if cancel.is_set():
            bus.emit({'event': 'cancelled'})
            return
        bus.emit({'event': 'done', **{k: d[k] for k in ('kind', 'index_type', 'bits')},
                  'healthy': False, 'detail': d.get('detail', ''),
                  'recovered_pages': r.get('recovered_pages', 0),
                  'recovered_messages': r.get('recovered_messages', 0),
                  'messages': r.get('messages', [])[:2000]})
    else:
        bus.emit({'event': 'done', **d})


@runner('report')
def _report(bus, cancel, p):
    """一键HTML报告: p={out, dev_model, dev_path, dev_size, serial,
    probe, smart, image, scan, files, extract, speed_bins, wxdat, notes}"""
    import report
    bus.emit({'event': 'phase', 'message': '生成HTML报告…'})
    d = os.path.dirname(p['out'])
    if d:
        os.makedirs(d, exist_ok=True)
    report.generate(p['out'], p)
    bus.emit({'event': 'done', 'out': p['out']})


@runner('hash')
def _hash(bus, cancel, p):
    ev, rc, killed = sfadapter.run_killable(['hash', '--source', p['source']], cancel, on_event=bus.emit)
    if killed:
        bus.emit({'event': 'cancelled'})
        return
    if rc != 0:
        raise RuntimeError('sf-cli hash 失败 rc=%s: %s' % (rc, (ev or {}).get('message')))
    bus.emit({'event': 'done', 'sha256': (ev or {}).get('sha256')})


@runner('carve')
def _carve(bus, cancel, p):
    args = ['carve', '--source', p['source'], '--out', p['out']]
    if p.get('types'):
        args += ['--types', p['types']]
    if p.get('start') is not None:
        args += ['--start', int(p['start'])]
    if p.get('end') is not None:
        args += ['--end', int(p['end'])]
    if p.get('mapfile') and os.path.exists(p['mapfile']):
        args += ['--map', p['mapfile']]
    ev, rc, killed = sfadapter.run_killable(args, cancel, on_event=bus.emit)
    if killed:
        bus.emit({'event': 'cancelled'})
        return
    if rc != 0:
        raise RuntimeError('sf-cli carve 失败 rc=%s: %s' % (rc, (ev or {}).get('message')))
    bus.emit({'event': 'done', 'result': ev})


@runner('carve_free')
def _carve_free(bus, cancel, p):
    """未分配空间预筛雕刻: p={source, partition_lba(分区起始字节), out, types?, mapfile?}
    先解析分配图(NTFS $Bitmap / FAT表) → 只在空闲区间逐段carve(快+降误报)"""
    import allocmap
    src = p['source']
    part = int(p.get('partition_lba') or 0)
    bus.emit({'event': 'phase', 'message': '解析分配图(NTFS $Bitmap / FAT表)…'})
    info = allocmap.analyze(src, part, on_progress=lambda e: bus.emit(e))
    if not info.get('ok'):
        raise RuntimeError('分配图解析失败: %s — 请改用全范围雕刻' % info.get('error'))
    stats = {k: info[k] for k in ('fs', 'cluster_size', 'total_clusters',
                                  'free_clusters', 'allocated_clusters', 'bytes_free')}
    bus.emit({'event': 'allocmap', **stats})
    ranges = allocmap.merge_extents(info['extents'], max_ranges=24)
    if not ranges:
        bus.emit({'event': 'done', 'files_found': 0, 'ranges': 0, **stats})
        return
    total = sum(e - s for s, e in ranges)
    bus.emit({'event': 'phase',
              'message': '%s 未分配 %.1fGB / %d段 → 逐段雕刻' % (
                  stats['fs'].upper(), total / 1e9, len(ranges))})
    found = 0
    done = 0
    for i, (s, e) in enumerate(ranges):
        if cancel.is_set():
            bus.emit({'event': 'cancelled'})
            return
        span = e - s
        args = ['carve', '--source', src, '--out', p['out'],
                '--start', str(s), '--end', str(e)]
        if p.get('types'):
            args += ['--types', p['types']]
        if p.get('mapfile') and os.path.exists(p['mapfile']):
            args += ['--map', p['mapfile']]

        def on_ev(ev, base=found, done0=done, span=span, ri=i, rn=len(ranges)):
            if ev.get('event') == 'carve_progress':
                bus.emit({'event': 'carve_progress',
                          'bytes_scanned': done0 + min(ev.get('bytes_scanned') or 0, span),
                          'bytes_total': total,
                          'files_found': base + (ev.get('files_found') or 0),
                          'range': ri + 1, 'ranges': rn})
            else:
                bus.emit(ev)
        ev, rc, killed = sfadapter.run_killable(args, cancel, on_event=on_ev)
        if killed:
            bus.emit({'event': 'cancelled'})
            return
        if rc != 0:
            raise RuntimeError('sf-cli carve 失败 rc=%s: %s' % (rc, (ev or {}).get('message')))
        found += (ev or {}).get('files_found') or (ev or {}).get('files') or 0
        done += span
    bus.emit({'event': 'done', 'files_found': found, 'ranges': len(ranges),
              'bytes_scanned': total, **stats})


@runner('verify')
def _verify(bus, cancel, p):
    """对已恢复目录做验证分类(复用实战verify代码)"""
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location('verifier', os.path.join(here, 'verifier_base.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    import io as _io, contextlib
    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        mod.main(p['dir'])
    bus.emit({'event': 'done', 'summary': buf.getvalue().strip()})
