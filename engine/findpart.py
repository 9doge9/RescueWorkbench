# -*- coding: utf-8 -*-
"""轻量分区定位 — 深挖面板免"先去分区页扫描"的摩擦
直接解析MBR(主分区)/GPT(基本盘), 逐分区读引导扇区按签名识别文件系统。
秒级(只读几个扇区), 与sf-cli的深度扫描互补(不做签名全盘搜)。
"""
import struct

FS_BY_SIG = {
    b'NTFS    ': 'ntfs',
    b'EXFAT   ': 'exfat',
    b'FAT32   ': 'fat32',
    b'FAT16   ': 'fat16',
    b'FAT12   ': 'fat12',
    b'-FVE-FS-': 'bitlocker',
    b'MSWIN4.1': 'bitlocker-togo',       # BitLocker To Go 的FAT壳
}

# GPT分区类型GUID前8字节 → 文件系统(仅作辅助, 最终以引导扇区签名为准)
GPT_LINUX_FS = b'\x0f\xc6\x3d_af8c-4772-8f79-3d69d8477de4'[:8]


def _sniff(f, base):
    """读扇区判文件系统 → 'ntfs'|'exfat'|'fat32'|'bitlocker'|'ext4'|'hfs+'|..."""
    try:
        f.seek(base)
        sec = f.read(512)
    except OSError:
        return None
    if len(sec) < 512:
        return None
    fs = FS_BY_SIG.get(sec[3:11])
    if fs:
        return fs
    # ext2/3/4: 魔数0xEF53 @ +0x438
    try:
        f.seek(base + 0x438)
        if f.read(2) == b'\x53\xef':
            return 'ext4'
        # HFS+: 'H+' @ +1024
        f.seek(base + 1024)
        vh = f.read(2)
        if vh in (b'H+', b'HX'):
            return 'hfs+'
        # APFS: NXSB @ 对象type字段(+24)
        f.seek(base + 24)
        if f.read(4) == b'NXSB':
            return 'apfs'
    except OSError:
        pass
    return None


def find_partitions(source):
    """→ [{'lba', 'offset'(字节), 'size_sectors', 'fs'}]
    MBR主分区(4条) + GPT(最多128条); 无分区表时若整盘是可识别FS返回单条@0"""
    parts = []
    with open(source, 'rb') as f:
        f.seek(0)
        mbr = f.read(512)
        if len(mbr) < 512 or mbr[510:512] != b'\x55\xaa':
            fs = _sniff(f, 0)            # 无分区表: 超级软盘/整盘FS
            if fs:
                import os
                parts.append({'lba': 0, 'offset': 0, 'fs': fs,
                              'size_sectors': os.path.getsize(source) // 512})
            return parts
        # ---- GPT? ----
        f.seek(512)
        gpt = f.read(512)
        if gpt[:8] == b'EFI PART':
            # 分区条目区: LBA2起, 每条128字节
            f.seek(1024)
            entries = f.read(128 * 128)
            for i in range(128):
                e = entries[128 * i:128 * (i + 1)]
                if e[:16] == b'\x00' * 16:
                    break
                lba, = struct.unpack_from('<Q', e, 32)
                tail, = struct.unpack_from('<Q', e, 40)
                if not lba:
                    continue
                fs = _sniff(f, lba * 512) or '未知'
                parts.append({'lba': lba, 'offset': lba * 512,
                              'size_sectors': (tail - lba + 1) if tail >= lba else 0,
                              'fs': fs})
            return parts
        # ---- MBR主分区(条目合法性校验: 裸FS引导扇区也有55AA, 防误判) ----
        import os as _os
        fsize = _os.pathsize(source) if False else _os.path.getsize(source)
        for i in range(4):
            e = mbr[446 + 16 * i:462 + 16 * i]
            ptype = e[4]
            if ptype == 0:
                continue
            lba, secs = struct.unpack_from('<II', e, 8)
            if not lba or secs == 0xFFFFFFFF:
                continue
            if (lba + secs) * 512 > fsize * 1.05 + 1024 * 1024:
                continue                          # 超出文件范围=垃圾条目
            fs = _sniff(f, lba * 512) or ('扩展' if ptype in (0x05, 0x0F) else '未知')
            parts.append({'lba': lba, 'offset': lba * 512,
                          'size_sectors': secs, 'fs': fs})
        if not parts:
            # 55AA但分区表无效 → 多半是裸FS引导扇区
            fs = _sniff(f, 0)
            if fs:
                parts.append({'lba': 0, 'offset': 0, 'fs': fs,
                              'size_sectors': fsize // 512})
    return parts


def find_fs(source, want):
    """找第一个匹配的文件系统分区 → {'offset'...} 或 None
    want: 'exfat'|'ext'|'bitlocker'|'ntfs'... ('ext'匹配ext4)"""
    for p in find_partitions(source):
        fs = p['fs']
        if want == 'ext':
            if fs and fs.startswith('ext'):
                return p
        elif want == 'bitlocker':
            if fs and fs.startswith('bitlocker'):
                return p
        elif fs == want:
            return p
    return None
