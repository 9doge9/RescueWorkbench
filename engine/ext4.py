# -*- coding: utf-8 -*-
"""ext4 + JBD2日志挖掘 — Linux盘/NAS/树莓派场景
两个恢复通道:
① 崩溃尾巴: "有descriptor无commit块"的未提交事务(掉电前最后一笔)
② 日志挖删: journal里保存的旧版目录块/ inode表块(descriptor的tag带fs块号,
   与GDT对照精确还原) → 已删文件的原名+inode+extent → 数据可直接读出
参考: Linux内核 fs/ext4 + include/linux/jbd2.h (journal全大端)
"""
import struct

EXT4_MAGIC = 0xEF53
JBD2_MAGIC = 0xC03B3998
JBD2_DESCRIPTOR = 1
JBD2_COMMIT = 2
JBD2_SUPERBLOCK = 3

FLAG_ESCAPE = 1
FLAG_SAME_UUID = 2
FLAG_LAST_TAG = 8

EXTENTS_FL = 0x80000
EXT_EXTENT_MAGIC = 0xF30A


class Vol:
    def __init__(self, f, base):
        f.seek(base + 1024)
        sb = f.read(1024)
        if len(sb) < 1024 or struct.unpack_from('<H', sb, 0x38)[0] != EXT4_MAGIC:
            raise ValueError('非ext4卷(无0xEF53魔数)')
        self.f = f
        self.base = base
        self.bs = 1024 << struct.unpack_from('<I', sb, 0x18)[0]
        self.blocks_per_group = struct.unpack_from('<I', sb, 0x20)[0]
        self.inodes_per_group = struct.unpack_from('<I', sb, 0x28)[0]
        self.inode_size = struct.unpack_from('<H', sb, 0x58)[0]
        self.incompat = struct.unpack_from('<I', sb, 0x64)[0]
        self.journal_inum = struct.unpack_from('<I', sb, 0xE0)[0] or 8
        self.volume_name = sb[0x78:0x88].split(b'\x00')[0].decode('utf-8', 'replace')
        self.blocks_count = struct.unpack_from('<I', sb, 0x4)[0]
        self._gdt = b''
        self.n_groups = 0

    def gdt_load(self):
        n_groups = (self.blocks_count + self.blocks_per_group - 1) // self.blocks_per_group
        self.gdt_entry = 64 if (self.incompat & 0x80) else 32
        self.f.seek(self.base + self.bs)
        self._gdt = self.f.read(n_groups * self.gdt_entry)
        self.n_groups = n_groups

    def group_inode_table(self, g):
        e = self._gdt[g * self.gdt_entry:]
        lo, = struct.unpack_from('<I', e, 8)
        hi = struct.unpack_from('<I', e, 0x28)[0] if self.gdt_entry == 64 else 0
        return (hi << 32) | lo

    def inode_table_map(self):
        """fs块号 → 该块槽位0对应的inode号"""
        out = {}
        per_blk = self.bs // self.inode_size
        for g in range(self.n_groups):
            t = self.group_inode_table(g)
            nblk = (self.inodes_per_group + per_blk - 1) // per_blk
            for b in range(t, t + nblk):
                out[b] = g * self.inodes_per_group + (b - t) * per_blk + 1
        return out

    def read_block(self, n):
        self.f.seek(self.base + n * self.bs)
        return self.f.read(self.bs)

    def read_inode(self, ino):
        per_blk = self.bs // self.inode_size
        g = (ino - 1) // self.inodes_per_group
        idx = (ino - 1) % self.inodes_per_group
        table = self.group_inode_table(g)
        blk = table + idx // per_blk
        data = self.read_block(blk)
        off = (idx % per_blk) * self.inode_size
        return parse_inode(data[off:off + self.inode_size], ino)

    def read_file_bytes(self, node):
        out = bytearray()
        for lb, phys, ln in sorted(node['extents']):
            for i in range(ln):
                out += self.read_block(phys + i)
        return bytes(out[:node['size']])


def parse_inode(slot, ino):
    if len(slot) < 128:
        return {'ino': ino, 'mode': 0, 'size': 0, 'flags': 0, 'extents': []}
    mode, = struct.unpack_from('<H', slot, 0)
    size_lo, = struct.unpack_from('<I', slot, 4)
    flags, = struct.unpack_from('<I', slot, 0x20)
    size_hi, = struct.unpack_from('<I', slot, 0x6C)
    extents = []
    ib = slot[0x28:0x64]
    if flags & EXTENTS_FL and len(ib) >= 12 and \
            struct.unpack_from('<H', ib, 0)[0] == EXT_EXTENT_MAGIC:
        entries, _, depth = struct.unpack_from('<HHH', ib, 2)
        if depth == 0:
            for i in range(min(entries, 4)):
                e = ib[12 + 12 * i:24 + 12 * i]
                lb, ln = struct.unpack_from('<IH', e, 0)
                hi, lo = struct.unpack_from('<HI', e, 6)
                if ln & 0x8000:
                    ln &= 0x7FFF
                if ln:
                    extents.append((lb, (hi << 32) | lo, ln))
    return {'ino': ino, 'mode': mode, 'size': (size_hi << 32) | size_lo,
            'flags': flags, 'extents': extents}


def parse_dir_block(buf):
    """目录块 → [(inode, name, deleted)]; 结构烂返回[]"""
    out = []
    pos = 0
    n = len(buf)
    while pos + 8 <= n:
        ino, rec_len, name_len, ftype = struct.unpack_from('<IHBB', buf, pos)
        if rec_len < 8 or (rec_len & 3) or pos + rec_len > n or ftype > 7:
            return []
        if name_len and name_len <= rec_len - 8:
            name = buf[pos + 8:pos + 8 + name_len].decode('utf-8', 'replace')
            if name not in ('.', '..'):
                out.append((ino, name, ino == 0))
        pos += rec_len
    return out


# ---------- JBD2 ----------

def _be32(b, off):
    return struct.unpack_from('>I', b, off)[0]


def _is_header(blk, btype=None):
    if len(blk) < 8 or _be32(blk, 0) != JBD2_MAGIC:
        return False
    return btype is None or _be32(blk, 4) == btype


def parse_journal(blocks):
    """journal块序列 → {'transactions': [{'seq','mapping':[(fs块, 数据)]}],
                       'uncommitted': [...], 'start'}
    单圈走查: 从sb的s_start起走一整圈(绝对游标), descriptor收tag、commit收事务"""
    n = len(blocks)
    start = 1
    if n and _is_header(blocks[0], JBD2_SUPERBLOCK):
        start = _be32(blocks[0], 24) % n or 1
    txs = []
    pending = []
    i = start
    limit = start + n                    # 恰好一圈(绝对)
    while i < limit and n:
        blk = blocks[i % n]
        if _is_header(blk, JBD2_DESCRIPTOR):
            tags = []
            pos = 8
            while pos + 8 <= len(blk):
                blocknr = _be32(blk, pos)
                flags = _be32(blk, pos + 4)
                pos += 8
                if not (flags & FLAG_SAME_UUID):
                    pos += 16
                tags.append((blocknr, flags))
                if flags & FLAG_LAST_TAG:
                    break
            mapping = []
            j = i + 1
            for blocknr, flags in tags:
                if j >= limit + n:
                    break
                if flags & FLAG_ESCAPE:
                    j += 1                  # 跳过魔数占位块
                mapping.append((blocknr, blocks[j % n]))
                j += 1
            pending.append({'seq': None, 'mapping': mapping})
            i = j
            continue
        if _is_header(blk, JBD2_COMMIT):
            if pending:
                txs.extend(pending)
                pending = []
            i += 1
            continue
        i += 1
    seq0 = _be32(blocks[0], 20) if n and _is_header(blocks[0], JBD2_SUPERBLOCK) else 1
    for k, tx in enumerate(txs):
        tx['seq'] = seq0 + k
    for k, tx in enumerate(pending):
        tx['seq'] = seq0 + len(txs) + k
    return {'transactions': txs, 'uncommitted': pending, 'start': start}


def journal_blocks_of(vol):
    jnode = vol.read_inode(vol.journal_inum)
    if not jnode['extents']:
        return None
    out = []
    for lb, phys, ln in sorted(jnode['extents']):
        for i in range(ln):
            out.append(vol.read_block(phys + i))
    return out


# ---------- 深挖 ----------

def mine_journal(source, part_lba, on_progress=None, cancel=None):
    with open(source, 'rb') as f:
        vol = Vol(f, part_lba)
        vol.gdt_load()
        jblocks = journal_blocks_of(vol)
        if not jblocks:
            return {'ok': False, 'error': 'journal inode无数据(extent缺失)'}
        rep = parse_journal(jblocks)
        it_map = vol.inode_table_map()
        journal_inodes = {}                     # ino → inode副本
        names = {}
        inode_copies = []
        all_tx = rep['transactions'] + rep['uncommitted']
        total_maps = sum(len(t['mapping']) for t in all_tx)
        seen_maps = 0
        for tx in all_tx:
            if cancel is not None and cancel.is_set():
                break
            for fs_block, data in tx['mapping']:
                seen_maps += 1
                if fs_block in it_map:
                    base_ino = it_map[fs_block]
                    per_blk = vol.bs // vol.inode_size
                    for s in range(per_blk):
                        node = parse_inode(
                            data[s * vol.inode_size:(s + 1) * vol.inode_size],
                            base_ino + s)
                        if node['mode'] and 0x8000 & node['mode'] and \
                                node['size'] and node['extents'] and \
                                (node['mode'] & 0xF000) == 0x8000:
                            journal_inodes[node['ino']] = node
                else:
                    for ino, name, deleted in parse_dir_block(data):
                        key = name
                        rec = names.setdefault(key, {'name': name, 'journal_ino': ino,
                                                     'sources': 0, 'deleted_entry': False})
                        rec['sources'] += 1
                        if deleted:
                            rec['deleted_entry'] = True
                if on_progress and seen_maps % 32 == 0:
                    on_progress({'event': 'progress', 'blocks': seen_maps,
                                 'total': total_maps, 'names': len(names)})
        # 名字 → 主表状态 + 日志inode副本配对
        results = []
        for rec in names.values():
            out = {'name': rec['name'], 'journal_ino': rec['journal_ino'],
                   'main_inode': '未知'}
            ino = rec['journal_ino']
            if ino:
                try:
                    node = vol.read_inode(ino)
                    if node['mode'] and node['size']:
                        out['main_inode'] = '存活'
                        out['size'] = node['size']
                    else:
                        out['main_inode'] = '已删'
                except Exception:
                    out['main_inode'] = '已删'
            if ino in journal_inodes:
                jc = journal_inodes[ino]
                out['journal_copy'] = {'size': jc['size'],
                                       'extents': [[lb, ph, ln] for lb, ph, ln in jc['extents']]}
                out['size'] = out.get('size', jc['size'])
            results.append(out)
        for node in journal_inodes.values():
            inode_copies.append({'ino': node['ino'], 'size': node['size']})
        return {'ok': True, 'volume_name': vol.volume_name,
                'block_size': vol.bs, 'journal_blocks': len(jblocks),
                'transactions': len(rep['transactions']),
                'uncommitted': len(rep['uncommitted']),
                'tx_detail': [{'seq': t['seq'], 'blocks': len(t['mapping'])}
                              for t in rep['transactions'][:50]],
                'names': results, 'inode_copies': len(inode_copies)}


def recover_by_ino(source, part_lba, ino, out_path, prefer_journal=True):
    """按inode号提取: 主表inode优先, 已删则用journal里的副本extent"""
    with open(source, 'rb') as f:
        vol = Vol(f, part_lba)
        vol.gdt_load()
        node = vol.read_inode(ino)
        if not (node['mode'] and node['size']):
            if prefer_journal:
                jblocks = journal_blocks_of(vol)
                rep = parse_journal(jblocks) if jblocks else {'transactions': []}
                it_map = vol.inode_table_map()
                for tx in rep['transactions'] + rep['uncommitted']:
                    for fs_block, data in tx['mapping']:
                        if fs_block in it_map:
                            base = it_map[fs_block]
                            per_blk = vol.bs // vol.inode_size
                            if base <= ino < base + per_blk:
                                s = ino - base
                                cand = parse_inode(
                                    data[s * vol.inode_size:(s + 1) * vol.inode_size], ino)
                                if cand['size'] and cand['extents']:
                                    node = cand
                                    break
            if not node['size']:
                raise ValueError('inode %d 在主表与journal中均无有效副本' % ino)
        data = vol.read_file_bytes(node)
        with open(out_path, 'wb') as w:
            w.write(data)
        return len(data), node['size']
