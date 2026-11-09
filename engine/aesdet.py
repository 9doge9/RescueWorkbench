# -*- coding: utf-8 -*-
"""加密卷/加密区检测 — 判断"数据还在但被锁"的场景
能力边界(诚实): 只做检测和线索定位; BitLocker解锁(密钥/恢复码→解密镜像)在第三梯队。
覆盖: BitLocker/BitLocker-To-Go卷头、LUKS(Linux)、FileVault特征、
      高熵区扫描(疑似加密/压缩数据分布图)。
"""
import math
import os
import struct

# 卷头签名(相对各自文件系统起始的偏移)
BITLOCKER_SIG = b'-FVE-FS-'
BITLOCKER_OFFSET = 3
LUKS_SIG = b'LUKS\xba\xbe'
LUKS_OFFSET = 0
# BitLocker To Go: FAT格式的BPB + FVE-FS标记在0x0A附近
BTG_MARKER = b'FVE-FS'


def detect_volume(source, offset=0):
    """读卷起始扇区 → {'kind': ..., 'detail': str} kind=None表示未检出加密卷"""
    try:
        with open(source, 'rb') as f:
            f.seek(offset)
            sec = f.read(512)
    except OSError as e:
        return {'kind': None, 'error': str(e)}
    if len(sec) < 512:
        return {'kind': None, 'detail': '扇区不足'}
    if sec[BITLOCKER_OFFSET:BITLOCKER_OFFSET + 8] == BITLOCKER_SIG:
        ver = struct.unpack_from('<H', sec, 11)[0]
        return {'kind': 'bitlocker',
                'detail': 'BitLocker卷 (FVE版本%d) — 卷结构完好, 数据加密存放; '
                          '解锁(密码/恢复密钥)在第三梯队' % ver}
    if sec[LUKS_OFFSET:LUKS_OFFSET + 6] == LUKS_SIG:
        ver = '%d.%d' % (sec[6], sec[7])
        return {'kind': 'luks',
                'detail': 'LUKS卷 (v%s, Linux) — 解锁在第三梯队' % ver}
    if BTG_MARKER in sec[:32] and sec[510:512] == b'\x55\xaa':
        return {'kind': 'bitlocker-togo',
                'detail': 'BitLocker To Go (U盘加密) — 解锁在第三梯队'}
    # 常见文件系统magic → 非加密
    for sig, name in ((b'NTFS    ', 'NTFS'), (b'EXFAT   ', 'exFAT'),
                      (b'FAT32   ', 'FAT32'), (b'FAT16   ', 'FAT16'), (b'FAT12   ', 'FAT12')):
        if sec[3:11] == sig:
            return {'kind': None, 'detail': name + ' 明文卷'}
    if sec[510:512] == b'\x55\xaa' and any(sec[446 + 16 * i + 4] != 0 for i in range(4)):
        return {'kind': None, 'detail': 'MBR分区盘(未加密) — 对各分区分别检测更准'}
    return {'kind': None, 'detail': '未识别(可能全盘加密或损坏) — 建议跑高熵扫描'}


def _entropy(b):
    """字节熵(bytes.count(int)是C实现, 比逐字节循环快一个量级)"""
    if not b:
        return 0.0
    n = len(b)
    e = 0.0
    for i in range(256):
        c = b.count(i)
        if c:
            p = c / n
            e -= p * math.log2(p)
    return e


def entropy_map(source, start=0, end=None, chunk=1024 * 1024, on_progress=None):
    """逐MB熵扫描 → {'bins':[(offset, entropy)], 'high_regions':[(s,e)],
                     'high_bytes': n, 'total_bytes': n, 'max': e}
    判读: 熵>7.5/字节 ≈ 加密或已压缩; NTFS压缩卷/exFAT本身低熵"""
    if end is None:
        end = os.path.getsize(source)
    bins = []
    high = []
    cur_hi_start = None
    pos = start
    sample = 128 * 1024                 # 每MB抽前128KB估熵(统计性质, 足够准)
    with open(source, 'rb') as f:
        while pos < end:
            if on_progress and pos % (256 * 1024 * 1024) < chunk:
                on_progress({'event': 'scan_progress',
                             'bytes_scanned': pos, 'bytes_total': end})
            f.seek(pos)
            b = f.read(chunk)
            if not b:
                break
            e = _entropy(b[:sample])
            bins.append((pos, round(e, 2)))
            if e > 7.5:
                if cur_hi_start is None:
                    cur_hi_start = pos
            else:
                if cur_hi_start is not None:
                    high.append((cur_hi_start, pos))
                    cur_hi_start = None
            pos += len(b)
    if cur_hi_start is not None:
        high.append((cur_hi_start, end))
    total = sum(e - s for s, e in high)
    return {'bins': bins[:20000], 'high_regions': high[:1000],
            'high_bytes': total, 'total_bytes': end - start,
            'max': max((e for _, e in bins), default=0.0)}
