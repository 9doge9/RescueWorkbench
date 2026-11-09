# -*- coding: utf-8 -*-
"""PST/OST 检查与抢救 — 结构体检 + 头损兜底扫描 + 邮件清单
格式经libpst源码校准(2026-11): 签名!BDN@0; 版本@0x0A(0x0E=32位/0x15,0x17=64位/
0x24=4K); 64位头: desc树根@0xE0/块树根@0xF0/文件尾@0xB8/加密@0x201;
B树页=512B, 页尾元数据(64位: 计数@0x1E8两字节/容量@0x1E9/条目大小@0x1EA/
叶标志@0x1EB 叶=0/回链@0x1F8), 记录从页头顺序排;
desc条目(64位)=d_id u64+desc_id u64+tree_id u64+parent u32+pad u32(32B);
index条目(64位)=id u64+offset u64+size u16+u0 u16+u1 u32(24B)。
两级修复通道:
① 结构体检: 头指针走查desc/块两棵B树 → 节点直方图+目录树+孤儿检测
② 头损兜底: 头指针无效时全盘扫desc叶页特征 → 邮件清单抢救(无头恢复)
诚实边界: v1为结构级检查与清单(不解析邮件正文/附件), 全量导出建议readpst。
"""
import struct

SIG = 0x4E444221                      # '!BDN'
TYPES = {0x0E: ('32位(Outlook 97-2002)', 32),
         0x15: ('64位(Outlook 2003+)', 64),
         0x17: ('64位(Outlook 2003+)', 64),
         0x24: ('4K页(Outlook 2013+)', 64)}   # 4K与64位同头布局, 仅B树页大小/页尾偏移不同
NID_FOLDER = 0x1
NID_MESSAGE = 0x4


def detect(source):
    """PST/OST识别 → dict"""
    try:
        with open(source, 'rb') as f:
            h = f.read(1024)
    except OSError as e:
        return {'kind': None, 'error': str(e)}
    if len(h) < 512 or struct.unpack_from('<I', h, 0)[0] != SIG:
        return {'kind': None, 'detail': '非PST/OST(无!BDN签名)'}
    it = h[0x0A]
    tname, bits = TYPES.get(it, ('未知版本0x%02X' % it, 0))
    out = {'kind': 'pst', 'index_type': tname, 'bits': bits,
           'page_size': 4096 if it == 0x24 else 512,
           'encryption': h[0x201] if bits else h[0x1CD]}
    if bits == 64:
        out['desc_root'] = struct.unpack_from('<Q', h, 0xE0)[0]
        out['block_root'] = struct.unpack_from('<Q', h, 0xF0)[0]
        out['file_end'] = struct.unpack_from('<Q', h, 0xB8)[0]
    elif bits == 32:
        out['desc_root'] = struct.unpack_from('<I', h, 0xBC)[0]
        out['block_root'] = struct.unpack_from('<I', h, 0xC4)[0]
        out['file_end'] = struct.unpack_from('<I', h, 0xA8)[0]
    import os
    out['size'] = os.path.getsize(source)
    return out


def _page_meta(buf, bits):
    """B树页尾元数据 → (count, capacity, entry_size, is_leaf, backlink)"""
    if len(buf) >= 4096:
        # 4K页(Outlook 2013+): 计数/容量两字节@0xfd8/0xfda, 条目大小1B@0xfdc,
        # 叶标志1B@0xfdd, 回链8B@0xff0 (libpst ITEM_COUNT_OFFSET4K等)
        count = struct.unpack_from('<H', buf, 0xFD8)[0]
        cap = struct.unpack_from('<H', buf, 0xFDA)[0]
        esz = buf[0xFDC]
        leaf = buf[0xFDD] == 0
        back = struct.unpack_from('<Q', buf, 0xFF0)[0]
    elif bits == 64:
        # libpst的read_twobyte实为单字节语义: 计数/容量/条目大小/叶标志各占1字节
        count = buf[0x1E8]
        cap = buf[0x1E9]
        esz = buf[0x1EA]
        leaf = buf[0x1EB] == 0
        back = struct.unpack_from('<Q', buf, 0x1F8)[0]
    else:
        count = buf[0x1F0]
        cap = buf[0x1F1]
        esz = buf[0x1F2]
        leaf = buf[0x1F3] == 0
        back = struct.unpack_from('<I', buf, 0x1F8)[0]
    return count, cap, esz, leaf, back


def _walk_desc(f, off, bits, out, seen, depth=0, page=512):
    """desc B树走查(迭代式) → [{'d_id','desc_id','tree_id','parent'}]"""
    if depth > 8:
        return
    if off in seen or not off:
        return
    seen.add(off)
    f.seek(off)
    buf = f.read(page)
    if len(buf) < page:
        return
    count, cap, esz, leaf, back = _page_meta(buf, bits)
    if count > cap or cap * esz > page or esz not in (24, 32):
        return
    pos = 0
    if leaf:
        last = 0
        for i in range(count):
            if bits == 64:
                d_id, desc_id, tree_id, parent = struct.unpack_from('<QQQI', buf, pos)
                pos += 32
            else:
                d_id, desc_id, tree_id, parent = struct.unpack_from('<IIII', buf, pos)
                pos += 16
            if d_id <= last and i:
                return                        # 不升序=坏页
            last = d_id
            out.append({'d_id': d_id, 'desc_id': desc_id, 'tree_id': tree_id,
                        'parent': parent})
    else:
        for i in range(count):
            if bits == 64:
                start, u1, child = struct.unpack_from('<QQQ', buf, pos)
                pos += 24
            else:
                start, u1, child = struct.unpack_from('<III', buf, pos)
                pos += 12
            _walk_desc(f, child, bits, out, seen, depth + 1, page)


def _walk_blocks(f, off, bits, out, seen, depth=0, page=512):
    """块 B树走查 → {id: (offset, size)}"""
    if depth > 8:
        return
    if off in seen or not off:
        return
    seen.add(off)
    f.seek(off)
    buf = f.read(page)
    if len(buf) < page:
        return
    count, cap, esz, leaf, back = _page_meta(buf, bits)
    if count > cap or cap * esz > page or esz not in (24, 28):
        return
    pos = 0
    if leaf:
        last = 0
        for i in range(count):
            if bits == 4:
                bid, boff = struct.unpack_from('<QQ', buf, pos)
                sz = struct.unpack_from('<H', buf, pos + 16)[0]
                pos += 28
            elif bits == 64:
                bid, boff = struct.unpack_from('<QQ', buf, pos)
                sz = struct.unpack_from('<H', buf, pos + 16)[0]
                pos += 24
            else:
                bid, boff, sz = struct.unpack_from('<IIH', buf, pos)
                pos += 12
            if bid <= last and i:
                return
            last = bid
            out[bid] = (boff, sz)
    else:
        for i in range(count):
            if bits == 64:
                start, u1, child = struct.unpack_from('<QQQ', buf, pos)
                pos += 24
            else:
                start, u1, child = struct.unpack_from('<III', buf, pos)
                pos += 12
            _walk_blocks(f, child, bits, out, seen, depth + 1, page)


def analyze(source):
    """结构体检: 两棵B树+目录树+孤儿 → 报告dict"""
    d = detect(source)
    if d.get('kind') != 'pst':
        return d
    if not d.get('desc_root'):
        d['healthy'] = False
        d['detail'] = '头内B树根指针无效(头损) — 可用recover_scan兜底'
        return d
    entries = []
    blocks = {}
    pg = d.get('page_size', 512)
    with open(source, 'rb') as f:
        _walk_desc(f, d['desc_root'], d['bits'], entries, set(), page=pg)
        _walk_blocks(f, d['block_root'], d['bits'], blocks, set(), page=pg)
    if not entries:
        d['healthy'] = False
        d['detail'] = '头指针指向的B树不可读(头损或格式不认识) — 可用recover_scan兜底'
        return d
    d['healthy'] = True
    d['desc_count'] = len(entries)
    d['block_count'] = len(blocks)
    # 节点类型直方图(低5位类型域)
    hist = {}
    folders = {}
    msgs = []
    for e in entries:
        t = e['d_id'] & 0x1F
        hist[t] = hist.get(t, 0) + 1
        if t == NID_FOLDER and e['d_id'] != 0x2142:
            folders[e['d_id']] = e
        elif t == NID_MESSAGE:
            msgs.append(e)
    d['histogram'] = hist
    d['folder_count'] = len(folders)
    d['message_count'] = len(msgs)
    # 孤儿: parent不在已知d_id集合
    ids = {e['d_id'] for e in entries}
    orphans = [e for e in msgs if e['parent'] not in ids and e['parent'] not in (0, 0x2142)]
    d['orphan_messages'] = len(orphans)
    # 数据块缺失的邮件
    missing = sum(1 for e in msgs if e['desc_id'] and e['desc_id'] not in blocks)
    d['missing_blocks'] = missing
    return d


def recover_scan(source, on_progress=None, cancel=None):
    """头损兜底: 全盘扫desc叶页特征(512B对齐) → 邮件清单
    判据: 页尾计数/容量/条目大小合理 + d_id升序 + 高位类型域合理"""
    import os
    d = detect(source)
    if d.get('kind') != 'pst':
        return d
    bits = d.get('bits', 64)
    pg = d.get('page_size', 512)
    found = []
    seen_pages = set()
    with open(source, 'rb') as f:
        size = os.path.getsize(source)
        pos = pg
        CH = 8 * 1024 * 1024
        while pos < size:
            if cancel is not None and cancel.is_set():
                break
            f.seek(pos)
            buf = f.read(CH)
            if not buf:
                break
            for off in range(0, len(buf) - pg + 1, pg):
                page = buf[off:off + pg]
                count, cap, esz, leaf, back = _page_meta(page, bits)
                if not leaf or not (0 < count <= cap <= (400 if pg == 4096 else 60)) or esz != 32:
                    continue
                ok = True
                last = 0
                n = 0
                for i in range(count):
                    d_id = struct.unpack_from('<Q', page, i * 32)[0]
                    if d_id <= last and i:
                        ok = False
                        break
                    last = d_id
                    if (d_id >> 5) > (1 << 32) or (d_id & 0x1F) > 0x1F:
                        ok = False
                        break
                    n += 1
                if ok and n >= 2:
                    poff = pos + off
                    if poff not in seen_pages:
                        seen_pages.add(poff)
                        for i in range(count):
                            d_id, desc_id, tree_id, parent = struct.unpack_from(
                                '<QQQI', page, i * 32)
                            if (d_id & 0x1F) == NID_MESSAGE:
                                found.append({'d_id': d_id, 'desc_id': desc_id,
                                              'parent': parent,
                                              'page': poff})
            pos += len(buf)
            if on_progress and pos % (64 * 1024 * 1024) < CH:
                on_progress({'event': 'progress', 'bytes': pos, 'total': size})
    return {'kind': 'pst', 'page_size': pg, 'recovered_pages': len(seen_pages),
            'recovered_messages': len(found),
            'messages': found[:5000], 'bits': bits}
