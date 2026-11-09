# -*- coding: utf-8 -*-
"""Mac文件系统测试:
A. 合成HFS+卷(卷头+位图+catalog双节点B树+双文件) → 识别+列表+提取逐字节
B. 深层路径(嵌套目录)还原
C. extents溢出(>8段不覆盖size)如实返回None
D. APFS识别(合成NXSB) + 非Mac卷拒绝
"""
import os
import sys
import struct
import tempfile
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'engine'))

import macfs

FAILS = []
BS = 4096
VH = 1024
ALLOC_BLK = 4          # 位图块
CATALOG_BLK = 6        # catalog块(节点0=头,节点1=叶子)
CATALOG_NODES = 2
DATA_BLK = 8           # 文件数据起始
TOTAL_BLOCKS = 300

HELLO = b'HELLO-HFS-PLUS-' * 1024          # 15KB → 4块
README = b'# readme\nAPFS is complex, HFS+ is honest.\n' * 200   # ~7KB → 2块


def check(name, ok, detail=''):
    print(('PASS ' if ok else 'FAIL ') + name + (('  ' + detail) if detail else ''), flush=True)
    if not ok:
        FAILS.append(name)


def _fork_bytes(size, exts):
    b = bytearray(80)
    struct.pack_into('<QII', b, 0, size, 0, sum(c for _, c in exts))
    for i, (s, c) in enumerate(exts[:8]):
        struct.pack_into('<II', b, 16 + 8 * i, s, c)
    return bytes(b)


def _key(parent, name):
    nb = name.encode('utf-16-be')
    k = struct.pack('<HIH', 6 + 2 + len(nb), parent, len(name)) + nb
    return k


def _node_desc(kind, nrec, fLink=0):
    return struct.pack('<iibbH6x', fLink, 0, kind & 0xFF, 1, nrec)


def build_hfsplus(path):
    """最小合法HFS+: 卷头/位图/catalog(头节点+1叶节点)/两目录两文件"""
    img = bytearray(TOTAL_BLOCKS * BS)
    # ---- 卷头 ----
    vh = bytearray(512)
    vh[0:2] = b'H+'
    struct.pack_into('<H', vh, 2, 4)
    struct.pack_into('<I', vh, 0x20, 2)             # fileCount
    struct.pack_into('<I', vh, 0x24, 2)             # folderCount(根+docs)
    struct.pack_into('<I', vh, 0x28, BS)            # blockSize
    struct.pack_into('<I', vh, 0x2C, TOTAL_BLOCKS)
    struct.pack_into('<I', vh, 0x30, 10)
    n_alloc = (TOTAL_BLOCKS + BS * 8 - 1) // (BS * 8)   # 位图块数(4096块/块)
    vh[0x70:0xC0] = _fork_bytes(n_alloc * BS, [(ALLOC_BLK, n_alloc)])
    vh[0xC0:0x110] = _fork_bytes(0, [])             # extentsFile空
    vh[0x110:0x160] = _fork_bytes(CATALOG_NODES * BS, [(CATALOG_BLK, CATALOG_NODES)])
    img[VH:VH + 512] = vh
    # ---- 位图 ----
    bm = bytearray(BS * n_alloc)
    used = DATA_BLK + 4 + 2                         # 位图+catalog+hello+readme区
    for i in range(used):
        bm[i // 8] |= 1 << (i % 8)
    img[ALLOC_BLK * BS:(ALLOC_BLK + n_alloc) * BS] = bm
    # ---- 文件数据 ----
    img[DATA_BLK * BS:DATA_BLK * BS + len(HELLO)] = HELLO
    rb = DATA_BLK + 4
    img[rb * BS:rb * BS + len(README)] = README
    # ---- catalog叶节点记录 ----
    def file_rec(fid, size, exts):
        b = bytearray(232)
        struct.pack_into('<H', b, 0, 2)             # recordType=file
        struct.pack_into('<H', b, 2, 0)             # flags
        struct.pack_into('<I', b, 8, fid)
        struct.pack_into('<I', b, 0x10, 0xACC45E00) # contentModDate特征
        b[72:152] = _fork_bytes(size, exts)
        return bytes(b)

    def folder_rec(fid):
        b = bytearray(88)
        struct.pack_into('<H', b, 0, 1)
        struct.pack_into('<H', b, 2, 0)
        struct.pack_into('<I', b, 8, fid)
        return bytes(b)

    def thread_rec(parent, name):
        nb = name.encode('utf-16-be')
        return struct.pack('<HHI', 3, 0, parent) + \
            struct.pack('<H', len(name)) + nb

    recs = [
        _key(1, '') + thread_rec(1, ''),                                  # 根线程
        _key(2, 'docs') + folder_rec(10),
        _key(2, 'hello.txt') + file_rec(100, len(HELLO), [(DATA_BLK, 4)]),
        _key(10, '') + thread_rec(2, 'docs'),                            # docs线程
        _key(10, 'readme.md') + file_rec(101, len(README), [(rb, 3)]),
    ]
    # 叶节点
    leaf = bytearray(BS)
    leaf[0:14] = struct.pack('<iibbH2x', 0, 0, -1, 1, len(recs))
    pos = 14
    offs = []
    for r in recs:
        leaf[pos:pos + len(r)] = r
        offs.append(pos)
        pos += len(r)
    for i, o in enumerate(offs):
        struct.pack_into('<H', leaf, BS - 2 * (i + 1), o)
    struct.pack_into('<H', leaf, BS - 2 * (len(recs) + 1), pos)          # 空闲偏移
    # 头节点
    head = bytearray(BS)
    head[0:14] = struct.pack('<iibbH2x', 1, 0, 1, 0, 3)
    bth = struct.pack('<HIIIIH', 1, 1, len(recs), 1, 1, BS) + \
        struct.pack('<H', 516) + struct.pack('<II', CATALOG_NODES, 0)
    head[14:14 + len(bth)] = bth
    # 头节点记录偏移: [BTHeaderRec, 128B user, 记录偏移数组自身]
    struct.pack_into('<H', head, BS - 2, 14)
    struct.pack_into('<H', head, BS - 4, 14 + 106)
    struct.pack_into('<H', head, BS - 6, 14 + 106 + 128)
    struct.pack_into('<H', head, BS - 8, 14 + 106 + 128 + 2)
    img[CATALOG_BLK * BS:CATALOG_BLK * BS + BS] = head
    img[(CATALOG_BLK + 1) * BS:(CATALOG_BLK + 2) * BS] = leaf
    with open(path, 'wb') as f:
        f.write(img)
    return {'hello': HELLO, 'readme': README, 'rb': rb}


def test_hfsplus(tmp):
    p = os.path.join(tmp, 'mac.img')
    facts = build_hfsplus(p)
    d = macfs.detect(p, 0)
    check('HFS+识别', d.get('kind') == 'hfs+' and d['block_size'] == BS and
          d['file_count'] == 2, str(d))
    with open(p, 'rb') as f:
        vol = macfs.Vol(f, 0)
        files, folders = vol.load()
    names = {fl['path']: fl for fl in files}
    check('文件列表', set(names) == {'hello.txt', 'docs/readme.md'},
          str(sorted(names)))
    check('文件大小', names['hello.txt']['size'] == len(facts['hello']) and
          names['docs/readme.md']['size'] == len(facts['readme']))
    with open(p, 'rb') as f:
        vol = macfs.Vol(f, 0)
        files, _ = vol.load()
        got = {fl['path']: vol.read_file(fl) for fl in files}
    check('hello提取逐字节', got['hello.txt'] == facts['hello'])
    check('嵌套readme提取逐字节', got['docs/readme.md'] == facts['readme'])


def test_overflow(tmp):
    p = os.path.join(tmp, 'mac.img')
    build_hfsplus(p)
    with open(p, 'rb') as f:
        vol = macfs.Vol(f, 0)
        files, _ = vol.load()
        fl = next(x for x in files if x['name'] == 'hello.txt')
        # 篡改size超出extent覆盖 → read_file诚实返回None
        fl['size'] = sum(c for _, c in fl['extents']) * vol.bs + 1
        check('extents溢出如实None', vol.read_file(fl) is None)


def test_apfs(tmp):
    p = os.path.join(tmp, 'apfs.img')
    b = bytearray(4096)
    b[24:28] = b'NXSB'                              # 对象type字段
    struct.pack_into('<I', b, 32, 4096)             # block_size
    struct.pack_into('<Q', b, 40, 1000)             # blocks
    open(p, 'wb').write(b)
    d = macfs.detect(p, 0)
    check('APFS识别', d.get('kind') == 'apfs' and d['block_size'] == 4096 and
          '未实现' in d['detail'], str(d))
    junk = os.path.join(tmp, 'junk.img')
    open(junk, 'wb').write(os.urandom(8192))
    check('非Mac卷拒绝', macfs.detect(junk, 0).get('kind') is None)


def main():
    tmp = tempfile.mkdtemp(prefix='macfs_')
    try:
        test_hfsplus(tmp)
        test_overflow(tmp)
        test_apfs(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print('\n%s: %d项失败' % ('总判定' if FAILS else '全绿', len(FAILS)), flush=True)
    sys.exit(1 if FAILS else 0)


if __name__ == '__main__':
    main()
