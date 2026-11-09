# -*- coding: utf-8 -*-
"""纯MFT扫描器 — $Boot/BPB损坏时的NTFS兜底恢复通道
原理: MFT记录以"FILE"开头+fixup数组,1024字节定长,磁盘上连续。
      全盘搜FILE签名→自举定位MFT起点→遍历全部记录→重建文件树。
许可: 参考analyzeMFT(MIT)的解析逻辑,自研精简实现。
"""
import struct
import os

MFT_RECORD_SIZE = 1024
FILE_MAGIC = b'FILE'

# 属性类型
ATTR_STANDARD = 0x10
ATTR_FILENAME = 0x30
ATTR_DATA = 0x80
ATTR_END = 0xFFFFFFFF

# 标志位
FLAG_IN_USE = 0x01
FLAG_DIRECTORY = 0x02


def apply_fixup(record):
    """应用更新序列数组(fixup)还原真实数据"""
    if len(record) < MFT_RECORD_SIZE or record[:4] != FILE_MAGIC:
        return None
    usa_off = struct.unpack_from('<H', record, 4)[0]
    usa_cnt = struct.unpack_from('<H', record, 6)[0]
    if usa_cnt == 0 or usa_off + usa_cnt * 2 > len(record):
        return None
    usn = record[usa_off:usa_off + 2]
    fixed = bytearray(record)
    sector_count = len(record) // 512
    for i in range(sector_count):
        end_off = (i + 1) * 512 - 2
        if fixed[end_off:end_off + 2] != usn:
            return None
        val_off = usa_off + (i + 1) * 2
        fixed[end_off:end_off + 2] = fixed[val_off:val_off + 2]
    return bytes(fixed)


def parse_record(record):
    """解析单条MFT记录 → dict或None"""
    fixed = apply_fixup(record)
    if fixed is None:
        return None

    seq = struct.unpack_from('<H', fixed, 16)[0]
    link = struct.unpack_from('<H', fixed, 18)[0]
    flags = struct.unpack_from('<H', fixed, 22)[0]
    used = struct.unpack_from('<I', fixed, 24)[0]
    base_ref = struct.unpack_from('<Q', fixed, 32)[0]
    attr_off = struct.unpack_from('<H', fixed, 20)[0]

    if used > MFT_RECORD_SIZE or attr_off >= used:
        return None

    result = {
        'seq': seq, 'link': link,
        'in_use': bool(flags & FLAG_IN_USE),
        'is_dir': bool(flags & FLAG_DIRECTORY),
        'base_ref': base_ref,
        'name': '', 'parent': 5,           # 默认根目录
        'size': 0, 'runs': [], 'resident_data': None,
        'compressed': False, 'encrypted': False,
        'timestamps': {},
    }

    # 遍历属性
    pos = attr_off
    while pos + 4 <= used:
        attr_type = struct.unpack_from('<I', fixed, pos)[0]
        if attr_type == ATTR_END:
            break
        attr_len = struct.unpack_from('<I', fixed, pos + 4)[0]
        if attr_len < 8 or pos + attr_len > used:
            break

        non_resident = fixed[pos + 8]

        if attr_type == ATTR_FILENAME and not non_resident:
            _parse_filename(fixed, pos, result)
        elif attr_type == ATTR_DATA:
            _parse_data(fixed, pos, attr_len, non_resident, result)
        elif attr_type == ATTR_STANDARD and not non_resident:
            _parse_std_info(fixed, pos, result)

        pos += attr_len

    return result


def _parse_filename(buf, pos, result):
    """解析$FILE_NAME属性"""
    try:
        val_len = struct.unpack_from('<I', buf, pos + 16)[0]
        val_off = struct.unpack_from('<H', buf, pos + 20)[0]
        val = buf[pos + val_off:pos + val_off + val_len]
        if len(val) < 66:
            return
        result['parent'] = struct.unpack_from('<Q', val, 0)[0] & 0xFFFFFFFFFFFF
        name_len = val[64]
        if name_len > 0 and name_len <= 255:
            result['name'] = val[66:66 + name_len * 2].decode('utf-16-le', errors='replace')
        # 时间戳
        for tname, off in [('created', 8), ('modified', 16), ('accessed', 24)]:
            ft = struct.unpack_from('<Q', val, off)[0]
            if ft > 0:
                result['timestamps'][tname] = ft
    except (struct.error, IndexError):
        pass


def _parse_data(buf, pos, attr_len, non_resident, result):
    """解析$DATA属性"""
    # 检查是否有名称(如::$DATA的流)——只取未命名
    name_len = buf[pos + 9]
    name_off = struct.unpack_from('<H', buf, pos + 12)[0]
    if name_len > 0:
        return  # 命名流(如ADS)跳过
    attr_flags = struct.unpack_from('<H', buf, pos + 12 - 2)[0] if pos >= 2 else 0
    # 简单取flags: 在标准属性头偏移0xC处
    try:
        result['compressed'] = bool(struct.unpack_from('<H', buf, pos + 12)[0] & 0x0001)
        result['encrypted'] = bool(struct.unpack_from('<H', buf, pos + 12)[0] & 0x4000)
    except struct.error:
        pass
    if not non_resident:
        val_len = struct.unpack_from('<I', buf, pos + 16)[0]
        val_off = struct.unpack_from('<H', buf, pos + 20)[0]
        result['resident_data'] = buf[pos + val_off:pos + val_off + val_len]
        result['size'] = val_len
    else:
        try:
            start_vcn = struct.unpack_from('<Q', buf, pos + 16)[0]
            last_vcn = struct.unpack_from('<Q', buf, pos + 24)[0]
            run_off = struct.unpack_from('<H', buf, pos + 32)[0]
            alloc_size = struct.unpack_from('<Q', buf, pos + 40)[0]
            real_size = struct.unpack_from('<Q', buf, pos + 48)[0]
            result['size'] = real_size
            runs = _decode_runs(buf[pos + run_off:pos + attr_len], start_vcn)
            result['runs'] = runs
        except (struct.error, IndexError):
            pass


def _decode_runs(data, start_vcn=0):
    """解码NTFS data runs → [(vcn, length, lcn), ...]"""
    runs = []
    vcn = start_vcn
    lcn = 0
    i = 0
    while i < len(data) and data[i] != 0:
        header = data[i]
        len_size = header & 0x0F
        off_size = (header >> 4) & 0x0F
        if len_size == 0 or i + 1 + len_size + off_size > len(data):
            break
        run_len = int.from_bytes(data[i+1:i+1+len_size], 'little')
        i += 1 + len_size
        if off_size > 0:
            off_bytes = data[i:i+off_size]
            # 有符号偏移
            off_val = int.from_bytes(off_bytes, 'little')
            if off_bytes[-1] & 0x80:
                off_val -= (1 << (off_size * 8))
            lcn += off_val
            runs.append((vcn, run_len, lcn))
        else:
            runs.append((vcn, run_len, None))  # sparse
        vcn += run_len
        i += off_size
    return runs


def _parse_std_info(buf, pos, result):
    """解析$STANDARD_INFORMATION属性(时间戳)"""
    try:
        val_len = struct.unpack_from('<I', buf, pos + 16)[0]
        val_off = struct.unpack_from('<H', buf, pos + 20)[0]
        if val_len >= 32:
            for tname, off in [('created', 0), ('modified', 8)]:
                ft = struct.unpack_from('<Q', buf, pos + val_off + off)[0]
                if ft > 0:
                    result['timestamps'][tname] = ft
    except (struct.error, IndexError):
        pass


def scan_mft(source, on_progress=None):
    """扫描磁盘/镜像中的MFT → (mft_offset, records dict)
    1. 先读$Boot的BPB定位MFT(正常路径)
    2. BPB无效时全盘搜FILE签名(兜底路径)
    """
    with open(source, 'rb') as f:
        # ── 路径A: BPB定位 ──
        f.seek(0)
        boot = f.read(512)
        if len(boot) == 512 and boot[3:11] == b'NTFS    ':
            bps = struct.unpack_from('<H', boot, 11)[0]
            spc = boot[13]
            mft_lcn = struct.unpack_from('<Q', boot, 48)[0]
            mft_record_size = struct.unpack_from('<b', boot, 64)[0]
            if mft_record_size > 0:
                actual_record_size = mft_record_size * spc * bps
            else:
                actual_record_size = 1 << (-mft_record_size)
            cluster_size = spc * bps
            mft_offset = mft_lcn * cluster_size
            f.seek(mft_offset)
            probe = f.read(4)
            if probe == FILE_MAGIC:
                return _read_all_mft(f, mft_offset, actual_record_size, on_progress, cluster_size)

        # ── 路径B: 全盘搜FILE签名 ──
        f.seek(0, 2)
        total = f.tell()
        # 按扇区扫, 找到MFT记录0的特征(它有$DATA属性指向自身)
        step = 512
        pos = 0
        chunk_size = 4 * 1024 * 1024
        while pos < total:
            f.seek(pos)
            chunk = f.read(chunk_size + MFT_RECORD_SIZE)
            if not chunk:
                break
            idx = 0
            while idx < len(chunk) - MFT_RECORD_SIZE:
                idx = chunk.find(FILE_MAGIC, idx)
                if idx < 0:
                    break
                # 校验是否是有效的MFT记录0($MFT自身)
                rec = chunk[idx:idx + MFT_RECORD_SIZE]
                if len(rec) == MFT_RECORD_SIZE:
                    parsed = parse_record(rec)
                    if parsed and parsed.get('runs') and not parsed['base_ref']:
                        if parsed['name'] == '$MFT':
                            mft_off = pos + idx
                            if on_progress:
                                on_progress({'event': 'phase', 'message': '签名搜索定位MFT @%d' % mft_off})
                            # 兜底路径: 尝试常见簇大小
                            for cs in (4096, 512, 1024, 2048, 8192, 16384, 32768, 65536):
                                result = _read_all_mft(f, mft_off, MFT_RECORD_SIZE, on_progress, cs)
                                if len(result[1]) > 4:
                                    return result
                            return _read_all_mft(f, mft_off, MFT_RECORD_SIZE, on_progress)
                idx += MFT_RECORD_SIZE
            pos += chunk_size
            if on_progress and pos % (100 * 1024 * 1024) == 0:
                on_progress({'event': 'scan_progress', 'bytes_scanned': pos, 'bytes_total': total})

        return None, {}


def _read_all_mft(f, mft_offset, record_size, on_progress=None, cluster_size=4096):
    """读全部MFT记录 — 通过record0的$DATA runs定位(MFT可能碎片化)"""
    # 先读record 0获取MFT自身的data runs
    f.seek(mft_offset)
    rec0_raw = f.read(record_size)
    rec0 = parse_record(rec0_raw)
    if not rec0 or not rec0.get('runs'):
        # record0没有runs(异常)——退化为顺序读
        return _read_sequential(f, mft_offset, record_size, on_progress)

    # 通过data runs收集所有MFT字节
    mft_bytes = b''
    for vcn, length, lcn in rec0['runs']:
        if lcn is None:
            mft_bytes += b'\x00' * (length * cluster_size)
        else:
            f.seek(lcn * cluster_size)
            mft_bytes += f.read(length * cluster_size)

    # 解析全部记录
    records = {}
    count = 0
    for i in range(0, len(mft_bytes) - record_size + 1, record_size):
        raw = mft_bytes[i:i + record_size]
        parsed = parse_record(raw)
        if parsed:
            records[count] = parsed
        count += 1
        if on_progress and count % 1000 == 0:
            on_progress({'event': 'progress', 'records': count})
    return mft_offset, records


def _read_sequential(f, mft_offset, record_size, on_progress=None):
    """退化的顺序读取(仅当MFT恰好连续时有效)"""
    f.seek(mft_offset)
    records = {}
    count = 0
    consecutive_fail = 0
    while True:
        raw = f.read(record_size)
        if len(raw) < record_size:
            break
        if raw[:4] != FILE_MAGIC:
            consecutive_fail += 1
            if consecutive_fail > 64:
                break
        else:
            consecutive_fail = 0
            parsed = parse_record(raw)
            if parsed:
                records[count] = parsed
        count += 1
        if on_progress and count % 1000 == 0:
            on_progress({'event': 'progress', 'records': count})
    return mft_offset, records


def build_tree(records):
    """从MFT记录构建文件树 → {id: {name, parent_id, children, ...}}"""
    tree = {}
    # 根目录(5)特殊处理
    tree[5] = {'id': 5, 'parent_id': 0, 'name': '卷根', 'is_dir': True, 'children': []}
    for rid, rec in records.items():
        if rec['base_ref'] != 0:
            continue  # 扩展记录跳过
        if not rec['name']:
            continue
        tree[rid] = {
            'id': rid,
            'parent_id': rec['parent'],
            'name': rec['name'],
            'is_dir': rec['is_dir'],
            'size': rec['size'],
            'in_use': rec['in_use'],
            'deleted': not rec['in_use'],
            'runs': rec['runs'],
            'resident_data': rec['resident_data'],
            'compressed': rec['compressed'],
            'encrypted': rec['encrypted'],
            'timestamps': rec['timestamps'],
            'children': [],
        }
    # 挂载子项
    roots = []
    for rid, node in tree.items():
        parent = tree.get(node['parent_id'])
        if parent and parent['id'] != rid:
            parent['children'].append(node)
        elif rid != 5:
            roots.append(node)
    # 根5的直接子项 + 孤儿
    all_roots = tree[5]['children'] + roots
    return tree, all_roots


def extract_file(f, node, cluster_size, partition_offset=0):
    """从镜像中提取单文件内容"""
    if node.get('resident_data') is not None:
        return node['resident_data'][:node['size']]
    runs = node.get('runs') or []
    if not runs:
        return b''
    data = b''
    for vcn, length, lcn in runs:
        if lcn is None:
            data += b'\x00' * (length * cluster_size)
        else:
            offset = partition_offset + lcn * cluster_size
            f.seek(offset)
            data += f.read(length * cluster_size)
    return data[:node['size']]


def probe_ntfs(source):
    """探测NTFS分区健康度: 返回'healthy'(BPB可用) / 'mft_scan'(需兜底) / 'not_ntfs'"""
    with open(source, 'rb') as f:
        f.seek(0)
        boot = f.read(512)
        if len(boot) == 512 and boot[3:11] == b'NTFS    ':
            bps = struct.unpack_from('<H', boot, 11)[0]
            spc = boot[13]
            mft_lcn = struct.unpack_from('<Q', boot, 48)[0]
            mft_off = mft_lcn * spc * bps
            f.seek(mft_off)
            probe = f.read(4)
            if probe == FILE_MAGIC:
                return 'healthy'
            return 'mft_scan'
        # BPB无效——搜一下盘上有没有MFT记录
        f.seek(0)
        chunk = f.read(4 * 1024 * 1024)
        if FILE_MAGIC in chunk:
            return 'mft_scan'
        return 'not_ntfs'
