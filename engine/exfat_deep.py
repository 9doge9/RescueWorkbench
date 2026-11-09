# -*- coding: utf-8 -*-
"""exFAT深度恢复 — 已删目录项+孤儿目录全簇挖掘
背景: sf-cli按目录树遍历(从root走活链), 看得到"树上的已删项"; 但
  ① 条目被新文件覆盖、② 整目录删除后其目录簇成为孤儿(无活链指向)——
  这些场景树遍历看不到。本模块全簇扫描目录项条目集, 是树遍历的超集。
校准(实测exfat.vhd, 8样本全中): NameHash=规范Figure4——大写化名字的
UTF-16LE字节流, 每字节 h=ROTC1(h)+byte; FileName字段在C1条目字节2起。
几何: 簇n字节偏移 = part + (heap_off + (n-2)*spc)*bps   ← heap_off是扇区数!
"""
import struct

ENTRY_SZ = 32
FAT_EOF = 0xFFFFFFFF
FAT_BAD = 0xFFFFFFF7


def name_hash(name):
    """规范Figure4 NameHash(实测校准)"""
    h = 0
    for b in name.upper().encode('utf-16-le'):
        h = ((h & 1) << 15) | (h >> 1)
        h = (h + b) & 0xFFFF
    return h


def set_checksum(entries_bytes):
    """规范Figure2: 条目集校验(跳过主项字节2,3)"""
    h = 0
    for idx, b in enumerate(entries_bytes):
        if idx in (2, 3):
            continue
        h = ((h & 1) << 15) | (h >> 1)
        h = (h + b) & 0xFFFF
    return h


class Vol:
    def __init__(self, f, base):
        f.seek(base)
        boot = f.read(512)
        if len(boot) < 512 or boot[3:11] != b'EXFAT   ':
            raise ValueError('非exFAT卷')
        self.f = f
        self.base = base
        self.bps = 1 << boot[0x6C]
        self.spc = 1 << boot[0x6D]
        self.cs = self.bps * self.spc
        self.fat_off, = struct.unpack_from('<I', boot, 0x50)
        self.fat_len, = struct.unpack_from('<I', boot, 0x54)
        self.heap_off, = struct.unpack_from('<I', boot, 0x58)
        self.count, = struct.unpack_from('<I', boot, 0x5C)
        self.root_cl, = struct.unpack_from('<I', boot, 0x60)
        self._fat = b''

    def fat_load(self):
        need = (self.count + 2) * 4
        self.f.seek(self.base + self.fat_off * self.bps)
        self._fat = self.f.read(need)
        return len(self._fat) >= need

    def fat(self, n):
        if not self._fat or n * 4 + 4 > len(self._fat):
            return 0
        return struct.unpack_from('<I', self._fat, n * 4)[0]

    def clus_off(self, n):
        return self.base + (self.heap_off + (n - 2) * self.spc) * self.bps

    def read_clus(self, n):
        self.f.seek(self.clus_off(n))
        return self.f.read(self.cs)

    def chain(self, first, limit=None):
        """FAT链簇号列表; 断链返回(None, 已走部分)标记"""
        out = []
        seen = set()
        c = first
        while 2 <= c <= self.count + 1:
            if c in seen:
                return out, True
            seen.add(c)
            out.append(c)
            nxt = self.fat(c)
            if nxt == FAT_EOF:
                return out, False
            if nxt == 0 or nxt == FAT_BAD:
                return out, True        # 链被清(已删) → 截断标记
            c = nxt
            if limit and len(out) >= limit:
                return out, False
        return out, True


def _dos_ts(v):
    """exFAT Timestamp(4B): 秒/2(5) 分(6) 时(5) 日(5) 月(4) 年+1980(7)"""
    if not v:
        return None
    y = ((v >> 25) & 0x7F) + 1980
    mo = (v >> 21) & 0xF
    d = (v >> 16) & 0x1F
    h = (v >> 11) & 0x1F
    mi = (v >> 5) & 0x3F
    s = (v & 0x1F) * 2
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None
    return '%04d-%02d-%02d %02d:%02d:%02d' % (y, mo, d, h, mi, s)


def parse_entry_sets(buf):
    """一个目录簇字节 → 条目集列表(不校验InUse归属, 全收)"""
    sets = []
    i = 0
    n = len(buf)
    while i + ENTRY_SZ <= n:
        t = buf[i]
        if t in (0x85, 0x05):
            sec_cnt = buf[i + 1]
            if 1 <= sec_cnt <= 18 and i + ENTRY_SZ * (sec_cnt + 1) <= n:
                raw = buf[i:i + ENTRY_SZ * (sec_cnt + 1)]
                sec_types = [raw[ENTRY_SZ * (k + 1)] for k in range(sec_cnt)]
                if sec_types[0] in (0xC0, 0x40) and \
                        all(x in (0xC1, 0x41) for x in sec_types[1:]):
                    sets.append((i, raw, t == 0x05))
                    i += ENTRY_SZ * (sec_cnt + 1)
                    continue
        i += ENTRY_SZ
    return sets


def _make_record(vol, dir_cluster, off, raw, deleted):
    sec_cnt = raw[1]
    c0 = raw[ENTRY_SZ:ENTRY_SZ * 2]
    nlen = c0[3]
    stored_hash, = struct.unpack_from('<H', c0, 4)
    vsize, = struct.unpack_from('<Q', c0, 8)
    first_cl, = struct.unpack_from('<I', c0, 20)
    dlen, = struct.unpack_from('<Q', c0, 24)
    name = ''
    for k in range(1, sec_cnt):
        seg = raw[ENTRY_SZ * (k + 1):ENTRY_SZ * (k + 2)]
        name += seg[2:32].decode('utf-16-le', errors='ignore')
    name = name[:nlen]
    attrs, = struct.unpack_from('<H', raw, 4)
    h_ok = (nlen >= 1 and bool(name) and name_hash(name) == stored_hash)
    # 删除操作会把类型字节高位置0(85→05等), 还原后再算set checksum才有意义
    restored = bytearray(raw)
    if deleted:
        for k in range(len(restored) // ENTRY_SZ):
            restored[k * ENTRY_SZ] |= 0x80
    chk = set_checksum(bytes(restored))
    return {
        'name': name, 'is_dir': bool(attrs & 0x10),
        'size': dlen, 'valid_size': vsize, 'first_cluster': first_cl,
        'deleted': deleted,
        'no_fat_chain': bool(c0[1] & 2),
        'hash_ok': h_ok, 'set_checksum_ok': (chk == struct.unpack_from('<H', raw, 2)[0]),
        'created': _dos_ts(struct.unpack_from('<I', raw, 8)[0]),
        'modified': _dos_ts(struct.unpack_from('<I', raw, 12)[0]),
        'dir_cluster': dir_cluster, 'entry_off': off,
    }


def walk_live(vol):
    """活目录树BFS → live目录簇集合(用于孤儿判定)"""
    live = set()
    queue = [vol.root_cl]
    seen = set()
    while queue:
        cl = queue.pop()
        if cl in seen or not (2 <= cl <= vol.count + 1):
            continue
        seen.add(cl)
        clusters, _ = vol.chain(cl, limit=4096)
        for c in clusters:
            if c in live:
                continue
            live.add(c)
            data = vol.read_clus(c)
            for off, raw, deleted in parse_entry_sets(data):
                if deleted or raw[0] != 0x85:
                    continue
                rec = _make_record(vol, c, off, raw, False)
                if rec['is_dir'] and rec['first_cluster'] >= 2:
                    queue.append(rec['first_cluster'])
    return live


def scan(source, part_lba, on_progress=None, cancel=None):
    """全簇深扫 → {'records': [...], 'live_clusters': n, 'clusters_scanned': n}"""
    with open(source, 'rb') as f:
        vol = Vol(f, part_lba)
        vol.fat_load()
        live = walk_live(vol)
        records = []
        cs = vol.cs
        buf = b''
        buf_first = 2                      # buf首对应的簇号
        CH = 8 * 1024 * 1024
        pos = vol.clus_off(2)
        f.seek(pos)
        scanned = 0
        while scanned < vol.count:
            if cancel is not None and cancel.is_set():
                return {'records': records, 'cancelled': True,
                        'live_clusters': len(live), 'clusters_scanned': scanned}
            buf = f.read(CH)
            if not buf:
                break
            per = cs // 32                 # 簇内条目数(目录项32B对齐)
            for k in range(0, len(buf) // cs):
                cl = buf_first + k
                if cl > vol.count + 1:
                    break
                clu = buf[k * cs:(k + 1) * cs]
                if clu.count(0x85) + clu.count(0x05) == 0:
                    continue               # 快筛: 无主项字节
                for off, raw, deleted in parse_entry_sets(clu):
                    rec = _make_record(vol, cl, off, raw, deleted)
                    if cl in live and not deleted:
                        continue           # 树上可见的活项: sf-cli已覆盖
                    records.append(rec)
            scanned += len(buf) // cs
            buf_first += len(buf) // cs
            if on_progress and scanned % 4096 < CH // cs:
                on_progress({'event': 'scan_progress',
                             'clusters': scanned, 'clusters_total': vol.count,
                             'found': len(records)})
        # 分类标注
        for r in records:
            r['orphan'] = (not r['deleted'])
        return {'records': records, 'live_clusters': len(live),
                'clusters_scanned': scanned}


def extract(vol, rec, max_clusters=65536):
    """提取文件内容 → (bytes, mode, truncated)
    mode: 'fat_chain'(FAT链完好) | 'nofat'(无链文件按长度连续读) |
          'contig_fallback'(链被清, 连续兜底, 可能混入新数据)"""
    size = rec['size']
    if size == 0 or rec['first_cluster'] < 2:
        return b'', 'empty', False
    need = (min(size, rec['valid_size'] or size) + vol.cs - 1) // vol.cs
    need = min(need, max_clusters)
    out = bytearray()
    if rec['no_fat_chain']:
        for i in range(need):
            out += vol.read_clus(rec['first_cluster'] + i)
        return bytes(out[:size]), 'nofat', False
    clusters, broke = vol.chain(rec['first_cluster'], limit=need)
    if clusters and not broke and len(clusters) >= need:
        for c in clusters[:need]:
            out += vol.read_clus(c)
        return bytes(out[:size]), 'fat_chain', False
    # FAT链被清(删除的典型后果): 连续兜底, 遇已分配簇(被新文件占用)截断
    for i in range(need):
        c = rec['first_cluster'] + i
        if vol.fat(c) != 0:
            return bytes(out[:size]), 'contig_fallback', True
        out += vol.read_clus(c)
    return bytes(out[:size]), 'contig_fallback', len(out) < size
