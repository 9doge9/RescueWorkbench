# -*- coding: utf-8 -*-
"""Tier4前两项测试: 勒索现场检测 + CHK识别拆分
勒索: 合成被勒索目录(怪扩展×N + 勒索信含BTC/onion + 高熵) → high+家族;
      干净目录 → low; 正常.zip文件不误报
CHK:  合成 JPG+ZIP 拼接CHK / 纯JPG CHK / 不可识别CHK → 拆分字节精确
"""
import os
import sys
import struct
import tempfile
import shutil
import random

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'engine'))

import scenecheck as ransom
import chkfile

FAILS = []


def check(name, ok, detail=''):
    print(('PASS ' if ok else 'FAIL ') + name + (('  ' + detail) if detail else ''), flush=True)
    if not ok:
        FAILS.append(name)


def _jpg(n=8000):
    return b'\xff\xd8\xff\xe0' + b'\x7f' * n + b'\xff\xd9'


def _zipf(inner=b'hello'):
    import io
    import zipfile
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, 'w') as z:
        z.writestr('a.txt', inner)
    return bio.getvalue()


def test_ransom(tmp):
    # 被勒索现场
    hit = os.path.join(tmp, 'hit')
    os.makedirs(os.path.join(hit, 'docs'), exist_ok=True)
    rnd = random.Random(42)
    for i in range(40):
        with open(os.path.join(hit, 'docs', 'file%02d.makop' % i), 'wb') as f:
            f.write(bytes(rnd.randrange(256) for _ in range(20000)))
    for i in range(10):
        open(os.path.join(hit, 'normal%02d.jpg' % i), 'wb').write(_jpg(500))
    note = ('All your files have been encrypted!\n'
            'Send 0.5 BTC to bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh\n'
            'Contact: http://gx7yq5v2m3x4abcdefgxyz.onion/restore\n'
            'MAKOP ransomware')
    open(os.path.join(hit, 'readme-warning.txt'), 'w').write(note)
    res = ransom.scan_dir(hit)
    v = ransom.analyze(res)
    check('勒索: suspicion=high', v['suspicion'] == 'high',
          str((v['suspicion'], v['evidence'][:2])))
    check('勒索: 家族=Makop', v['family'] == 'Makop', str(v.get('family')))
    check('勒索: 证据含异常扩展名+勒索信',
          any('makop' in e for e in v['evidence']) and
          any('ransom-note-found' in e for e in v['evidence']))
    check('勒索: 有处置建议', len(v['advice']) >= 2)
    # 干净目录
    clean = os.path.join(tmp, 'clean')
    os.makedirs(clean, exist_ok=True)
    for i in range(30):
        open(os.path.join(clean, 'photo%02d.jpg' % i), 'wb').write(_jpg(2000))
    v2 = ransom.analyze(ransom.scan_dir(clean))
    check('干净目录: low', v2['suspicion'] == 'low', str(v2['evidence']))
    # 正常zip不误报(高熵但非勒索)
    zips = os.path.join(tmp, 'zips')
    os.makedirs(zips, exist_ok=True)
    for i in range(25):
        open(os.path.join(zips, 'arc%02d.zip' % i), 'wb').write(_zipf())
    v3 = ransom.analyze(ransom.scan_dir(zips))
    check('正常zip目录: low/medium不误报为high', v3['suspicion'] != 'high',
          v3['suspicion'])


def test_chk(tmp):
    d = os.path.join(tmp, 'found')
    os.makedirs(d, exist_ok=True)
    # ① JPG+ZIP拼接
    combo = _jpg(6000) + _zipf(b'CHK-INNER-TEST')
    open(os.path.join(d, 'FILE0001.CHK'), 'wb').write(combo)
    info = chkfile.identify(os.path.join(d, 'FILE0001.CHK'))
    check('CHK: 拼接识别2段', len(info['parts']) == 2 and
          info['parts'][0]['type'] == 'JPEG' and info['parts'][1]['type'] == 'ZIP/Office',
          str([(p['type'], p['size']) for p in info['parts']]))
    check('CHK: 边界精确', info['parts'][0]['end'] == len(_jpg(6000)) and
          info['coverage'] == 1.0)
    # ② 纯JPG
    open(os.path.join(d, 'FILE0002.CHK'), 'wb').write(_jpg(3000))
    info2 = chkfile.identify(os.path.join(d, 'FILE0002.CHK'))
    check('CHK: 纯JPG识别', info2['head_type'] == 'JPEG' and len(info2['parts']) == 1)
    # ③ 不可识别
    open(os.path.join(d, 'FILE0003.CHK'), 'wb').write(
        bytes(random.Random(7).randrange(256) for _ in range(9000)))
    info3 = chkfile.identify(os.path.join(d, 'FILE0003.CHK'))
    check('CHK: 乱数据不误识别', info3['head_type'] is None and not info3['parts'])
    # ④ 目录级拆分提取 → 字节精确
    out = os.path.join(tmp, 'chkout')
    res = chkfile.scan_dir(d, out_dir=out)
    files = {}
    for r in res:
        for x in r.get('extracted', []):
            files[os.path.basename(x)] = open(x, 'rb').read()
    jpg = open(os.path.join(d, 'FILE0001.CHK'), 'rb').read()[:len(_jpg(6000))]
    zc = _zipf(b'CHK-INNER-TEST')
    got_jpg = next((v for k, v in files.items() if k.startswith('FILE0001') and k.endswith('.jpg')), None)
    got_zip = next((v for k, v in files.items() if k.startswith('FILE0001') and k.endswith('.zip')), None)
    check('CHK: 提取JPG字节一致', got_jpg == jpg)
    check('CHK: 提取ZIP字节一致', got_zip == zc)


def main():
    tmp = tempfile.mkdtemp(prefix='t4_')
    try:
        test_ransom(tmp)
        test_chk(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print('\n%s: %d项失败' % ('总判定' if FAILS else '全绿', len(FAILS)), flush=True)
    sys.exit(1 if FAILS else 0)


if __name__ == '__main__':
    main()
