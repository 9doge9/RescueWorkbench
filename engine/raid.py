# -*- coding: utf-8 -*-
"""RAID重组 — 成员盘镜像 → 虚拟盘raw（接全链路恢复）
场景: NAS/服务器RAID卡坏或阵列被拆, 手里有N块成员盘的镜像。
支持: RAID0(条带) / RAID5(分布式校验, 四种经典布局) / RAID1(镜像, 取首盘)。
检测: 候选(级别×条带大小×盘序×布局)逐一在内存里拼出虚拟盘头部,
      按结构特征打分(MBR 55AA / GPT EFI PART / 分区表指向的文件系统魔数)。
降级: RAID5缺一块成员盘也能用其余盘XOR重建(mdadm丢失盘语义)。
诚实边界: RAID6双校验/控制器私有偏移(Dell H700等)未覆盖, 如实报告。
"""
import os
import struct
import itertools

STRIPES = (16, 32, 64, 128, 256, 512, 1024)      # KB
ROTATIONS = ('left-sym', 'left-asym', 'right-sym', 'right-asym')
PROBE_MB = 6                                      # 检测时拼虚拟盘前6MB


def _parity_disk(rot, n, row):
    if rot.startswith('left'):
        return (n - 1) - (row % n)
    return row % n


def chunk_map(level, n, rot, chunk, total_virtual):
    """虚拟盘chunk索引 → (成员盘序号, 盘内chunk号) 列表(懒生成器)
    RAID0: 虚拟chunk c → 盘 c%n, 盘内 c//n
    RAID5: 每行n-1个数据chunk; 行r=ci//(n-1), 行内j=ci%(n-1)"""
    out = []
    if level == 0:
        nchunks = total_virtual // chunk
        for c in range(nchunks):
            out.append((c % n, c // n))
    elif level == 5:
        nchunks = total_virtual // chunk
        for c in range(nchunks):
            r, j = divmod(c, n - 1)
            pd = _parity_disk(rot, n, r)
            if rot.endswith('sym'):
                d = (pd + 1 + j) % n
            else:
                d = j if j < pd else j + 1
            out.append((d, r))          # 盘内chunk号=行号(每盘每行恰好1块)
    return out


def _fs_magic(buf, off):
    """分区起始扇区的文件系统魔数识别"""
    if off + 512 > len(buf):
        return 0
    sec = buf[off:off + 512]
    if sec[3:11] == b'NTFS    ':
        return 3
    if sec[3:11] == b'EXFAT   ':
        return 3
    if sec[510:512] == b'\x55\xaa':
        return 2
    return 0


def _score(buf):
    """拼出的虚拟盘头部缓冲 → 结构分"""
    s = 0
    if len(buf) >= 512 and buf[510:512] == b'\x55\xaa':
        s += 10
    if len(buf) >= 520 and buf[512:520] == b'EFI PART':
        s += 12
    if len(buf) >= 512 and buf[3:11] in (b'NTFS    ', b'EXFAT   ', b'FAT32   ',
                                         b'FAT16   '):
        s += 8
    # MBR分区表逐项验证(只要求分区起始扇区落在探测缓冲内)
    if s >= 10 and len(buf) >= 512 and buf[446:510] != b'\x00' * 64:
        for i in range(4):
            e = buf[446 + 16 * i:462 + 16 * i]
            if e[4] == 0:
                continue
            lba, secs = struct.unpack_from('<II', e, 8)
            if lba and secs and (lba + 1) * 512 <= len(buf):
                s += _fs_magic(buf, lba * 512) * 4
    return s


def detect(members, on_progress=None):
    """成员盘路径列表 → 最佳配置(含打分), 支持元素为None表示缺失盘
    → {'level', 'stripe_kb', 'order', 'rotation', 'score', 'candidates'}"""
    present = [i for i, m in enumerate(members) if m]
    n = len(members)
    if n < 2:
        return {'error': '至少两块成员盘(缺失盘用null占位)'}
    # 读各盘头部到内存(检测复用)
    head = {}
    need = PROBE_MB * 1024 * 1024 + 1024 * 1024
    for i in present:
        with open(members[i], 'rb') as f:
            head[i] = f.read(need)
    sizes = {i: os.path.getsize(members[i]) for i in present}
    min_size = min(sizes.values())
    # RAID1检测: 两盘在多个偏移点都一致(全零头部的RAID0会误报, 多点抽查防)
    if len(present) >= 2:
        ps = present[:2]
        a, b = head[ps[0]], head[ps[1]]
        probes = (0, 256 * 1024, 640 * 1024, 1536 * 1024)
        if all(a[o:o + 4096] == b[o:o + 4096] for o in probes):
            return {'level': 1, 'order': present[:2], 'score': 100,
                    'detail': '两块成员盘多点内容一致: 疑似RAID1镜像, 直接用任一成员盘'}
    # 候选: 盘序(≤4盘全排列, >4只试给定序)
    orders = list(itertools.permutations(range(n))) if n <= 4 else [tuple(range(n))]
    # 级别先验: 成员盘同行XOR≈0 → RAID5(校验盘导致); 全成员在场才可判
    levels = (0, 5)
    if len(present) == n:
        zero_rows = 0
        sample_rows = 0
        for chunk_kb in (64, 128):
            c = chunk_kb * 1024
            rows = min(need // c, 24)
            for r in range(rows):
                x = bytearray(c)
                has_data = False
                for i in present:
                    off = r * c
                    blk = head[i][off:off + c]
                    if not blk:
                        break
                    if blk.count(0) < c * 0.9:
                        has_data = True
                    for k in range(c):
                        x[k] ^= blk[k]
                else:
                    sample_rows += 1
                    if x == bytearray(c) and has_data:
                        zero_rows += 1
        if sample_rows and zero_rows / sample_rows > 0.6:
            levels = (5,)
    cands = []
    best = None
    tried = 0
    total_try = len(orders) * len(STRIPES) * (2 + len(ROTATIONS))
    for level in levels:
        for stripe in STRIPES:
            chunk = stripe * 1024
            virt_probe = min(PROBE_MB * 1024 * 1024, (min_size // chunk) * chunk * (n if level == 0 else n - 1))
            if virt_probe < 1024 * 512:
                continue
            for order in orders:
                # order[i] = 虚拟盘位置i用的是哪个成员盘下标
                if 5 == level:
                    rotations = ROTATIONS
                else:
                    rotations = (None,)
                for rot in rotations:
                    tried += 1
                    mapping = chunk_map(level, n, rot, chunk, virt_probe)
                    if not mapping:
                        continue
                    buf = bytearray(virt_probe)
                    ok = True
                    for c, (slot, mchunk) in enumerate(mapping):
                        off = mchunk * chunk
                        src = head.get(order[slot])
                        if src is None or off + chunk > len(src):
                            ok = False
                            break
                        buf[c * chunk:(c + 1) * chunk] = src[off:off + chunk]
                    if not ok:
                        continue
                    s = _score(bytes(buf))
                    if s > 0:
                        cands.append({'level': level, 'stripe_kb': stripe,
                                      'order': list(order), 'rotation': rot,
                                      'score': s})
                    if best is None or s > best['score']:
                        best = {'level': level, 'stripe_kb': stripe,
                                'order': list(order), 'rotation': rot, 'score': s}
                    if on_progress and tried % 20 == 0:
                        on_progress({'event': 'progress', 'tried': tried,
                                     'total': total_try})
    if not best or best['score'] < 10:
        return {'error': '未识别出RAID结构(打分过低)。可能: RAID6/控制器私有布局/'
                         '成员盘顺序或偏移特殊。可试手动指定参数重建',
                'candidates': sorted(cands, key=lambda x: -x['score'])[:5]}
    best['candidates'] = sorted(cands, key=lambda x: -x['score'])[:5]
    best['virtual_size'] = (min_size // (best['stripe_kb'] * 1024)) * \
        (best['stripe_kb'] * 1024) * (n if best['level'] == 0 else n - 1)
    # 前二得分接近 → 结构点不足以唯一确定, 如实提醒
    if len(best['candidates']) >= 2 and \
            best['candidates'][0]['score'] - best['candidates'][1]['score'] < 4:
        best['ambiguous'] = True
        best['detail'] = ('有多种配置得分接近(校验块可能伪装成数据块)。'
                          '已选最高分; 若重建后无法识别分区, 试候选列表中的其他配置')
    return best


def rebuild(members, cfg, out_path, on_progress=None, cancel=None):
    """按配置重建虚拟盘 → raw文件
    RAID5缺盘: 数据块缺失时用同行其余盘XOR(含校验块)恢复"""
    n = len(members)
    level = cfg['level']
    chunk = cfg['stripe_kb'] * 1024
    order = cfg['order']
    rot = cfg.get('rotation')
    if level == 1:
        import shutil
        shutil.copyfile(members[order[0]], out_path)
        return out_path
    sizes = [os.path.getsize(members[i]) if members[i] else 0 for i in range(n)]
    min_size = min([s for s in sizes if s] or [0])
    nchunks_per_disk = min_size // chunk
    virt_chunks = nchunks_per_disk * (n if level == 0 else n - 1)
    total = virt_chunks * chunk
    files = {}
    for i in range(n):
        if members[i]:
            files[i] = open(members[i], 'rb')
    try:
        with open(out_path, 'wb') as out:
            row_cache = {}
            done = 0
            CH = 4 * 1024 * 1024
            buf = bytearray()
            for c in range(virt_chunks):
                if cancel is not None and cancel.is_set():
                    return None
                if level == 0:
                    slot, mchunk = c % n, c // n
                else:
                    r, j = divmod(c, n - 1)
                    pd = _parity_disk(rot, n, r)
                    if rot.endswith('sym'):
                        slot = (pd + 1 + j) % n
                    else:
                        slot = j if j < pd else j + 1
                    mchunk = r
                src = files.get(order[slot])
                if src is not None:
                    src.seek(mchunk * chunk)
                    buf += src.read(chunk)
                else:
                    # 降级: XOR同行其余全部盘(数据+校验)
                    x = bytearray(chunk)
                    for k in range(n):
                        if k == order[slot] or k not in files:
                            continue
                        files[k].seek(mchunk * chunk)
                        other = files[k].read(chunk)
                        for i in range(chunk):
                            x[i] ^= other[i]
                    buf += bytes(x)
                if len(buf) >= CH or c == virt_chunks - 1:
                    out.write(buf)
                    done += len(buf)
                    buf = bytearray()
                    if on_progress and (done // CH) % 4 == 0:
                        on_progress({'event': 'progress', 'bytes_done': done,
                                     'bytes_total': total})
            out.truncate(total)
        return out_path
    finally:
        for f in files.values():
            f.close()
