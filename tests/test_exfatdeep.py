# -*- coding: utf-8 -*-
"""exFAT深度挖掘测试:
A. name_hash/set_checksum 用真夹具8个已知样本校准断言
B. 真exfat.vhd: 全簇扫出 3已删+2孤儿; 双校验通过; 活文件不误报
C. 提取三个文件(含1MB) SHA256与manifest逐一比对
D. 覆盖场景: 把已删条目集从root覆盖掉 → 树遍历看不见的孤儿目录仍能挖出
"""
import os
import sys
import json
import struct
import hashlib
import tempfile
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'engine'))

import exfat_deep as ed

FIX = os.path.join(HERE, 'fixtures')
FAILS = []


def check(name, ok, detail=''):
    print(('PASS ' if ok else 'FAIL ') + name + (('  ' + detail) if detail else ''), flush=True)
    if not ok:
        FAILS.append(name)


VHD = os.path.join(FIX, 'exfat.vhd')
PART = 128 * 512


def test_hash():
    # 与真夹具条目存储值逐一比对(8样本, 2026-11-06实机校准)
    known = [('del_root.txt', 0xe36f), ('暂存', 0xc0b0), ('data.bin', 0xb9c2),
             ('文档', 0xa0c3), ('照片', 0xa0be), ('深层', 0x60b6),
             ('我的 文档 (副本).txt', 0x33c2), ('System Volume Information', 0xffb8)]
    ok = all(ed.name_hash(n) == h for n, h in known)
    check('NameHash规范算法(8样本)', ok,
          str([(n, '%04x' % ed.name_hash(n)) for n, h in known if ed.name_hash(n) != h]))


def scan_vhd(path=VHD):
    return ed.scan(path, PART)


def test_scan():
    res = scan_vhd()
    recs = res['records']
    names = lambda d: [r['name'] for r in recs if r['is_dir'] == d]
    check('扫出5项(3已删+2孤儿)', len(recs) == 5, str([(r['name'], r['deleted']) for r in recs]))
    del_files = sorted(r['name'] for r in recs if r['deleted'] and not r['is_dir'])
    del_dirs = sorted(r['name'] for r in recs if r['deleted'] and r['is_dir'])
    check('已删项', del_files == ['del_in_sub.txt', 'del_root.txt'] and
          del_dirs == ['暂存'], str((del_files, del_dirs)))
    check('孤儿项(整目录删除后的簇内条目)',
          sorted(r['name'] for r in recs if r['orphan'] and not r['is_dir']) ==
          ['临时A.txt', '临时B.bin'])
    check('NameHash/SetChecksum双校验', all(r['hash_ok'] and r['set_checksum_ok'] for r in recs),
          str([(r['name'], r['hash_ok'], r['set_checksum_ok']) for r in recs]))
    check('时间戳解析', all(r['modified'] and r['modified'].startswith('2026-11-05')
                            for r in recs))
    check('活文件不误报', not any(r['name'] in ('data.bin', '说明.docx', '壁纸.jpg')
                                  and not r['deleted'] and not r['orphan'] for r in recs))


def test_extract():
    man = {m['path']: m for m in json.load(
        open(os.path.join(FIX, 'exfat.manifest.json'), encoding='utf-8-sig'))}
    res = scan_vhd()
    with open(VHD, 'rb') as f:
        vol = ed.Vol(f, PART)
        vol.fat_load()
        for name, mpath in (('del_root.txt', 'del_root.txt'),
                            ('临时A.txt', '暂存/临时A.txt'),
                            ('临时B.bin', '暂存/临时B.bin')):
            rec = next((r for r in res['records']
                        if r['name'] == name and not r['is_dir']), None)
            if not rec:
                check('提取' + name, False, '未找到')
                continue
            data, mode, trunc = ed.extract(vol, rec)
            ok = hashlib.sha256(data).hexdigest() == man[mpath]['sha256']
            check('提取%s SHA一致(%s)' % (name, mode), ok and not trunc,
                  '%dB trunc=%s' % (len(data), trunc))


def test_overwritten(tmp):
    """根目录里的已删条目被覆盖后, 孤儿目录簇仍可挖出(树遍历的盲区场景)"""
    import struct as st
    p = os.path.join(tmp, 'ov.vhd')
    shutil.copy(VHD, p)
    with open(p, 'r+b') as f:
        vol = ed.Vol(f, PART)
        # root簇里 del_root.txt 已删条目集 @320..416, 暂存@608..704 → 全部覆盖为0xCC
        f.seek(vol.clus_off(vol.root_cl) + 320)
        f.write(b'\xCC' * 96)
        f.seek(vol.clus_off(vol.root_cl) + 608)
        f.write(b'\xCC' * 96)
    res = ed.scan(p, PART)
    recs = res['records']
    check('覆盖后root已删项消失', not any(r['name'] in ('del_root.txt', '暂存')
                                          and r['dir_cluster'] == 6 for r in recs),
          str([r['name'] for r in recs]))
    # 791孤儿目录簇不受影响: 临时A/B仍可挖出且内容完好
    with open(p, 'rb') as f:
        vol = ed.Vol(f, PART)
        vol.fat_load()
        man = {m['path']: m for m in json.load(
            open(os.path.join(FIX, 'exfat.manifest.json'), encoding='utf-8-sig'))}
        ok = True
        for name, mpath in (('临时A.txt', '暂存/临时A.txt'), ('临时B.bin', '暂存/临时B.bin')):
            rec = next((r for r in recs if r['name'] == name and not r['is_dir']), None)
            if not rec:
                ok = False
                continue
            data, _, _ = ed.extract(vol, rec)
            ok = ok and hashlib.sha256(data).hexdigest() == man[mpath]['sha256']
        check('孤儿项不受root覆盖影响(SHA一致)', ok)


def main():
    test_hash()
    test_scan()
    test_extract()
    tmp = tempfile.mkdtemp(prefix='exdeep_')
    try:
        test_overwritten(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print('\n%s: %d项失败' % ('总判定' if FAILS else '全绿', len(FAILS)), flush=True)
    sys.exit(1 if FAILS else 0)


if __name__ == '__main__':
    main()
