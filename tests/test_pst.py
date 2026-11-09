# -*- coding: utf-8 -*-
"""PST/OST测试(全合成, 格式经libpst校准):
A. 合成64位PST(!BDN头+desc叶页+块叶页) → 识别+结构体检(目录/邮件/孤儿/缺块)
B. 头损(清零B树根指针) → analyze报不健康 → recover_scan兜底找回邮件清单
C. 乱数据拒绝
"""
import os
import sys
import struct
import tempfile
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'engine'))

import pst as pstmod

FAILS = []
DESC_PAGE_OFF = 0x2000            # desc叶页
BLOCK_PAGE_OFF = 0x2200           # 块叶页


def check(name, ok, detail=''):
    print(('PASS ' if ok else 'FAIL ') + name + (('  ' + detail) if detail else ''), flush=True)
    if not ok:
        FAILS.append(name)


def desc_leaf_page(entries, pg=512):
    """entries=[(d_id,desc_id,tree_id,parent)] → 叶页(64位格式, 512B或4K页)"""
    page = bytearray(pg)
    pos = 0
    for d_id, desc_id, tree_id, parent in entries:
        struct.pack_into('<QQQI', page, pos, d_id, desc_id, tree_id, parent)
        pos += 32
    if pg == 4096:
        struct.pack_into('<H', page, 0xFD8, len(entries))      # count(两字节)
        struct.pack_into('<H', page, 0xFDA, 128)               # capacity
        page[0xFDC] = 32
        page[0xFDD] = 0
        struct.pack_into('<Q', page, 0xFF0, 0)
    else:
        page[0x1E8] = len(entries)
        page[0x1E9] = 16
        page[0x1EA] = 32
        page[0x1EB] = 0
        struct.pack_into('<Q', page, 0x1F8, 0)
    return bytes(page)


def block_leaf_page(entries, pg=512):
    """entries=[(bid, offset, size)] → 64位index叶页(512B或4K页)"""
    page = bytearray(pg)
    pos = 0
    for bid, off, size in entries:
        struct.pack_into('<QQH', page, pos, bid, off, size)
        pos += 24
    if pg == 4096:
        struct.pack_into('<H', page, 0xFD8, len(entries))
        struct.pack_into('<H', page, 0xFDA, 128)
        page[0xFDC] = 24
        page[0xFDD] = 0
        struct.pack_into('<Q', page, 0xFF0, 0)
    else:
        page[0x1E8] = len(entries)
        page[0x1E9] = 21
        page[0x1EA] = 24
        page[0x1EB] = 0
        struct.pack_into('<Q', page, 0x1F8, 0)
    return bytes(page)


def build(path, fmt='64'):
    """合成PST: fmt='64'(512B页)或'4k'(4096B页, Outlook 2013+)"""
    img = bytearray(256 * 1024 if fmt == '4k' else 64 * 1024)
    PG = 4096 if fmt == '4k' else 512
    DP = DESC_PAGE_OFF if fmt != '4k' else 0x4000
    BP = BLOCK_PAGE_OFF if fmt != '4k' else 0x5000
    # ---- 头 ----
    struct.pack_into('<I', img, 0, 0x4E444221)                  # !BDN
    img[0x0A] = 0x24 if fmt == '4k' else 0x17
    img[0x201] = 0                                              # 未加密
    struct.pack_into('<Q', img, 0xE0, DP)                       # desc树根
    struct.pack_into('<Q', img, 0xF0, BP)                       # 块树根
    struct.pack_into('<Q', img, 0xB8, len(img))                 # 文件尾
    # ---- desc叶页(升序d_id) ----
    ROOT_IPM = 0x2142                                           # Top of Folders
    F1, F2 = 0x2101, 0x2121                                     # 两个目录(nid类型=0x01)
    M1, M2, M3 = 0x2204, 0x2224, 0x2404                         # 邮件(nid类型=0x04)
    B1, B2 = 0x0104, 0x0204                                     # 邮件数据块id
    descs = [  # 叶页内d_id须升序
        (F1, 0, 0, ROOT_IPM),
        (F2, 0, 0, ROOT_IPM),
        (ROOT_IPM, 0, 0, 0),
        (M1, B1, 0, F1),                                        # 正常邮件
        (M2, B2, 0, F2),                                        # 数据块缺失
        (M3, 0x0304, 0, 0x9999),                                # 孤儿(parent不存在)
    ]
    img[DP:DP + PG] = desc_leaf_page(descs, PG)
    # ---- 块叶页 ----
    img[BP:BP + PG] = block_leaf_page([
        (B1, 0x8000, 0x40),
        (0x0304, 0x8100, 0x40),
    ], PG)
    # 数据块占位
    img[0x8000:0x8040] = b'MSG1-DATA'
    img[0x8100:0x8140] = b'MSG3-DATA'
    with open(path, 'wb') as f:
        f.write(img)
    return {'folders': 2, 'messages': 3, 'orphans': 1, 'missing': 1}


def test_detect(tmp):
    p = os.path.join(tmp, 'a.pst')
    build(p)
    d = pstmod.detect(p)
    check('PST识别', d.get('kind') == 'pst' and d['bits'] == 64 and
          d['encryption'] == 0 and d['desc_root'] == DESC_PAGE_OFF,
          str({k: d.get(k) for k in ('kind', 'bits', 'encryption')}))
    junk = os.path.join(tmp, 'junk.pst')
    open(junk, 'wb').write(os.urandom(4096))
    check('乱数据拒绝', pstmod.detect(junk).get('kind') is None)


def test_analyze(tmp):
    p = os.path.join(tmp, 'a.pst')
    exp = build(p)
    d = pstmod.analyze(p)
    check('体检: 健康', d.get('healthy') is True)
    check('体检: 目录数', d.get('folder_count') == exp['folders'],
          '%s vs %s' % (d.get('folder_count'), exp['folders']))
    check('体检: 邮件数', d.get('message_count') == exp['messages'])
    check('体检: 孤儿邮件', d.get('orphan_messages') == exp['orphans'],
          str(d.get('orphan_messages')))
    check('体检: 数据块缺失', d.get('missing_blocks') == exp['missing'],
          str(d.get('missing_blocks')))


def test_recover(tmp):
    p = os.path.join(tmp, 'a.pst')
    build(p)
    # 头损: 清零两棵B树根指针
    with open(p, 'r+b') as f:
        f.seek(0xE0)
        f.write(b'\x00' * 16)
    d = pstmod.analyze(p)
    check('头损: 体检报不健康', d.get('healthy') is False and '兜底' in d.get('detail', ''),
          str(d.get('detail'))[:60])
    r = pstmod.recover_scan(p)
    check('兜底扫描: 找回邮件清单', r.get('recovered_messages') == 3,
          'pages=%s msgs=%s' % (r.get('recovered_pages'), r.get('recovered_messages')))
    check('兜底扫描: 邮件d_id正确',
          sorted(m['d_id'] for m in r['messages']) == [0x2204, 0x2224, 0x2404])


def test_4k(tmp):
    p = os.path.join(tmp, 'a4k.pst')
    exp = build(p, fmt='4k')
    d = pstmod.analyze(p)
    check('4K: 识别+体检', d.get('kind') == 'pst' and d.get('page_size') == 4096 and
          d.get('healthy') is True, str({k: d.get(k) for k in ('page_size', 'healthy')}))
    check('4K: 计数', d.get('folder_count') == 2 and d.get('message_count') == 3 and
          d.get('orphan_messages') == 1 and d.get('missing_blocks') == 1,
          str({k: d.get(k) for k in ('folder_count', 'message_count',
                                     'orphan_messages', 'missing_blocks')}))
    with open(p, 'r+b') as f:
        f.seek(0xE0)
        f.write(bytes(16))
    r = pstmod.recover_scan(p)
    check('4K: 头损兜底找回', r.get('recovered_messages') == 3 and
          r.get('page_size') == 4096, 'msgs=%s' % r.get('recovered_messages'))


def main():
    tmp = tempfile.mkdtemp(prefix='pst_')
    try:
        test_detect(tmp)
        test_analyze(tmp)
        test_recover(tmp)
        test_4k(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print('\n%s: %d项失败' % ('总判定' if FAILS else '全绿', len(FAILS)), flush=True)
    sys.exit(1 if FAILS else 0)


if __name__ == '__main__':
    main()
