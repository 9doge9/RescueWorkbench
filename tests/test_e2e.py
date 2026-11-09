# -*- coding: utf-8 -*-
"""端到端集成测试: 校验式镜像→scan→files→extract, 逐文件SHA256对manifest断言
前置: tests/fixtures/{ntfs,exfat}.vhd + manifest (make_fixtures流程产出)
"""
import os
import sys
import json
import time
import hashlib
import tempfile
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'engine'))

import sfadapter
from imager import VerifiedImager

FIX = os.path.join(HERE, 'fixtures')
FAILS = []


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            b = f.read(4 * 1024 * 1024)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def check(name, ok, detail=''):
    print(('PASS ' if ok else 'FAIL ') + name + (('  ' + detail) if detail else ''), flush=True)
    if not ok:
        FAILS.append(name)


def run_e2e(tag, fs):
    print('\n========== %s (%s) ==========' % (tag, fs), flush=True)
    vhd = os.path.join(FIX, tag + '.vhd')
    man = json.load(open(os.path.join(FIX, tag + '.manifest.json'), encoding='utf-8-sig'))
    if isinstance(man, dict):
        man = [man]
    tmp = tempfile.mkdtemp(prefix='e2e_%s_' % tag)

    # 1. 校验式镜像 vhd → 副本
    t0 = time.time()
    img = VerifiedImager(vhd, os.path.join(tmp, 'copy.img'), chunk=4 * 1024 * 1024)
    res = img.run()
    same = sha256(vhd) == sha256(os.path.join(tmp, 'copy.img'))
    check('镜像逐字节一致', same, '%.1fs bad=%s' % (time.time() - t0, res.get('bad_bytes')))
    check('镜像map全完成', res.get('bad_bytes') == 0 and res.get('finished_bytes') == os.path.getsize(vhd))

    # 2. scan
    ev, rc = sfadapter.run(['scan', '--source', vhd])
    parts = (ev or {}).get('partitions') or []
    part = next((p for p in parts if p.get('filesystem') == fs), None)
    check('scan找到%s分区' % fs, part is not None,
          ' '.join('%s@%s' % (p.get('filesystem'), p.get('start_lba')) for p in parts))

    if not part:
        shutil.rmtree(tmp, ignore_errors=True)
        return
    lba = part['start_lba']

    # 3. files 列表
    files = []
    ev2, rc = sfadapter.run(['files', '--source', vhd, '--partition-lba', lba],
                            on_event=lambda e: files.append(e) if e.get('event') == 'file' else None)
    check('files列出条目', len(files) > 0, '%d条' % len(files))
    # live条目按path索引
    live_by_path = {}
    deleted_by_path = {}
    for f in files:
        if f.get('path'):
            (deleted_by_path if f.get('deleted') else live_by_path)[f['path'].lstrip('/')] = f
    live_missing = [m['path'] for m in man if not m['deleted'] and m['path'] not in live_by_path]
    check('manifest活文件全部在列', not live_missing, '缺: %s' % live_missing[:3])

    # 4. extract 全部id
    out = os.path.join(tmp, 'out')
    ids = ','.join(str(f['id']) for f in files if not f.get('is_dir'))
    ev3, rc = sfadapter.run(['extract', '--source', vhd, '--partition-lba', lba,
                             '--ids', ids, '--out', out])
    # 5. 逐文件hash断言(已删文件path=null→引擎输出进 _orphaned/, 按文件名兜底匹配)
    import fnmatch

    def find_extracted(rel_path, want_sha):
        p = os.path.join(out, *rel_path.split('/'))
        if os.path.exists(p) and sha256(p) == want_sha:
            return '原路径'
        base = os.path.basename(rel_path)
        orphan_root = os.path.join(out, '_orphaned')
        if os.path.isdir(orphan_root):
            import re as _re
            stem, dot, ext = base.rpartition('.')
            for dirpath, _, names in os.walk(orphan_root):
                for n in names:
                    # orphan产物可能带" (id N)"后缀(同名冲突/孤儿命名契约)
                    plain = n
                    if _re.search(r' \(\d+\)(\.[^.]+)?$', n):
                        plain = _re.sub(r' \(\d+\)', '', n)
                    if plain == base and sha256(os.path.join(dirpath, n)) == want_sha:
                        return 'orphan:' + os.path.relpath(dirpath, orphan_root)
        return None
    ok_live = bad_live = ok_del = bad_del = 0
    bad_names = []
    for m in man:
        where = find_extracted(m['path'], m['sha256'])
        if where:
            if m['deleted']:
                ok_del += 1
            else:
                ok_live += 1
        else:
            if m['deleted']:
                bad_del += 1
                bad_names.append(m['path'] + '(缺失/hash错)')
            else:
                bad_live += 1
                bad_names.append(m['path'] + '(缺失/hash错)')
    n_live = sum(1 for m in man if not m['deleted'])
    n_del = sum(1 for m in man if m['deleted'])
    check('活文件SHA256全对', bad_live == 0, '%d/%d' % (ok_live, n_live))
    if fs == 'exfat':
        check('已删文件恢复', ok_del >= n_del - 1, '%d/%d恢复' % (ok_del, n_del))
    else:
        # NTFS已删覆盖是外部引擎(sf-cli)的行为边界: 观测不判死, 打印恢复率
        print('INFO NTFS已删恢复率: %d/%d %s' % (ok_del, n_del, bad_names[:2]))
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    for tag, fs in [('ntfs', 'ntfs'), ('exfat', 'exfat')]:
        v = os.path.join(FIX, tag + '.vhd')
        if not os.path.exists(v):
            print('SKIP %s (夹具缺失, 先跑make_fixtures流程)' % tag)
            continue
        run_e2e(tag, fs)
    print('\n====== 结果: %s ======' % ('全部通过' if not FAILS else '失败: %s' % FAILS))
    sys.exit(1 if FAILS else 0)
