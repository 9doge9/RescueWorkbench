# -*- coding: utf-8 -*-
"""单元测试: mapfile边界 / sfadapter事件转发(回归bug) / 稳定性探测 / safeguard"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'engine'))

FAILS = []


def check(name, ok, detail=''):
    print(('PASS ' if ok else 'FAIL ') + name + (('  ' + detail) if detail else ''), flush=True)
    if not ok:
        FAILS.append(name)


# ---- mapfile ----
from mapfile import MapFile, MapError, FINISHED, BAD_SECTOR, NON_TRIMMED

m = MapFile(10000)
m.mark(0, 4096, FINISHED)
m.mark(4096, 512, BAD_SECTOR)
check('mark产生三段', len(m.blocks) == 3, str(m.blocks))
m.mark(0, 4608, FINISHED)
check('覆盖合并', len(m.blocks) == 2, str([b[2] for b in m.blocks]))
# 边界
ok = False
try:
    m.mark(9999, 2, FINISHED)      # 越界
except MapError:
    ok = True
check('mark越界拒绝', ok)
# roundtrip
p = tempfile.mktemp(suffix='.map')
m.save(p)
m2 = MapFile.load(p, expect_size=10000)
check('save/load往返', m2.blocks == m.blocks)
os.unlink(p)
# 尺寸不匹配拒绝
ok = False
try:
    MapFile.load(p, expect_size=999) if os.path.exists(p) else None
    with open(p, 'w') as f:
        f.write('0x0     ?\n0x00000000  0x00002710  ?\n')
    MapFile.load(p, expect_size=999)
except MapError:
    ok = True
check('map尺寸不符拒绝', ok)
os.unlink(p)

# ---- sfadapter 事件转发(回归: 曾丢on_event) ----
import sfadapter
seen = []
ev, rc = sfadapter.run(['version'], on_event=lambda e: seen.append(e))
check('run转发全部事件', len(seen) >= 2 and any(e.get('event') == 'command' for e in seen),
      '%d个事件' % len(seen))
check('run返回最后事件', (ev or {}).get('event') == 'version')

# ---- stability ----
import stability
tmpf = tempfile.mktemp(suffix='.bin')
with open(tmpf, 'wb') as f:
    f.write(os.urandom(6 * 1024 * 1024))
bad, total, det = stability.probe_repeat(stability.open_source(tmpf), 6 * 1024 * 1024)
check('定点探测稳定文件', bad == 0)
drift, dd = stability.probe_stream_vs_seek(stability.open_source(tmpf), 6 * 1024 * 1024, span_mb=4)
check('流式vs定点一致', drift is False)
os.unlink(tmpf)

# ---- imager 叶子块抽检(回归: 曾mid越界误判坏) ----
from imager import VerifiedImager
src = tempfile.mktemp(suffix='.img')
dst = src + '.copy'
with open(src, 'wb') as f:
    f.write(os.urandom(700 * 1024))          # 非整块尾块
res = VerifiedImager(src, dst).run()
check('非整块文件无坏区', res['bad_bytes'] == 0 and res['finished_bytes'] == 700 * 1024)
check('非整块文件逐字节一致', open(src, 'rb').read() == open(dst, 'rb').read())
for p in (src, dst, dst + '.map'):
    os.unlink(p)

def test_findpart():
    import findpart
    vhd = os.path.join(os.path.dirname(HERE), 'tests', 'fixtures', 'exfat.vhd')
    if not os.path.exists(vhd):
        return
    parts = findpart.find_partitions(vhd)
    assert parts and parts[0]['fs'] == 'exfat' and parts[0]['lba'] == 128, parts
    hit = findpart.find_fs(vhd, 'exfat')
    assert hit and hit['offset'] == 128 * 512
    # 无分区表的裸exFAT(取exfat.vhd数据区造一个)
    import tempfile, shutil
    tmp = tempfile.mkdtemp()
    try:
        raw = os.path.join(tmp, 'raw.img')
        with open(vhd, 'rb') as f, open(raw, 'wb') as w:
            f.seek(128 * 512)
            w.write(f.read(4 * 1024 * 1024))
        parts2 = findpart.find_partitions(raw)
        assert parts2 and parts2[0]['fs'] == 'exfat' and parts2[0]['lba'] == 0, parts2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_orphan_strip():
    import sfadapter
    import tempfile, shutil
    d = tempfile.mkdtemp()
    try:
        for n in ('a (id 5).txt', 'b (id 12).jpg', 'c.jpg'):
            open(os.path.join(d, n), 'wb').write(b'x')
        n = sfadapter.strip_orphan_suffix(d)
        assert n == 2, n
        assert sorted(os.listdir(d)) == ['a.txt', 'b.jpg', 'c.jpg']
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_harvest():
    import harvest
    import tempfile, shutil
    base = tempfile.mkdtemp()
    try:
        d1 = os.path.join(base, 'out_exfat')          # 目录产出
        os.makedirs(d1)
        open(os.path.join(d1, 'a.jpg'), 'wb').write(b'x' * 1000)
        open(os.path.join(d1, 'b.jpg'), 'wb').write(b'y' * 3000)
        f2 = os.path.join(base, 'dec.raw')            # 文件产出
        open(f2, 'wb').write(b'z' * 500)
        gone = os.path.join(base, 'gone_dir')         # 已不存在的输出
        jobs = [
            {'id': 'j1', 'kind': 'exfatdeep', 'state': 'done',
             'params': {'out': d1}, 'result': {}, 'created': 100},
            {'id': 'j2', 'kind': 'blunlock', 'state': 'done',
             'params': {'out': os.path.join(base, 'xx')}, 'result': {'raw': f2}, 'created': 200},
            {'id': 'j3', 'kind': 'extract', 'state': 'done',
             'params': {'out': gone}, 'result': {}, 'created': 300},
            {'id': 'j4', 'kind': 'extract', 'state': 'failed',   # 失败任务不进账
             'params': {'out': d1}, 'result': None, 'created': 400},
        ]
        r = harvest.collect(jobs=jobs)
        assert r['total_files'] == 3, r
        assert r['total_bytes'] == 4500, r
        assert r['missing'] == 2, r                     # gone目录 + blunlock的out+'.raw'不存在
        paths = [e['path'] for e in r['entries']]
        assert d1 in paths and f2 in paths, paths
        e1 = next(e for e in r['entries'] if e['path'] == d1)
        assert e1['dir'] and e1['files'] == 2 and e1['bytes'] == 4000, e1
        e2 = next(e for e in r['entries'] if e['path'] == f2)
        assert (not e2['dir']) and e2['files'] == 1 and e2['bytes'] == 500, e2
        assert e1['kinds'] == ['exfatdeep'], e1['kinds']
    finally:
        shutil.rmtree(base, ignore_errors=True)

def test_jobstore_stability():
    """稳定性审计修复的回归: 原子写往返 + 假活任务扫除 + limit"""
    import jobstore
    import time as _t
    old_dir = jobstore.JOBS_DIR
    import tempfile, shutil
    d = tempfile.mkdtemp()
    try:
        jobstore.JOBS_DIR = d                    # 重定向到临时目录
        j = jobstore.new_job('probe', {'source': 'X'})
        j['state'] = 'running'
        jobstore.save(j)
        # 原子写: 目录里不应残留 .tmp
        assert not any(f.endswith('.tmp') for f in os.listdir(d)), os.listdir(d)
        # 往返
        j2 = jobstore.load(j['id'])
        assert j2 and j2['state'] == 'running'
        # 假活扫除
        j3 = jobstore.new_job('image', {'source': 'Y'})
        j3['state'] = 'pending'
        jobstore.save(j3)
        n = jobstore.mark_stale_jobs()
        assert n == 2, n
        assert jobstore.load(j['id'])['state'] == 'failed'
        assert '服务重启中断' in jobstore.load(j['id'])['error']
        # limit
        assert len(jobstore.list_jobs(limit=1)) == 1
        # done的不被扫
        j4 = jobstore.new_job('hash', {'source': 'Z'})
        j4['state'] = 'done'
        jobstore.save(j4)
        assert jobstore.mark_stale_jobs() == 0
        assert jobstore.load(j4['id'])['state'] == 'done'
    finally:
        jobstore.JOBS_DIR = old_dir
        shutil.rmtree(d, ignore_errors=True)

if __name__ == '__main__':
    for _t in (test_findpart, test_orphan_strip, test_harvest, test_jobstore_stability):
        try:
            _t()
        except AssertionError as e:
            FAILS.append(_t.__name__)
            print('FAIL %s %s' % (_t.__name__, e), flush=True)
    print('\n====== 单元结果: %s ======' % ('全部通过' if not FAILS else '失败: %s' % FAILS))
    sys.exit(1 if FAILS else 0)
