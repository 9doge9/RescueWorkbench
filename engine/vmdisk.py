# -*- coding: utf-8 -*-
"""虚拟磁盘 VMDK/VHD/VHDX 检测与展开 — 让整套恢复引擎吃进虚拟机盘
场景: 删除的虚拟机/快照/导出盘, 里面是完整的NTFS/ext4卷。
- fixed VHD / flat VMDK: 本身就是raw(±512B尾巴/flat偏移), 检测后直接用原文件
- dynamic VHD: BAT稀疏块 → 展开成raw(洞=零, 稀疏写不占实盘)
- sparse VMDK: grain目录/表 → 同上展开
- VHDX: v1只检测识别(日志结构, 暂不展开, 如实标注)
展开后的raw接给 sf-cli/allocmap/exfat_deep/h264carve 全链路。
"""
import os
import struct

VHD_TYPES = {0: 'none', 1: 'deprecated', 2: 'fixed', 3: 'dynamic', 4: 'differencing'}


def _read_head(f, n=512):
    f.seek(0)
    return f.read(n)


def detect(path):
    """→ {'kind', 'virtual_size', 'detail', 'needs_expand'} kind=None表示非虚拟盘"""
    try:
        with open(path, 'rb') as f:
            size = os.path.getsize(path)
            head = _read_head(f, 512)
            if len(head) < 8:
                return {'kind': None, 'detail': '文件过小'}
            # VMDK描述文件是文本, 可能只有几百字节 — 先于其他检查
            if head.startswith(b'# Disk DescriptorFile'):
                info = _parse_vmdk_descriptor(f, path)
                if info:
                    return info
                return {'kind': 'vmdk-descriptor', 'virtual_size': None,
                        'detail': 'VMDK描述文件但解析失败', 'needs_expand': False}
            # ---- VHD: 尾部512B footer 'conectix' (大端) ----
            f.seek(max(0, size - 512))
            foot = f.read(512)
            if foot[:8] == b'conectix':
                dtype, = struct.unpack_from('>I', foot, 0x3C)
                vsize, = struct.unpack_from('>Q', foot, 0x30)
                data_off, = struct.unpack_from('>Q', foot, 0x10)
                kind = VHD_TYPES.get(dtype, '?%d' % dtype)
                if dtype == 2:
                    return {'kind': 'vhd-fixed', 'virtual_size': vsize,
                            'detail': '固定VHD(type2): 数据=文件本体(尾512B是footer), '
                                      '原文件可直接当镜像用', 'needs_expand': False}
                if dtype == 3:
                    return {'kind': 'vhd-dynamic', 'virtual_size': vsize,
                            'detail': '动态VHD(type3): 稀疏块存储, 需展开成raw',
                            'needs_expand': True, 'dyn_header_off': data_off}
                return {'kind': 'vhd-' + kind, 'virtual_size': vsize,
                        'detail': 'VHD type=%s(差异盘需父盘, v1不支持)' % kind,
                        'needs_expand': False}
            # ---- VHDX: 文件头 'vhdxfile' ----
            if head[:8] == b'vhdxfile':
                return {'kind': 'vhdx', 'virtual_size': None,
                        'detail': 'VHDX(Hyper-V): 日志结构+BAT, v1只识别不展开(如实)',
                        'needs_expand': False}
            # ---- VMDK稀疏extent头KDMV ----
            if head[:4] == b'KDMV' and len(head) >= 64:
                return _parse_vmdk_sparse_header(head, path)
            return {'kind': None, 'detail': '未识别的虚拟磁盘格式'}
    except OSError as e:
        return {'kind': None, 'detail': str(e)}


# ---------- VMDK descriptor ----------

def _parse_vmdk_descriptor(f, path):
    f.seek(0)
    text = f.read(64 * 1024).decode('utf-8', 'replace')
    virtual = 0
    flat = None
    has_sparse = False
    for line in text.split('\n'):
        line = line.strip()
        if line.startswith('RW ') or line.startswith('RDONLY ') or line.startswith('NOACCESS '):
            parts = line.split()
            if len(parts) >= 4:
                try:
                    sectors = int(parts[1])
                except ValueError:
                    continue
                virtual += sectors * 512
                if parts[2].upper() in ('FLAT', 'VMFS'):
                    flat = (parts[3].strip('"'), int(parts[4]) * 512 if len(parts) > 4 else 0)
                elif parts[2].upper() == 'SPARSE':
                    has_sparse = True
    if has_sparse:
        return {'kind': 'vmdk-sparse-set', 'virtual_size': virtual,
                'detail': 'VMDK含稀疏extent, v1不支持组合展开(单独打开-extent文件可解)',
                'needs_expand': False}
    if flat:
        name, off = flat
        base = os.path.dirname(os.path.abspath(path))
        fp = name if os.path.isabs(name) else os.path.join(base, name)
        if os.path.exists(fp):
            return {'kind': 'vmdk-flat', 'virtual_size': virtual,
                    'flat_file': fp, 'flat_offset': off,
                    'detail': 'VMDK扁平盘: extent文件本身就是raw', 'needs_expand': False}
        return {'kind': 'vmdk-flat', 'virtual_size': virtual,
                'detail': 'flat extent缺失: %s' % name, 'needs_expand': False}
    return None


def _parse_vmdk_sparse_header(head, path):
    magic, version = struct.unpack_from('<II', head, 0)
    capacity, = struct.unpack_from('<Q', head, 12)
    grain, = struct.unpack_from('<I', head, 20)
    num_gt, = struct.unpack_from('<I', head, 40)
    gd_off, = struct.unpack_from('<Q', head, 52)
    rgd_off, = struct.unpack_from('<Q', head, 44)
    if not grain or not num_gt:
        return {'kind': None, 'detail': 'VMDK稀疏头字段异常'}
    if gd_off == 0xFFFFFFFFFFFFFFFF:
        gd_off = rgd_off
    return {'kind': 'vmdk-sparse', 'virtual_size': capacity * 512,
            'capacity_sectors': capacity, 'grain_sectors': grain, 'num_gtes': num_gt,
            'gd_offset': gd_off, 'needs_expand': True,
            'detail': 'VMDK稀疏extent: grain=%dKB 需展开成raw' % (grain * 512 // 1024)}


# ---------- dynamic VHD 展开 ----------

def _expand_dynamic_vhd(path, out_path, info, on_progress=None, cancel=None):
    with open(path, 'rb') as f:
        f.seek(info['dyn_header_off'])
        dh = f.read(1024)
        if dh[:8] != b'cxsparse':
            raise ValueError('动态VHD头损坏(非cxsparse)')
        table_off, = struct.unpack_from('>Q', dh, 16)
        max_entries, = struct.unpack_from('>I', dh, 28)
        block_size, = struct.unpack_from('>I', dh, 32)
        f.seek(table_off)
        bat = f.read(max_entries * 4)
        bitmap_len = ((block_size // 512 + 7) // 8 + 511) // 512 * 512
        out = open(out_path, 'wb')
        try:
            blocks = 0
            for i in range(max_entries):
                if cancel is not None and cancel.is_set():
                    return None
                entry, = struct.unpack_from('>I', bat, i * 4)
                raw_pos = i * block_size
                if entry == 0:
                    continue
                f.seek(entry * 512)
                f.read(bitmap_len)                 # 位图(整块照抄, 洞本就是零)
                data = f.read(block_size)
                out.seek(raw_pos)
                out.write(data)
                blocks += 1
                if on_progress and blocks % 64 == 0:
                    on_progress({'event': 'progress', 'blocks': blocks,
                                 'total': max_entries})
            out.truncate(info['virtual_size'])
        finally:
            out.close()
        return {'blocks': blocks, 'block_size': block_size}


# ---------- sparse VMDK 展开 ----------

def _expand_vmdk_sparse(path, out_path, info, on_progress=None, cancel=None):
    grain = info['grain_sectors'] * 512
    per_gt = info['num_gtes']
    capacity = info['capacity_sectors']
    gd_off = info['gd_offset'] * 512              # 规范: gdOffset单位是扇区
    num_gd = (capacity + grain // 512 * per_gt - 1) // (grain // 512 * per_gt)
    with open(path, 'rb') as f, open(out_path, 'wb') as out:
        f.seek(gd_off)
        gd = f.read(num_gd * 4)
        written = 0
        for i in range(num_gd):
            if cancel is not None and cancel.is_set():
                return None
            gde, = struct.unpack_from('<I', gd, i * 4)
            if gde == 0 or gde == 0xFFFFFFFF:
                continue
            f.seek(gde * 512)
            gt = f.read(per_gt * 4)
            for j in range(per_gt):
                gte, = struct.unpack_from('<I', gt, j * 4)
                g_no = i * per_gt + j
                if gte == 0 or gte == 0xFFFFFFFF:
                    continue
                raw_pos = g_no * grain
                if raw_pos >= info['virtual_size']:
                    break
                f.seek(gte * 512)
                data = f.read(grain)
                out.seek(raw_pos)
                out.write(data)
                written += 1
                if on_progress and written % 64 == 0:
                    on_progress({'event': 'progress', 'grains': written,
                                 'total': num_gd * per_gt})
        out.truncate(info['virtual_size'])
        return {'grains': written, 'grain_size': grain}


def expand(path, out_path, info=None, on_progress=None, cancel=None):
    """虚拟盘 → raw镜像; 返回统计dict; 不支持的类型抛ValueError"""
    info = info or detect(path)
    kind = info.get('kind')
    if kind == 'vhd-dynamic':
        return _expand_dynamic_vhd(path, out_path, info, on_progress, cancel)
    if kind == 'vmdk-sparse':
        return _expand_vmdk_sparse(path, out_path, info, on_progress, cancel)
    if kind == 'vhd-fixed':
        raise ValueError('fixed VHD无需展开: 原文件直接当镜像用(末512B是footer, 不碍事)')
    if kind == 'vmdk-flat':
        raise ValueError('flat VMDK无需展开: extent文件 %s 直接当镜像用'
                         % info.get('flat_file', '?'))
    raise ValueError('v1不支持展开: %s (%s)' % (kind, info.get('detail')))
