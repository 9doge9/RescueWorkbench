# -*- coding: utf-8 -*-
"""未分配空间映射 — carving预筛(TSK blkls思路)
NTFS: $Boot BPB → MFT record0 runs → record6($Bitmap) $DATA → 位图
exFAT / FAT32 / FAT16: BPB → FAT表 → 值为0的项=空闲簇
输出: 空闲簇合并成连续字节区间(含分区偏移的绝对位置), 供 carve --start/--end 逐段调用。
收益: carving只扫未分配区域 → 快 + 降误报(活文件区域不重复挖)。
局限: 文件系统结构必须可读; MFT/FAT被毁时调用方回退全范围雕刻。
"""
import struct

import mft_scan


# ---------- 对外入口 ----------

def analyze(source, part_lba=0, min_bytes=4096, on_progress=None):
    """分析镜像/设备的未分配空间
    source: 镜像文件或 \\.\\PhysicalDriveX / 卷设备
    part_lba: 分区起始字节偏移(0=源本身就是一个分区/无分区表)
    min_bytes: 小于此长度的空闲段丢弃(太短放不下目标文件)
    → {'ok', 'fs', 'cluster_size', 'total_clusters', 'free_clusters',
       'allocated_clusters', 'bytes_free', 'extents':[(s,e)绝对字节], 'extents_raw', 'error'}
    """
    try:
        with open(source, 'rb') as f:
            f.seek(part_lba)
            boot = f.read(512)
            if len(boot) < 512:
                return {'ok': False, 'error': 'boot sector读取失败(偏移%d)' % part_lba}
            fs = _detect(boot)
            if fs == 'ntfs':
                return _ntfs(f, boot, part_lba, min_bytes, on_progress)
            if fs == 'exfat':
                return _exfat(f, boot, part_lba, min_bytes, on_progress)
            if fs in ('fat32', 'fat16'):
                return _fat(f, boot, part_lba, fs, min_bytes, on_progress)
            return {'ok': False, 'error': '无法识别文件系统(支持NTFS/exFAT/FAT32/FAT16)'}
    except OSError as e:
        return {'ok': False, 'error': str(e)}


def merge_extents(extents, max_ranges=24):
    """把过碎的空闲段按最小间隔合并到不超过max_ranges段
    (carve是逐段起子进程, 段数失控会拖死; 合并代价是少量已分配区域被顺带扫一遍)"""
    ex = sorted(extents)
    while len(ex) > max_ranges:
        gi = min(range(len(ex) - 1), key=lambda i: ex[i + 1][0] - ex[i][1])
        ex[gi] = (ex[gi][0], ex[gi + 1][1])
        del ex[gi + 1]
    return ex


# ---------- 文件系统识别 ----------

def _detect(boot):
    if boot[3:11] == b'EXFAT   ':
        return 'exfat'
    if boot[3:11] == b'NTFS    ':
        return 'ntfs'
    # FAT族: BPB字段合法性筛查
    if boot[510:512] != b'\x55\xaa':
        return None
    bps = struct.unpack_from('<H', boot, 11)[0]
    spc = boot[13]
    if bps not in (512, 1024, 2048, 4096) or not spc or spc & (spc - 1):
        return None
    reserved = struct.unpack_from('<H', boot, 14)[0]
    nfats = boot[16]
    if not reserved or not nfats:
        return None
    tot = struct.unpack_from('<H', boot, 19)[0] or struct.unpack_from('<I', boot, 32)[0]
    root_ent = struct.unpack_from('<H', boot, 17)[0]
    fatsz = struct.unpack_from('<H', boot, 22)[0] or struct.unpack_from('<I', boot, 36)[0]
    if not tot or not fatsz:
        return None
    root_secs = (root_ent * 32 + bps - 1) // bps
    clusters = (tot - (reserved + nfats * fatsz + root_secs)) // spc
    if clusters >= 65525:
        return 'fat32'
    if clusters >= 4085:
        return 'fat16'
    return None  # FAT12不支持


# ---------- 通用: 空闲簇runs → 结果 ----------

def _mk_result(fs, cluster_size, count, free, runs_abs, min_bytes):
    """runs_abs: [(起,止)绝对字节] — min过滤后即extents"""
    extents = [(s, e) for s, e in runs_abs if e - s >= min_bytes]
    return {
        'ok': True, 'fs': fs, 'cluster_size': cluster_size,
        'total_clusters': count, 'free_clusters': free,
        'allocated_clusters': count - free,
        'bytes_free': free * cluster_size,
        'extents': extents, 'extents_raw': len(runs_abs),
    }


# ---------- NTFS: $Bitmap ----------

def _ntfs(f, boot, base, min_bytes, on_progress):
    bps = struct.unpack_from('<H', boot, 11)[0]
    spc = boot[13]
    total_sec = struct.unpack_from('<Q', boot, 40)[0]
    mft_lcn = struct.unpack_from('<Q', boot, 48)[0]
    rec_size_b = struct.unpack_from('<b', boot, 64)[0]
    cs = spc * bps
    if not cs or not total_sec:
        return {'ok': False, 'error': 'NTFS BPB字段异常'}
    rec_size = rec_size_b * cs if rec_size_b > 0 else 1 << (-rec_size_b)
    total_clusters = total_sec // spc

    # record0 → MFT自身runs (MFT可能碎片化)
    f.seek(base + mft_lcn * cs)
    rec0 = mft_scan.parse_record(f.read(rec_size))
    if not rec0 or not rec0.get('runs'):
        return {'ok': False, 'error': 'MFT record0解析失败'}
    # 定位record6($Bitmap)
    off = _record_offset(rec0['runs'], cs, 6, rec_size, base)
    if off is None:
        return {'ok': False, 'error': 'record6($Bitmap)不在MFT runs内'}
    f.seek(off)
    rec6 = mft_scan.parse_record(f.read(rec_size))
    if not rec6:
        return {'ok': False, 'error': 'record6($Bitmap)解析失败'}

    need = (total_clusters + 7) // 8
    if rec6.get('resident_data') is not None:
        bmp = bytes(rec6['resident_data'][:need])
    else:
        if not rec6.get('runs'):
            return {'ok': False, 'error': '$Bitmap无数据runs'}
        bmp = bytearray()
        for vcn, length, lcn in rec6['runs']:
            if len(bmp) >= need:
                break
            if lcn is None:
                bmp += b'\x00' * (length * cs)
            else:
                f.seek(base + lcn * cs)
                bmp += f.read(length * cs)
        bmp = bytes(bmp[:need])
    if not bmp:
        return {'ok': False, 'error': '$Bitmap为空'}

    # 遍历位图 → 空闲簇runs (整字节快路径)
    free_runs = []
    free = 0
    cur_start = None
    cur_len = 0
    for i in range(total_clusters):
        b = bmp[i >> 3]
        if b == 0x00:
            isfree = True
        elif b == 0xFF:
            isfree = False
        else:
            isfree = not (b >> (i & 7)) & 1
        if isfree:
            free += 1
            if cur_len and cur_start + cur_len == i:
                cur_len += 1
            else:
                if cur_len:
                    free_runs.append((cur_start, cur_len))
                cur_start, cur_len = i, 1
        if on_progress and i % 4194304 == 0 and i:
            on_progress({'event': 'scan_progress', 'clusters': i, 'clusters_total': total_clusters})
    if cur_len:
        free_runs.append((cur_start, cur_len))
    # 簇号i的字节 = base + i*cs
    runs_abs = [(base + s * cs, base + (s + l) * cs) for s, l in free_runs]
    return _mk_result('ntfs', cs, total_clusters, free, runs_abs, min_bytes)


def _record_offset(runs, cluster_size, recno, rec_size, base):
    """MFT记录号→绝对字节偏移(沿record0的runs)"""
    target = recno * rec_size
    pos = 0
    for vcn, length, lcn in runs:
        n = length * cluster_size
        if pos <= target < pos + n:
            if lcn is None:
                return None
            return base + lcn * cluster_size + (target - pos)
        pos += n
    return None


# ---------- exFAT ----------

def _exfat(f, boot, base, min_bytes, on_progress):
    sec = 1 << boot[0x6C]
    cs = sec << boot[0x6D]
    fat_off = struct.unpack_from('<I', boot, 0x50)[0]
    fat_len = struct.unpack_from('<I', boot, 0x54)[0]
    heap_off = struct.unpack_from('<I', boot, 0x58)[0]
    count = struct.unpack_from('<I', boot, 0x5C)[0]
    if not (cs and fat_len and count):
        return {'ok': False, 'error': 'exFAT BPB字段异常'}
    data_base = base + heap_off * sec
    return _scan_fat(f, base + fat_off * sec, fat_len * sec, 4, count, data_base, cs,
                     'exfat', min_bytes, on_progress)


# ---------- FAT32 / FAT16 ----------

def _fat(f, boot, base, kind, min_bytes, on_progress):
    bps = struct.unpack_from('<H', boot, 11)[0]
    spc = boot[13]
    reserved = struct.unpack_from('<H', boot, 14)[0]
    nfats = boot[16]
    root_ent = struct.unpack_from('<H', boot, 17)[0]
    tot = struct.unpack_from('<H', boot, 19)[0] or struct.unpack_from('<I', boot, 32)[0]
    fatsz = struct.unpack_from('<H', boot, 22)[0] or struct.unpack_from('<I', boot, 36)[0]
    if not (bps and spc and tot and fatsz):
        return {'ok': False, 'error': 'FAT BPB字段异常'}
    root_secs = (root_ent * 32 + bps - 1) // bps
    data_start_sec = reserved + nfats * fatsz + root_secs
    count = (tot - data_start_sec) // spc
    esz = 4 if kind == 'fat32' else 2
    data_base = base + data_start_sec * bps
    return _scan_fat(f, base + reserved * bps, fatsz * bps, esz, count, data_base,
                     spc * bps, kind, min_bytes, on_progress)


# ---------- FAT表扫描(项0,1是保留项: 媒体描述符/脏标志, 非数据簇) ----------

def _scan_fat(f, fat_off, fat_bytes, esz, count, data_base, cs, fs, min_bytes, on_progress):
    if count <= 0:
        return {'ok': False, 'error': '无数据簇'}
    free_runs = []
    free = 0
    cur_start = None
    cur_len = 0
    idx = 0
    pos = esz * 2          # 跳过保留项0/1 —— 数据簇从FAT项2开始
    chunk_size = 4 * 1024 * 1024
    while pos < fat_bytes and idx < count:
        f.seek(fat_off + pos)
        chunk = f.read(min(chunk_size, fat_bytes - pos))
        if not chunk:
            break
        pos += len(chunk)
        for i in range(0, len(chunk) - esz + 1, esz):
            if idx >= count:
                break
            v = int.from_bytes(chunk[i:i + esz], 'little')
            if v == 0:
                free += 1
                if cur_len and cur_start + cur_len == idx:
                    cur_len += 1
                else:
                    if cur_len:
                        free_runs.append((cur_start, cur_len))
                    cur_start, cur_len = idx, 1
            idx += 1
        if on_progress:
            on_progress({'event': 'scan_progress', 'clusters': idx, 'clusters_total': count})
    if cur_len:
        free_runs.append((cur_start, cur_len))
    # 簇idx(0基)的字节 = data_base + idx*cs
    runs_abs = [(data_base + s * cs, data_base + (s + l) * cs) for s, l in free_runs]
    return _mk_result(fs, cs, count, free, runs_abs, min_bytes)
