# -*- coding: utf-8 -*-
"""SQLite残留分析 — 聊天记录/浏览器历史库的体检+自由页挖掘
场景: 微信/QQ/浏览器的历史记录是SQLite库; 删除的消息往往还躺在自由页(freelist)里,
      库文件被删后也可按magic从镜像里雕刻回来。
注意: 新版微信库是SQLCipher加密(WCDB), 非明文SQLite头——如实报告"已加密", 解密不在本模块。
"""
import os
import re
import struct
import sqlite3

MAGIC = b'SQLite format 3\x00'


def is_sqlite(path, read_head=None):
    """明文SQLite? (头16字节magic+合法页大小)"""
    try:
        head = read_head if read_head is not None else _head(path)
        if head is None or head[:16] != MAGIC:
            return False
        ps = struct.unpack_from('>H', head, 16)[0]
        if ps == 1:
            ps = 65536
        return ps in (512, 1024, 2048, 4096, 8192, 16384, 32768, 65536)
    except (OSError, struct.error):
        return False


def _head(path, n=100):
    with open(path, 'rb') as f:
        return f.read(n)


def looks_encrypted_sqlite(path):
    """WCDB(SQLCipher)等加密库: 第一个page被加密, 但页大小等结构处仍残留熵特征——
    简化判据: 扩展名.db/.db_sqlite且前16字节不是明文magic且前4KB高熵"""
    try:
        with open(path, 'rb') as f:
            b = f.read(4096)
        if len(b) < 512:
            return False
        ent = _entropy(b)
        return ent > 7.2
    except OSError:
        return False


def _entropy(b):
    if not b:
        return 0
    freq = [0] * 256
    for x in b:
        freq[x] += 1
    n = len(b)
    import math
    e = 0.0
    for c in freq:
        if c:
            p = c / n
            e -= p * math.log2(p)
    return e


def inventory(path):
    """库体检: 页信息+表清单+行数; 打不开的库给原因"""
    out = {'path': path, 'is_sqlite': False, 'encrypted': False,
           'tables': [], 'error': None, 'freelist_pages': 0, 'page_size': 0,
           'page_count': 0}
    try:
        head = _head(path)
    except OSError as e:
        out['error'] = str(e)
        return out
    out['is_sqlite'] = is_sqlite(path, head)
    if not out['is_sqlite']:
        # 加密判定只对数据库类扩展名做——否则jpg/zip等高熵文件全被误报
        if os.path.splitext(path)[1].lower() in ('.db', '.sqlite', '.db3', '.sqlite3') \
                and os.path.getsize(path) > 4096 and looks_encrypted_sqlite(path):
            out['encrypted'] = True
        else:
            out['error'] = '非SQLite格式'
        return out
    ps = struct.unpack_from('>H', head, 16)[0]
    out['page_size'] = 65536 if ps == 1 else ps
    out['page_count'] = struct.unpack_from('>I', head, 28)[0]
    out['freelist_pages'] = struct.unpack_from('>I', head, 36)[0]
    try:
        con = sqlite3.connect('file:%s?mode=ro&immutable=1' % _urlpath(path), uri=True)
        try:
            cur = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            for (t,) in cur.fetchall():
                try:
                    cnt = con.execute('SELECT COUNT(*) FROM "%s"' % t.replace('"', '""')).fetchone()[0]
                except sqlite3.DatabaseError:
                    cnt = -1
                out['tables'].append({'name': t, 'rows': cnt})
        finally:
            con.close()
    except sqlite3.DatabaseError as e:
        out['error'] = '库损坏或加密: %s' % e
    return out


def _urlpath(p):
    return p.replace('?', '%3f').replace('#', '%23')


def dump_table(path, table, limit=200):
    """通用取表: 行转竖线分隔字符串(截断保护)"""
    con = sqlite3.connect('file:%s?mode=ro&immutable=1' % _urlpath(path), uri=True)
    try:
        cur = con.execute('SELECT * FROM "%s" LIMIT %d' % (table.replace('"', '""'), limit))
        cols = [d[0] for d in cur.description]
        rows = []
        for r in cur.fetchall():
            rows.append([_cell(c) for c in r])
        return cols, rows
    finally:
        con.close()


def _cell(c):
    if c is None:
        return ''
    if isinstance(c, bytes):
        if len(c) > 64:
            return '<BLOB %d字节>' % len(c)
        return repr(c)
    s = str(c)
    return s if len(s) <= 200 else s[:200] + '…'


def free_pages_text(path, min_len=6, max_fragments=2000):
    """自由页(已删数据所在)文本挖掘 → 可读字符串片段列表
    自由页链: header偏移32=首个trunk页号, 36=总数; trunk页: [下一trunk, 本trunk叶页数, 叶页号...]"""
    head = _head(path)
    if not is_sqlite(path, head):
        return []
    ps = 65536 if struct.unpack_from('>H', head, 16)[0] == 1 else struct.unpack_from('>H', head, 16)[0]
    size = os.path.getsize(path)
    pages = size // ps
    trunk = struct.unpack_from('>I', head, 32)[0]
    count = struct.unpack_from('>I', head, 36)[0]
    leafs = []
    seen = set()
    while trunk and trunk <= pages and trunk not in seen and len(leafs) < count:
        seen.add(trunk)
        with open(path, 'rb') as f:
            f.seek((trunk - 1) * ps)
            tp = f.read(ps)
        if len(tp) < 8:
            break
        nxt, n = struct.unpack_from('>II', tp, 0)
        for i in range(min(n, (len(tp) - 8) // 4)):
            ln = struct.unpack_from('>I', tp, 8 + 4 * i)[0]
            if 1 <= ln <= pages:
                leafs.append(ln)
        trunk = nxt
    fragments = []
    with open(path, 'rb') as f:
        for ln in leafs[:512]:
            f.seek((ln - 1) * ps)
            page = f.read(ps)
            for s in _strings(page, min_len):
                fragments.append(s)
                if len(fragments) >= max_fragments:
                    return fragments
    return fragments


_CJK = re.compile(r'[\u4e00-\u9fff\u3000-\u303f]')


def _strings(page, min_len):
    """可打印串: ASCII或UTF-8中文(数据库文本多为UTF-8)"""
    out = []
    cur = bytearray()
    i = 0
    n = len(page)
    while i < n:
        b = page[i]
        if 32 <= b < 127:
            cur.append(b)
            i += 1
            continue
        # UTF-8多字节(2~4)
        if b >= 0xC0 and i + 1 < n:
            ln = 2 if b < 0xE0 else (3 if b < 0xF0 else 4)
            chunk = page[i:i + ln]
            if ln == 3 and len(chunk) == 3:
                cur += chunk                      # 常见中文3字节
                i += 3
                continue
        if cur:
            s = bytes(cur).decode('utf-8', 'ignore')
            if len(s) >= min_len and (s.isprintable()):
                out.append(s)
            cur = bytearray()
        i += 1
    if cur:
        s = bytes(cur).decode('utf-8', 'ignore')
        if len(s) >= min_len and s.isprintable():
            out.append(s)
    return out


def carve_from_image(source, start=0, end=None, max_dbs=200, on_progress=None):
    """按magic在镜像里找SQLite库 → [{offset, page_count, est_size, out}]
    大小取header页数×页大小(被截断的库也能按实际字节数收)"""
    if end is None:
        end = os.path.getsize(source)
    found = []
    with open(source, 'rb') as f:
        pos = start
        consumed_to = start
        CH = 8 * 1024 * 1024
        while pos < end - 100:
            f.seek(pos)
            buf = f.read(CH)
            if not buf:
                break
            i = 0
            while True:
                i = buf.find(MAGIC, i)
                if i < 0 or pos + i >= end:
                    break
                off = pos + i
                i += 1
                if off < consumed_to:
                    continue
                ps = struct.unpack_from('>H', buf, i + 16 - 1)[0] if i + 18 < len(buf) else 0
                ps = 65536 if ps == 1 else ps
                pc = struct.unpack_from('>I', buf, i + 28 - 1)[0] if i + 32 < len(buf) else 0
                if ps in (512, 1024, 2048, 4096, 8192, 16384, 32768, 65536) and pc > 0:
                    est = min(ps * pc, end - off)
                    found.append({'offset': off, 'page_count': pc, 'page_size': ps,
                                  'est_size': est})
                    consumed_to = off + est
                    if len(found) >= max_dbs:
                        return found
            pos += max(len(buf) - 64, 1)
            if on_progress and pos % (64 * 1024 * 1024) < CH:
                on_progress({'event': 'scan_progress', 'bytes_scanned': min(pos, end),
                             'bytes_total': end, 'found': len(found)})
    return found
