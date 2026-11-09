# -*- coding: utf-8 -*-
"""RAID重组测试(全合成):
A. 合成虚拟盘(MBR+exFAT魔数分区+填充) → RAID0 3盘乱序 → 检测+重建逐字节
B. RAID5 left-sym 3盘乱序(含校验) → 检测+重建逐字节
C. RAID5 降级(缺1盘) → XOR重建逐字节
D. 干净无结构盘 → 拒绝(打分低)
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

import raid

FAILS = []


def check(name, ok, detail=''):
    print(('PASS ' if ok else 'FAIL ') + name + (('  ' + detail) if detail else ''), flush=True)
    if not ok:
        FAILS.append(name)


def make_virtual(size=24 * 1024 * 1024, part_lba=128):
    """虚拟盘: MBR + 三分区(exFAT@128 / FAT16@640 / FAT32@5000) + 填充
    三分区落在不同RAID行上: 校验块伪装的'伪MBR'过不了多处魔数验证"""
    img = bytearray(size)
    img[510:512] = b'\x55\xaa'
    parts = ((part_lba, 512 - part_lba, 0x07, b'EXFAT   '),
             (640, 4360, 0x06, b'FAT16   '),
             (5000, (size // 512) - 5000, 0x0B, b'FAT32   '))
    for i, (lba, secs, ptype, magic) in enumerate(parts):
        e = bytearray(16)
        e[4] = ptype
        struct.pack_into('<II', e, 8, lba, secs)
        img[446 + 16 * i:462 + 16 * i] = e
    # 填充(注意: 切片赋值长度必须严格相等, 否则bytearray会变长/缩短!)
    rnd = random.Random(2026)
    pattern = bytes(rnd.randrange(1, 256) for _ in range(4080))
    for off in range(1024 * 512, size, 4096):
        img[off:off + 4096] = struct.pack('<Q', off) + pattern + struct.pack('<Q', off)
    # 分区魔数最后写(防被填充覆盖)
    for lba, _, _, magic in parts:
        img[lba * 512 + 3:lba * 512 + 11] = magic
        img[lba * 512 + 510:lba * 512 + 512] = b'\x55\xaa'
    return bytes(img)


def split_raid0(virt, n, chunk, order):
    """按盘序order(第i块虚拟盘位=成员盘order[i])切RAID0"""
    disks = [bytearray() for _ in range(n)]
    nch = len(virt) // chunk
    for c in range(nch):
        slot = c % n
        disks[order[slot]].extend(virt[c * chunk:(c + 1) * chunk])
    return [bytes(d) for d in disks]


def split_raid5(virt, n, chunk, order, rot='left-sym'):
    """成员盘内容; order同RAID0语义(虚拟槽位→成员盘); 每盘恰好nrows个chunk"""
    nrows = (len(virt) // chunk) // (n - 1)
    disks = [bytearray(b'\x00' * (chunk * nrows)) for _ in range(n)]
    for r in range(nrows):
        pd = raid._parity_disk(rot, n, r)
        parity = bytearray(chunk)
        for j in range(n - 1):
            vc = r * (n - 1) + j
            data = virt[vc * chunk:(vc + 1) * chunk]
            if rot.endswith('sym'):
                slot = (pd + 1 + j) % n
            else:
                slot = j if j < pd else j + 1
            m = order[slot]
            off = r * chunk
            disks[m][off:off + chunk] = data
            for i in range(chunk):
                parity[i] ^= data[i]
        disks[order[pd]][r * chunk:(r + 1) * chunk] = bytes(parity)
    return [bytes(d) for d in disks]


def test_raid0(tmp):
    virt = make_virtual()
    n, chunk = 3, 128 * 1024
    true_order = [2, 0, 1]           # 故意乱序
    disks = split_raid0(virt, n, chunk, true_order)
    paths = []
    for i, d in enumerate(disks):
        p = os.path.join(tmp, 'r0_%d.img' % i)
        open(p, 'wb').write(d)
        paths.append(p)
    cfg = raid.detect(paths)
    check('RAID0检测: 级别+条带', cfg.get('level') == 0 and cfg.get('stripe_kb') == 128,
          str({k: cfg.get(k) for k in ('level', 'stripe_kb', 'score', 'error')}))
    check('RAID0检测: 盘序还原', cfg.get('order') == true_order,
          '%s vs %s' % (cfg.get('order'), true_order))
    out = os.path.join(tmp, 'r0.raw')
    raid.rebuild(paths, cfg, out)
    check('RAID0重建逐字节', open(out, 'rb').read() == virt)


def test_raid5(tmp):
    virt = make_virtual()
    n, chunk = 3, 64 * 1024
    true_order = [1, 2, 0]
    disks = split_raid5(virt, n, chunk, true_order)
    paths = []
    for i, d in enumerate(disks):
        p = os.path.join(tmp, 'r5_%d.img' % i)
        open(p, 'wb').write(d)
        paths.append(p)
    cfg = raid.detect(paths)
    check('RAID5检测: 级别+条带', cfg.get('level') == 5 and cfg.get('stripe_kb') == 64,
          str({k: cfg.get(k) for k in ('level', 'stripe_kb', 'score', 'error')}))
    check('RAID5检测: 布局+盘序', cfg.get('rotation') == 'left-sym' and
          cfg.get('order') == true_order,
          '%s %s vs %s' % (cfg.get('rotation'), cfg.get('order'), true_order))
    out = os.path.join(tmp, 'r5.raw')
    raid.rebuild(paths, cfg, out)
    check('RAID5重建逐字节', open(out, 'rb').read() == virt)


def test_degraded(tmp):
    virt = make_virtual()
    n, chunk = 3, 256 * 1024
    true_order = [0, 2, 1]
    disks = split_raid5(virt, n, chunk, true_order)
    paths = []
    for i, d in enumerate(disks):
        p = os.path.join(tmp, 'd5_%d.img' % i)
        open(p, 'wb').write(d)
        paths.append(p)
    # 用全成员检测出配置, 然后抽走一块盘(置None)重建
    cfg = raid.detect(paths)
    check('降级前提: 检测成功', cfg.get('level') == 5)
    members = list(paths)
    members[true_order.index(2)] = None      # 抽走虚拟槽位2对应的成员盘
    out = os.path.join(tmp, 'd5.raw')
    raid.rebuild(members, cfg, out)
    got = open(out, 'rb').read()
    check('RAID5降级XOR重建逐字节', got == virt,
          'len=%d/%d' % (len(got), len(virt)))


def test_reject(tmp):
    p = [os.path.join(tmp, 'junk%d.img' % i) for i in range(2)]
    rnd = random.Random(9)
    for x in p:
        open(x, 'wb').write(bytes(rnd.randrange(256) for _ in range(2 * 1024 * 1024)))
    cfg = raid.detect(p)
    check('乱数据拒绝', 'error' in cfg or cfg.get('score', 0) < 10,
          str(cfg)[:80])


def main():
    tmp = tempfile.mkdtemp(prefix='raid_')
    try:
        test_raid0(tmp)
        test_raid5(tmp)
        test_degraded(tmp)
        test_reject(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print('\n%s: %d项失败' % ('总判定' if FAILS else '全绿', len(FAILS)), flush=True)
    sys.exit(1 if FAILS else 0)


if __name__ == '__main__':
    main()
