# -*- coding: utf-8 -*-
"""合成ext4镜像构造器(测试夹具) — 供test_ext4使用
布局: MBR@0(LBA128起ext4分区) | 4K块 | inode_size=256 | 单组
  超块@+1024, GDT@块1, 块位图@2, inode位图@3, inode表@4..131,
  journal@132..1155(inode8, 1024块), 根目录@1156, 数据@1157+
journal: J超块@0, 完整事务T1@2(旧根目录+inode表块, 含将删文件), 崩溃尾巴T2@6(无commit)
主fs随后"删除"victim.txt → 只有journal里还有它的名字+inode+extent
"""
import struct

BS = 4096
INODE_SIZE = 256
INODES_PER_GROUP = 2048
IT_BLOCKS = 128                    # inode表块数
JOURNAL_BLOCKS = 1024
JOURNAL_START = 132
ROOT_DIR_BLOCK = JOURNAL_START + JOURNAL_BLOCKS          # 1156
DATA_START = ROOT_DIR_BLOCK + 3                          # 子目录+文件数据
PART_OFF = 128 * 512                  # 分区起始(MBR后1MB)
TOTAL_BLOCKS = 20480                  # 80MB

JBD2_MAGIC = 0xC03B3998


def _inode(mode, size, extents, links=1):
    b = bytearray(INODE_SIZE)
    struct.pack_into('<H', b, 0, mode)
    struct.pack_into('<I', b, 4, size & 0xFFFFFFFF)
    struct.pack_into('<H', b, 0x1A, links)
    struct.pack_into('<I', b, 0x20, 0x80000)             # EXTENTS_FL
    struct.pack_into('<H', b, 0x28, 0xF30A)
    struct.pack_into('<HHH', b, 0x2A, len(extents), 4, 0)
    for i, (lb, phys, ln) in enumerate(extents):
        struct.pack_into('<IHHI', b, 0x34 + 12 * i, lb, ln, 0, phys)
    struct.pack_into('<I', b, 0x6C, size >> 32)
    return bytes(b)


def _dirents(entries):
    """entries=[(ino,name,ftype)] → 目录块"""
    b = bytearray(BS)
    pos = 0
    for ino, name, ftype in entries:
        raw = name.encode()
        need = (8 + len(raw) + 3) // 4 * 4
        b[pos:pos + 8] = struct.pack('<IHBB', ino, need, len(raw), ftype)
        b[pos + 8:pos + 8 + len(raw)] = raw
        pos += need
    if pos:
        # 最后一项吃掉剩余空间(rec_len字段在条目起点+4, 不是+2!)
        last_len = BS - (pos - need)
        struct.pack_into('<H', b, pos - need + 4, last_len)
    return bytes(b)


def _jheader(btype):
    return struct.pack('>II', JBD2_MAGIC, btype)


def build(path, live_content=b'LIVEFILE' * 512, victim_content=b'VICTIMDATA' * 934):
    """构造镜像: 完成后返回已知事实dict"""
    import os
    victim_blocks = (len(victim_content) + BS - 1) // BS
    victim_data_blk = DATA_START + 5                      # live@+0 sub@+1 inner@+3,4 victim@+5
    sub_dir_blk = DATA_START + 1
    live_blk = DATA_START
    img = bytearray(BS * TOTAL_BLOCKS)
    base = PART_OFF

    # ---- MBR ----
    img[510:512] = b'\x55\xaa'
    e = bytearray(16)
    e[0] = 0x80
    e[4] = 0x83                                   # Linux
    struct.pack_into('<I', e, 8, 128)             # start LBA
    struct.pack_into('<I', e, 12, TOTAL_BLOCKS * BS // 512 - 128)
    img[446:462] = e

    def put_block(n, data):
        img[base + n * BS: base + (n + 1) * BS] = data

    # ---- 超级块 ----
    sb = bytearray(1024)
    struct.pack_into('<I', sb, 0x0, INODES_PER_GROUP)          # s_inodes_count
    struct.pack_into('<I', sb, 0x4, TOTAL_BLOCKS)              # s_blocks_count_lo
    struct.pack_into('<I', sb, 0x14, 0)                        # first_data_block
    struct.pack_into('<I', sb, 0x18, 2)                        # log_block_size
    struct.pack_into('<I', sb, 0x20, 8 * BS)                   # blocks_per_group
    struct.pack_into('<I', sb, 0x28, INODES_PER_GROUP)
    struct.pack_into('<H', sb, 0x38, 0xEF53)
    struct.pack_into('<I', sb, 0x4C, 1)                        # rev_level
    struct.pack_into('<I', sb, 0x54, 10)                       # first_ino
    struct.pack_into('<H', sb, 0x58, INODE_SIZE)
    struct.pack_into('<I', sb, 0x60, 0x4)                      # compat: has_journal
    struct.pack_into('<I', sb, 0x64, 0x2)                      # incompat: filetype
    sb[0x78:0x88] = b'testvol'.ljust(16, b'\x00')
    struct.pack_into('<I', sb, 0xE0, 8)                        # journal_inum
    img[base + 1024: base + 2048] = sb

    # ---- GDT ----
    gdt = bytearray(BS)
    struct.pack_into('<I', gdt, 0, 2)                          # block bitmap
    struct.pack_into('<I', gdt, 4, 3)                          # inode bitmap
    struct.pack_into('<I', gdt, 8, 4)                          # inode table
    struct.pack_into('<H', gdt, 12, TOTAL_BLOCKS - DATA_START - 3)  # free blocks(随便)
    struct.pack_into('<H', gdt, 14, INODES_PER_GROUP - 6)
    struct.pack_into('<H', gdt, 16, 2)                         # used dirs
    put_block(1, bytes(gdt))

    # ---- 位图 ----
    bbm = bytearray(BS)
    used_bits = victim_data_blk + victim_blocks
    for i in range(used_bits):
        bbm[i // 8] |= 1 << (i % 8)
    put_block(2, bytes(bbm))
    ibm = bytearray(BS)
    for ino in (1, 2, 8, 11, 12, 13, 14):
        ibm[(ino - 1) // 8] |= 1 << ((ino - 1) % 8)
    put_block(3, bytes(ibm))

    # ---- inode表(块4起) ----
    it = bytearray(IT_BLOCKS * BS)
    def put_inode(ino, data):
        off = (ino - 1) * INODE_SIZE
        it[off:off + INODE_SIZE] = data
    put_inode(1, _inode(0, 0, []))                             # bad blocks(空)
    put_inode(2, _inode(0x41ED, BS, [(0, ROOT_DIR_BLOCK, 1)], links=3))
    put_inode(8, _inode(0x81A4, JOURNAL_BLOCKS * BS,
                        [(0, JOURNAL_START, JOURNAL_BLOCKS)]))
    put_inode(11, _inode(0x81A4, len(live_content), [(0, live_blk, 1)]))
    put_inode(12, _inode(0x41ED, BS, [(0, sub_dir_blk, 1)], links=2))
    put_inode(13, _inode(0x81A4, BS * 2, [(0, DATA_START + 3, 2)]))
    put_inode(14, _inode(0x81A4, len(victim_content),
                         [(0, victim_data_blk, victim_blocks)]))
    for k in range(IT_BLOCKS):
        put_block(4 + k, it[k * BS:(k + 1) * BS])

    # ---- 目录与数据 ----
    root_entries = [(2, '.', 2), (2, '..', 2),
                    (11, 'live.txt', 1), (12, 'sub', 2), (14, 'victim.txt', 1)]
    sub_entries = [(12, '.', 2), (2, '..', 2), (13, 'inner.bin', 1)]
    put_block(ROOT_DIR_BLOCK, _dirents(root_entries))
    put_block(sub_dir_blk, _dirents(sub_entries))
    img[base + live_blk * BS: base + live_blk * BS + len(live_content)] = live_content
    img[base + (DATA_START + 3) * BS: base + (DATA_START + 5) * BS] = \
        b'INNERBLOB' * (BS * 2 // 9)
    img[base + victim_data_blk * BS:
         base + (victim_data_blk + victim_blocks) * BS] = \
        victim_content.ljust(victim_blocks * BS, b'\x00')

    # ---- journal ----
    old_root = _dirents(root_entries)                          # 含victim的旧版
    it_block0 = it[:BS]                                        # 含inode14的表块
    j = [b'\x00' * BS] * JOURNAL_BLOCKS
    jsb = bytearray(BS)
    jsb[0:8] = _jheader(3)
    struct.pack_into('>I', jsb, 8, BS)                         # s_blocksize
    struct.pack_into('>I', jsb, 12, JOURNAL_BLOCKS)            # s_maxlen
    struct.pack_into('>I', jsb, 16, 1)                         # s_first
    struct.pack_into('>I', jsb, 20, 1)                         # s_sequence
    struct.pack_into('>I', jsb, 24, 2)                         # s_start
    j[0] = bytes(jsb)
    # T1@2: descriptor(2 tags: 根目录块, inode表块0) + 数据 + commit
    desc = bytearray(BS)
    desc[0:8] = _jheader(1)
    struct.pack_into('>II', desc, 8, ROOT_DIR_BLOCK, 2)        # SAME_UUID
    struct.pack_into('>II', desc, 16, 4, 2 | 8)                # LAST_TAG
    j[2] = bytes(desc)
    j[3] = old_root
    j[4] = it_block0
    j[5] = _jheader(2) + b'\x00' * (BS - 8)                    # commit
    # T2@6: 崩溃尾巴(无commit)
    desc2 = bytearray(BS)
    desc2[0:8] = _jheader(1)
    struct.pack_into('>II', desc2, 8, ROOT_DIR_BLOCK, 2 | 8)
    j[6] = bytes(desc2)
    j[7] = old_root
    for k in range(JOURNAL_BLOCKS):
        put_block(JOURNAL_START + k, j[k])

    # ---- "删除"victim.txt: 目录项inode清零 + inode位清除 + inode清零 ----
    root_now = bytearray(_dirents(root_entries))
    pos = 0
    while pos + 8 <= len(root_now):
        ino, rec_len, name_len, ftype = struct.unpack_from('<IHBB', root_now, pos)
        name = bytes(root_now[pos + 8:pos + 8 + name_len])
        if name == b'victim.txt':
            struct.pack_into('<I', root_now, pos, 0)           # ino=0
        pos += rec_len
    put_block(ROOT_DIR_BLOCK, bytes(root_now))
    ibm2 = bytearray(ibm)
    ibm2[(14 - 1) // 8] &= ~(1 << ((14 - 1) % 8))
    put_block(3, bytes(ibm2))
    img[base + 4 * BS + 13 * INODE_SIZE:
         base + 4 * BS + 14 * INODE_SIZE] = b'\x00' * INODE_SIZE

    with open(path, 'wb') as f:
        f.write(img)
    return {'victim_ino': 14, 'victim_blocks': victim_blocks,
            'victim_data_blk': victim_data_blk, 'live_ino': 11,
            'part_lba': PART_OFF, 'root_dir_block': ROOT_DIR_BLOCK,
            'inode_table_block': 4, 'total_blocks': TOTAL_BLOCKS}
