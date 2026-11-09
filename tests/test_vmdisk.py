# -*- coding: utf-8 -*-
"""vmdisk测试:
A. fixed VHD夹具识别(exfat/ntfs.vhd): kind=vhd-fixed, 虚拟容量=file-512, 免展开
B. 合成dynamic VHD(从exfat.vhd数据区打包成2MB稀疏块) → 识别+展开 → 逐字节SHA一致
C. 合成带洞dynamic VHD → 洞=零、非洞块与原数据一致
D. 合成VMDK描述文件+flat extent → 识别+直用exten路径
E. 合成sparse VMDK(KDMV+GD+GT+grain) → 展开后grain位置内容一致、洞为零
F. VHDX magic → 识别但如实标注不支持; fixed展开 → 报错信息正确
"""
import os
import sys
import struct
import hashlib
import tempfile
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'engine'))

import vmdisk

FIX = os.path.join(HERE, 'fixtures')
FAILS = []


def check(name, ok, detail=''):
    print(('PASS ' if ok else 'FAIL ') + name + (('  ' + detail) if detail else ''), flush=True)
    if not ok:
        FAILS.append(name)


def sha_file(p, skip_tail=0, limit=None):
    h = hashlib.sha256()
    n = 0
    with open(p, 'rb') as f:
        while True:
            b = f.read(4 * 1024 * 1024)
            if not b:
                break
            n += len(b)
            if skip_tail and n > skip_tail:
                b = b[:skip_tail - (n - len(b))]
            h.update(b)
    return h.hexdigest()


def vhd_footer(virtual, dtype, data_offset):
    f = bytearray(512)
    f[0:8] = b'conectix'
    struct.pack_into('>I', f, 0xC, 0x00010000)
    struct.pack_into('>Q', f, 0x10, data_offset)
    struct.pack_into('>Q', f, 0x28, virtual)
    struct.pack_into('>Q', f, 0x30, virtual)
    struct.pack_into('>I', f, 0x3C, dtype)
    f[0x44:0x54] = bytes(range(16))          # UUID
    return bytes(f)


def build_dynamic_vhd(path, data, block_size=2 * 1024 * 1024, holes=()):
    """数据区 → dynamic VHD (footer@0 + cxsparse@512 + BAT + blocks + footer尾)"""
    virtual = len(data)
    n_blocks = (virtual + block_size - 1) // block_size
    bat_off = 512 + 1024
    bitmap_len = ((block_size // 512 + 7) // 8 + 511) // 512 * 512
    block_area = (bat_off + n_blocks * 4 + 511) // 512 * 512   # BAT条目按扇区对齐
    with open(path, 'wb') as f:
        f.write(vhd_footer(virtual, 3, 512))                 # footer副本@0
        dh = bytearray(1024)
        dh[0:8] = b'cxsparse'
        struct.pack_into('>Q', dh, 8, 0xFFFFFFFFFFFFFFFF)
        struct.pack_into('>Q', dh, 16, bat_off)
        struct.pack_into('>I', dh, 24, 1)
        struct.pack_into('>I', dh, 28, n_blocks)
        struct.pack_into('>I', dh, 32, block_size)
        f.write(dh)
        entries = []
        cur = block_area
        for i in range(n_blocks):
            if i in holes:
                entries.append(0)
                continue
            entries.append(cur // 512)
            cur += bitmap_len + block_size
        f.write(b''.join(struct.pack('>I', e) for e in entries))
        f.seek(block_area)
        for i in range(n_blocks):
            if i in holes:
                continue
            f.write(b'\xFF' * bitmap_len)
            f.write(data[i * block_size:(i + 1) * block_size])
        f.write(vhd_footer(virtual, 3, 512))                 # 真footer@尾


def test_fixed():
    for tag in ('exfat', 'ntfs'):
        p = os.path.join(FIX, tag + '.vhd')
        info = vmdisk.detect(p)
        check('fixed VHD识别(%s)' % tag,
              info['kind'] == 'vhd-fixed' and
              info['virtual_size'] == os.path.getsize(p) - 512 and
              not info['needs_expand'],
              '%s %s' % (info.get('kind'), info.get('virtual_size')))


def test_dynamic(tmp):
    src = os.path.join(FIX, 'exfat.vhd')
    size = os.path.getsize(src)
    with open(src, 'rb') as f:
        data = f.read(size - 512)                    # 数据区(去footer)
    # B: 全分配
    p = os.path.join(tmp, 'dyn.vhd')
    build_dynamic_vhd(p, data)
    info = vmdisk.detect(p)
    check('dynamic VHD识别', info['kind'] == 'vhd-dynamic' and
          info['virtual_size'] == len(data) and info['needs_expand'], str(info)[:80])
    raw = os.path.join(tmp, 'dyn.raw')
    stat = vmdisk.expand(p, raw, info)
    check('dynamic展开逐字节一致', sha_file(raw) == hashlib.sha256(data).hexdigest(),
          'blocks=%s' % (stat or {}).get('blocks'))
    # 展开后MFT/exFAT引擎直接可用: 抽查MBR和exfat_deep结果
    sys.path.insert(0, os.path.join(ROOT, 'engine'))
    import exfat_deep
    res = exfat_deep.scan(raw, 128 * 512)
    check('展开raw上exFAT深挖可用', any(r['name'] == 'del_root.txt' for r in res['records']),
          'n=%d' % len(res['records']))
    # C: 带洞(第5块)
    p2 = os.path.join(tmp, 'hole.vhd')
    build_dynamic_vhd(p2, data, holes=(5,))
    raw2 = os.path.join(tmp, 'hole.raw')
    vmdisk.expand(p2, raw2)
    with open(raw2, 'rb') as f:
        hole = f.read(2 * 1024 * 1024 * 2)          # 前2块
        f.seek(5 * 2 * 1024 * 1024)
        hole5 = f.read(2 * 1024 * 1024)
        f.seek(4 * 2 * 1024 * 1024)
        blk4 = f.read(2 * 1024 * 1024)
    check('洞=零块', hole5 == b'\x00' * (2 * 1024 * 1024))
    check('非洞块完好', blk4 == data[4 * 2 * 1024 * 1024:5 * 2 * 1024 * 1024])


def test_vmdk_flat(tmp):
    # flat extent = exfat.vhd数据区前8MB
    src = os.path.join(FIX, 'exfat.vhd')
    with open(src, 'rb') as f:
        data = f.read(8 * 1024 * 1024)
    flat = os.path.join(tmp, 'disk-flat.vmdk')
    open(flat, 'wb').write(data)
    desc = os.path.join(tmp, 'disk.vmdk')
    open(desc, 'w', encoding='ascii').write(
        '# Disk DescriptorFile\nversion=1\nCID=fffffffe\n'
        'createType="monolithicFlat"\n\n# Extent Description\n'
        'RW %d FLAT "%s" 0\n\n# The Disk Data Base\n'
        '#DDB\n\nddb.virtualHWVersion = "4"\n' % (len(data) // 512, os.path.basename(flat)))
    info = vmdisk.detect(desc)
    check('VMDK flat识别', info['kind'] == 'vmdk-flat' and
          info['virtual_size'] == len(data) and
          info.get('flat_file', '').endswith('disk-flat.vmdk'),
          '%s %s' % (info.get('kind'), info.get('virtual_size')))


def test_vmdk_sparse(tmp):
    grain = 64 * 1024
    virtual = 8 * 1024 * 1024                       # 128 grains
    per_gt = 512
    gd_sec, gt_sec = 64, 128
    first_grain_sec = 256
    payload = {0: b'\x11' * grain, 3: b'\x33' * grain, 5: b'\x55' * grain}
    p = os.path.join(tmp, 'sparse.vmdk')
    with open(p, 'wb') as f:
        hdr = bytearray(512)
        struct.pack_into('<I', hdr, 0, 0x564D444B)          # KDMV
        struct.pack_into('<I', hdr, 4, 1)
        struct.pack_into('<Q', hdr, 12, virtual // 512)     # capacity
        struct.pack_into('<I', hdr, 20, grain // 512)       # grainSize
        struct.pack_into('<Q', hdr, 24, 0)                  # descriptor off
        struct.pack_into('<Q', hdr, 32, 0)                  # descriptor size
        struct.pack_into('<I', hdr, 40, per_gt)
        struct.pack_into('<Q', hdr, 44, 0xFFFFFFFFFFFFFFFF)  # rgd
        struct.pack_into('<Q', hdr, 52, gd_sec)              # gd offset(规范: 扇区)
        f.write(hdr)
        f.seek(gd_sec * 512)
        f.write(struct.pack('<I', gt_sec))                  # GD: 1项 → GT扇区
        f.seek(gt_sec * 512)
        for g in range(128):
            sec = (first_grain_sec + g * grain // 512) if g in payload else 0
            f.write(struct.pack('<I', sec))
        for g, d in payload.items():
            f.seek((first_grain_sec + g * grain // 512) * 512)
            f.write(d)
    info = vmdisk.detect(p)
    check('sparse VMDK识别', info['kind'] == 'vmdk-sparse' and
          info['virtual_size'] == virtual, str(info)[:80])
    raw = os.path.join(tmp, 'sparse.raw')
    stat = vmdisk.expand(p, raw, info)
    with open(raw, 'rb') as f:
        g0 = f.read(grain)
        f.seek(3 * grain); g3 = f.read(grain)
        f.seek(1 * grain); g1 = f.read(grain)
    check('grain内容一致', g0 == payload[0] and g3 == payload[3] and
          (stat or {}).get('grains') == 3)
    check('洞grain=零', g1 == b'\x00' * grain)
    check('raw长度=虚拟容量', os.path.getsize(raw) == virtual)


def test_vhdx(tmp):
    p = os.path.join(tmp, 'x.vhdx')
    open(p, 'wb').write(b'vhdxfile' + b'\x00' * 4096)
    info = vmdisk.detect(p)
    check('VHDX识别(仅识别)', info['kind'] == 'vhdx' and not info['needs_expand']
          and 'v1只识别' in info['detail'])
    try:
        vmdisk.expand(os.path.join(FIX, 'exfat.vhd'), os.path.join(tmp, 'x.raw'))
        ok = False
    except ValueError as e:
        ok = '无需展开' in str(e)
    check('fixed VHD展开→正确拒绝', ok)


def main():
    tmp = tempfile.mkdtemp(prefix='vmdisk_')
    try:
        test_fixed()
        test_dynamic(tmp)
        test_vmdk_flat(tmp)
        test_vmdk_sparse(tmp)
        test_vhdx(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print('\n%s: %d项失败' % ('总判定' if FAILS else '全绿', len(FAILS)), flush=True)
    sys.exit(1 if FAILS else 0)


if __name__ == '__main__':
    main()
