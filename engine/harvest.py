# -*- coding: utf-8 -*-
"""恢复成果总览 — 把所有任务产出的输出目录/文件聚合一张账
数据来源: jobstore任务记录(params/result) → 找输出路径 → 实地walk数文件算字节。
回答交付时的三个问题: 恢复了什么? 在哪? 一共多少?
"""
import os
import time


def _norm(p):
    return os.path.normcase(os.path.abspath(p))


def _out_paths(job):
    """任务记录 → (输出目录列表, 输出文件列表)"""
    k = job.get('kind')
    p = job.get('params') or {}
    r = job.get('result') or {}
    dirs, files = [], []
    if k in ('extract', 'carve', 'carve_free', 'h264carve', 'exfatdeep',
             'ext4journal', 'raidrebuild', 'hfslist', 'vmexpand', 'wxdat',
             'b64scan', 'chkscan', 'sqlscan', 'exifscan'):
        if p.get('out'):
            dirs.append(p['out'])
    elif k == 'blunlock':
        if p.get('out'):                       # pipeline: raw = out + '.raw'
            files.append(p['out'].rstrip('/\\') + '.raw')
        if r.get('raw'):
            files.append(r['raw'])
    elif k == 'image':
        if p.get('dest'):
            files.append(p['dest'])
    elif k == 'report':
        fp = r.get('out') or p.get('out')
        if fp:
            files.append(fp)
    elif k == 'mp4repair':
        for it in (r.get('results') or [])[:500]:
            if it.get('output'):
                files.append(it['output'])
    return dirs, files


def _walk_dir(d):
    n = b = 0
    latest = 0
    for dp, _, names in os.walk(d):
        for fn in names:
            fp = os.path.join(dp, fn)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            n += 1
            b += st.st_size
            if st.st_mtime > latest:
                latest = st.st_mtime
    return n, b, latest


def collect(jobs=None, limit=200):
    """→ {'entries': [...], 'total_files', 'total_bytes', 'missing': n}
    entries: {kind(聚合徽章), dir(bool), path, files, bytes, mtime, jobs} 按mtime降序"""
    if jobs is None:
        import jobstore
        jobs = jobstore.list_jobs()
    ent = {}
    missing = 0
    for j in jobs:
        if j.get('state') not in ('done', 'cancelled'):
            continue                      # 失败/运行中的任务不进账
        dirs, files = _out_paths(j)
        for d in dirs + files:
            key = _norm(d)
            if key not in ent:
                ent[key] = {'kind': j['kind'], 'kinds': [], 'dir': os.path.isdir(d),
                            'path': d, 'files': 0, 'bytes': 0, 'mtime': 0,
                            'jobs': [], 'created': j.get('created', 0)}
        for d in dirs:
            e = ent[_norm(d)]
            if not e['dir']:
                continue                  # 目录已不存在(被移走/删除)
            e['kinds'].append(j['kind'])
            e['jobs'].append(j['id'])
        for fp in files:
            e = ent[_norm(fp)]
            if e['dir'] or os.path.isfile(fp):
                e['kinds'].append(j['kind'])
                e['jobs'].append(j['id'])
    out = {'entries': [], 'total_files': 0, 'total_bytes': 0, 'missing': 0}
    for e in ent.values():
        if not e['dir'] and not os.path.isfile(e['path']):
            out['missing'] += 1
            continue
        if e['dir']:
            e['files'], e['bytes'], e['mtime'] = _walk_dir(e['path'])
            if e['mtime'] == 0:
                e['mtime'] = e['created']
        else:
            try:
                st = os.stat(e['path'])
                e['files'], e['bytes'], e['mtime'] = 1, st.st_size, st.st_mtime
            except OSError:
                out['missing'] += 1
                continue
        e['kinds'] = sorted(set(e['kinds']))
        e['kind'] = ' + '.join(e['kinds'][:2]) + ('等' if len(e['kinds']) > 2 else '')
        out['entries'].append(e)
    out['entries'].sort(key=lambda x: -x['mtime'])
    out['entries'] = out['entries'][:limit]
    out['total_files'] = sum(e['files'] for e in out['entries'])
    out['total_bytes'] = sum(e['bytes'] for e in out['entries'])
    return out
