# -*- coding: utf-8 -*-
"""allocmap单元+集成测试:
A. 合成FAT32/FAT16/exFAT镜像 → 空闲区间逐字节精确断言
B. merge_extents合并策略
C. VHD夹具(ntfs/exfat) → MBR→分区偏移→结构断言
D. 种文件冒烟: exfat.vhd副本的空闲区种入jpg → carve_free → 找回且hash一致
"""
import os
import sys
import struct
import shutil
import hashlib
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'engine'))

import allocmap

FIX = os.path.join(HERE, 'fixtures')
FAILS = []


def check(name, ok, detail=''):
    print(('PASS ' if ok else 'FAIL ') + name + (('  ' + detail) if detail else ''), flush=True)
    if not ok:
        FAILS.append(name)


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


# ---------- A. 合成镜像 ----------

def build_fat32(path):
    bps, spc, reserved, nfats = 512, 1, 32, 2
    tot = 68000          # 簇数需≥65525才会被判为FAT32
    # FAT区要装下 count+2 个4字节项; fatsz与簇数互相依赖, 迭代收敛
    fatsz = 524
    for _ in range(3):
        data_start = reserved + nfats * fatsz
        count = tot - data_start
        fatsz = max(fatsz, -(-(count + 2) * 4 // bps))
    data_start = reserved + nfats * fatsz            # 1072
    count = (tot - data_start) // spc                # 65718
    img = open(path, 'wb')
    boot = bytearray(512)
    boot[0:3] = b'\xEB\x3C\x90'
    boot[3:11] = b'MSDOS5.0'
    struct.pack_into('<H', boot, 11, bps)
    boot[13] = spc
    struct.pack_into('<H', boot, 14, reserved)
    boot[16] = nfats
    struct.pack_into('<H', boot, 22, 0)              # FAT16 size = 0
    struct.pack_into('<I', boot, 36, fatsz)          # FAT32 size
    struct.pack_into('<H', boot, 19, 0)
    struct.pack_into('<I', boot, 32, tot)
    struct.pack_into('<I', boot, 44, 2)              # root cluster
    boot[510:512] = b'\x55\xAA'
    img.write(boot)
    fat = bytearray(fatsz * bps)
    def sete(n, v):
        fat[n * 4:n * 4 + 4] = v.to_bytes(4, 'little')
    sete(0, 0xFFFFFFF8)
    sete(1, 0xFFFFFFFF)
    for n in (2, 3, 6, 8):
        sete(n, 0xFFFFFFFF)                          # 已分配: 簇2,3,6,8
    img.seek(reserved * bps)
    img.write(fat)
    img.seek((reserved + fatsz) * bps)
    img.write(fat)                                   # 第二份FAT
    img.truncate(tot * bps)
    img.close()
    return data_start * bps, count, bps * spc


def build_fat16(path):
    bps, spc, reserved, nfats, fatsz, root_ent = 512, 1, 2, 2, 40, 512
    tot = 8192
    root_secs = (root_ent * 32 + bps - 1) // bps      # 32
    data_start = reserved + nfats * fatsz + root_secs  # 114
    count = (tot - data_start) // spc                 # 8078
    img = open(path, 'wb')
    boot = bytearray(512)
    boot[0:3] = b'\xEB\x3C\x90'
    boot[3:11] = b'MSDOS5.0'
    struct.pack_into('<H', boot, 11, bps)
    boot[13] = spc
    struct.pack_into('<H', boot, 14, reserved)
    boot[16] = nfats
    struct.pack_into('<H', boot, 17, root_ent)
    struct.pack_into('<H', boot, 19, tot)
    struct.pack_into('<H', boot, 22, fatsz)
    struct.pack_into('<H', boot, 38, 0x29)
    boot[510:512] = b'\x55\xAA'
    img.write(boot)
    fat = bytearray(fatsz * bps)
    def sete(n, v):
        fat[n * 2:n * 2 + 2] = v.to_bytes(2, 'little')
    sete(0, 0xFFF8)
    sete(1, 0xFFFF)
    for n in (2, 3, 6, 8):
        sete(n, 0xFFFF)
    img.seek(reserved * bps)
    img.write(fat)
    img.seek((reserved + fatsz) * bps)
    img.write(fat)
    img.truncate(tot * bps)
    img.close()
    return data_start * bps, count, bps * spc


def build_exfat(path):
    sec_shift, clus_shift = 9, 3                      # 512B扇区, 4KB簇
    sec, cs = 1 << sec_shift, 1 << (sec_shift + clus_shift)
    fat_off, fat_len, heap_off, count = 32, 64, 160, 8000
    total_sec = heap_off + (count * cs) // sec + fat_len + 64
    img = open(path, 'wb')
    boot = bytearray(512)
    boot[0:3] = b'\xEB\x76\x90'
    boot[3:11] = b'EXFAT   '
    struct.pack_into('<Q', boot, 0x48, total_sec)     # VolumeLength
    struct.pack_into('<I', boot, 0x50, fat_off)
    struct.pack_into('<I', boot, 0x54, fat_len)
    struct.pack_into('<I', boot, 0x58, heap_off)
    struct.pack_into('<I', boot, 0x5C, count)
    struct.pack_into('<I', boot, 0x60, 2)             # root dir cluster
    boot[0x6C] = sec_shift
    boot[0x6D] = clus_shift
    boot[0x6E] = 1                                    # NumberOfFats
    boot[510:512] = b'\x55\xAA'
    img.write(boot)
    fat = bytearray(fat_len * sec)
    def sete(n, v):
        fat[n * 4:n * 4 + 4] = v.to_bytes(4, 'little')
    sete(0, 0xFFFFFFF8)
    sete(1, 0xFFFFFFFF)
    for n in (2, 3, 6, 8):
        sete(n, 0xFFFFFFFF)
    img.seek(fat_off * sec)
    img.write(fat)
    img.truncate(total_sec * sec)
    img.close()
    return heap_off * sec, count, cs


def expect_pattern(tag, res, data_base, count, cs):
    """簇{2,3,6,8}已分配 → 期望空闲段 idx[2,4) [5,6) [7,count)"""
    exp = [(data_base + 2 * cs, data_base + 4 * cs),
           (data_base + 5 * cs, data_base + 6 * cs),
           (data_base + 7 * cs, data_base + count * cs)]
    ok = res['ok'] and res['extents'] == exp
    check(tag + ' 空闲区间精确', ok,
          '' if ok else 'got=%s' % (res.get('extents'),))
    check(tag + ' 计数', res['free_clusters'] == count - 4 and res['allocated_clusters'] == 4,
          'free=%s alloc=%s count=%d' % (res.get('free_clusters'), res.get('allocated_clusters'), count))
    check(tag + ' bytes_free', res['bytes_free'] == (count - 4) * cs)


def test_synthetic(tmp):
    p32 = os.path.join(tmp, 'f32.img')
    base, count, cs = build_fat32(p32)
    res = allocmap.analyze(p32, 0, min_bytes=0)
    check('FAT32识别', res.get('ok') and res.get('fs') == 'fat32', str(res.get('fs') or res.get('error')))
    if res.get('ok'):
        expect_pattern('FAT32', res, base, count, cs)

    p16 = os.path.join(tmp, 'f16.img')
    base, count, cs = build_fat16(p16)
    res = allocmap.analyze(p16, 0, min_bytes=0)
    check('FAT16识别', res.get('ok') and res.get('fs') == 'fat16', str(res.get('fs') or res.get('error')))
    if res.get('ok'):
        expect_pattern('FAT16', res, base, count, cs)

    pex = os.path.join(tmp, 'exf.img')
    base, count, cs = build_exfat(pex)
    res = allocmap.analyze(pex, 0, min_bytes=0)
    check('exFAT识别', res.get('ok') and res.get('fs') == 'exfat', str(res.get('fs') or res.get('error')))
    if res.get('ok'):
        expect_pattern('exFAT', res, base, count, cs)

    # min_bytes过滤: 丢弃比4KB小的段(FAT32的2簇段=1024B, 1簇段=512B)
    res = allocmap.analyze(p32, 0, min_bytes=4096)
    only_big = [e for e in res['extents'] if e[1] - e[0] < 4096]
    check('min_bytes过滤', res.get('ok') and not only_big and res['extents'],
          str(res.get('extents')))

    # 非FS数据 → 拒绝
    junk = os.path.join(tmp, 'junk.img')
    open(junk, 'wb').write(b'\x00' * 100)
    res = allocmap.analyze(junk, 0)
    check('非FS拒绝', not res.get('ok'), str(res.get('error'))[:60])


# ---------- B. merge_extents ----------

def test_merge():
    ex = [(0, 10), (12, 20), (30, 40), (41, 50)]
    got = allocmap.merge_extents(ex, max_ranges=2)
    check('merge_extents', got == [(0, 20), (30, 50)], str(got))
    got = allocmap.merge_extents([(0, 10), (100, 200)], max_ranges=24)
    check('merge_extents不合并', got == [(0, 10), (100, 200)], str(got))
    many = [(i * 3, i * 3 + 1) for i in range(100)]
    got = allocmap.merge_extents(many, max_ranges=8)
    check('merge_extents收敛到上限', len(got) <= 8 and got[0][0] == 0 and got[-1][1] == 298,
          'n=%d' % len(got))


# ---------- C/D. VHD夹具 ----------

def mbr_part(path):
    """VHD(MBR)→(起始LBA, 扇区数) or None"""
    with open(path, 'rb') as f:
        mbr = f.read(512)
    if len(mbr) < 512 or mbr[510:512] != b'\x55\xaa':
        return None
    for i in range(4):
        e = mbr[446 + 16 * i:462 + 16 * i]
        if e[4] != 0:
            return struct.unpack_from('<I', e, 8)[0], struct.unpack_from('<I', e, 12)[0]
    return None


def test_vhd(tag, fsname):
    vhd = os.path.join(FIX, tag + '.vhd')
    if not os.path.exists(vhd):
        check('%s夹具存在' % tag, False, '缺文件')
        return None
    part = mbr_part(vhd)
    check('%s MBR分区' % tag, bool(part), str(part))
    if not part:
        return None
    lba, secs = part
    res = allocmap.analyze(vhd, lba * 512)
    check('%s %s识别' % (tag, fsname), res.get('ok') and res.get('fs') == fsname,
          str(res.get('fs') or res.get('error')))
    if not res.get('ok'):
        return None
    c = res['cluster_size']
    check('%s 计数自洽' % tag,
          res['free_clusters'] + res['allocated_clusters'] == res['total_clusters']
          and 0 < res['free_clusters'] < res['total_clusters']
          and res['bytes_free'] == res['free_clusters'] * c,
          'free=%s alloc=%s total=%s cs=%s' % (res['free_clusters'], res['allocated_clusters'],
                                               res['total_clusters'], c))
    end = (lba + secs) * 512
    ok = all(lba * 512 <= s < e <= end for s, e in res['extents'])
    sorted_np = all(res['extents'][i][1] <= res['extents'][i + 1][0]
                    for i in range(len(res['extents']) - 1))
    check('%s 区间在分区内且不重叠' % tag, ok and sorted_np,
          'n=%d first=%s' % (len(res['extents']), res['extents'][0] if res['extents'] else None))
    return res, lba


def test_carve_smoke(tmp, exfat_res, lba):
    """exfat.vhd副本空闲区种入jpg → carve_free 找回"""
    vhd = os.path.join(FIX, 'exfat.vhd')
    jpg_path = os.path.join(FIX, 'src', '照片', '壁纸.jpg')
    if not (os.path.exists(vhd) and os.path.exists(jpg_path)):
        check('种文件冒烟夹具', False, '缺vhd或jpg')
        return
    jpg = open(jpg_path, 'rb').read()
    plant = os.path.join(tmp, 'plant.vhd')
    shutil.copy(vhd, plant)
    # 找一段足够大的空闲区种文件
    ext = next((e for e in exfat_res['extents'] if e[1] - e[0] > len(jpg) + 262144), None)
    check('存在足够大空闲段', bool(ext), str(ext))
    if not ext:
        return
    off = ext[0] + 131072
    with open(plant, 'r+b') as f:
        f.seek(off)
        f.write(jpg)
    # 种入后分配图不变(FAT未动), 种入点仍在空闲区间内
    res2 = allocmap.analyze(plant, lba * 512)
    inside = res2.get('ok') and any(s <= off and off + len(jpg) <= e for s, e in res2['extents'])
    check('种入点落在未分配区间', bool(inside))
    # carve_free 逐段雕刻
    import pipeline
    outdir = os.path.join(tmp, 'carved')
    job = pipeline.start_job('carve_free', {'source': plant, 'partition_lba': lba * 512,
                                            'out': outdir})
    t0 = time.time()
    while job['state'] == 'running' and time.time() - t0 < 300:
        time.sleep(0.5)
    check('carve_free任务完成', job['state'] == 'done',
          '%s %s' % (job['state'], (job.get('error') or '')[:120]))
    if job['state'] != 'done':
        return
    result = job.get('result') or {}
    check('carve_free找到文件', (result.get('files_found') or 0) >= 1, str(result.get('files_found')))
    want = sha256_bytes(jpg)
    hashes = set()
    for dirpath, _, files in os.walk(outdir):
        for fn in files:
            with open(os.path.join(dirpath, fn), 'rb') as f:
                hashes.add(sha256_bytes(f.read()))
    check('种入jpg被完整找回(hash一致)', want in hashes,
          'carved_files=%d' % len(hashes))


def main():
    tmp = tempfile.mkdtemp(prefix='allocmap_')
    try:
        test_synthetic(tmp)
        test_merge()
        nt = test_vhd('ntfs', 'ntfs')
        ex = test_vhd('exfat', 'exfat')
        if ex:
            test_carve_smoke(tmp, ex[0], ex[1])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print('\n%s: %d项失败' % ('总判定' if FAILS else '全绿', len(FAILS)), flush=True)
    sys.exit(1 if FAILS else 0)


if __name__ == '__main__':
    main()
