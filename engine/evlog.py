# -*- coding: utf-8 -*-
"""Windows事件日志(.evtx)解析 — 取证时间线 + 常见事件ID
场景: 事件日志记录了登录(4624)/USB接入(20001)/服务安装(7045)等——
      勒索取证、泄密溯源、"什么时候拷的文件"都靠它。

v2格式账(经真实System.evtx校准: wevtutil epl导出 + Get-WinEvent逐条对照):
  文件头128B('ElfFile\\0') → 64KB块('ElfChnk\\0') → 记录。
  记录头: 魔数(2a2a0000)→size→记录号→FILETIME→body ... 尾部size回写
          (魔数在size**前**! 旧版写反导致真实文件0命中——合成夹具自洽掩盖了这点)
  body:   [0f 01 xx xx fragment头4B(低半字节F则跳4)] [0c模板调用10B: token+unk+tid u32+toff u32]
          [驻留模板(toff>记录偏移): 24B头(next u32/id u32/guid12B/dlen u32@+20) + dlen字节binxml]
          替换数组: [count u32][(u16 size, u8 type, u8 pad)×n][值区按descriptor.size紧排]
  事件ID: 走模板binxml找<EventID>元素内的NormalSubstitution(0x0d)下标, 再到值区取数。
          元素名经块内字符串表解析(64×u32指针@0x80, NameString: next u32/hash u16/len u16/UTF-16LE)。
          不渲染完整XML, 只挖需要的两个元素(EventID) — 诚实边界: 极个别异形模板可能取不到(eid=None)。
"""
import os
import struct
from datetime import datetime, timezone
from collections import Counter

FILE_MAGIC = b'ElfFile\x00'
CHUNK_MAGIC = b'ElfChnk\x00'
REC_MAGIC = b'\x2a\x2a\x00\x00'       # 记录魔数字节序(LE u32=0x00002a2a)
CHUNK_SIZE = 65536
EPOCH_DIFF = 116444736000000000  # 1601→1970 (100ns单位)

# 常见事件ID静态对照表(安全通道46xx/系统通道70xx/7045高危/USB/Defender/RDP/WLAN)
EVENT_NAMES = {
    41: ('异常断电/崩溃后重启', 'bad'),
    104: ('系统日志被清除', 'warn'),
    1074: ('计划内关机/重启(记录发起进程)', 'warn'),
    1102: ('安全审核日志被清除(高危)', 'bad'),
    1149: ('RDP远程连接成功', 'warn'),
    1116: ('Defender检出威胁', 'bad'),
    1117: ('Defender对威胁采取动作', 'bad'),
    20001: ('USB设备驱动安装(接入痕迹)', 'usb'),
    4624: ('登录成功', ''),
    4625: ('登录失败(暴力破解痕迹)', 'warn'),
    4634: ('注销', ''),
    4647: ('用户发起注销', ''),
    4648: ('使用显式凭据登录(横向移动痕迹)', 'warn'),
    4672: ('管理员特权登录', 'warn'),
    4688: ('新进程创建(需审核策略开启)', ''),
    4698: ('计划任务创建', 'warn'),
    4699: ('计划任务删除', ''),
    4720: ('用户账户被创建(高危)', 'bad'),
    4724: ('用户密码被重置', 'bad'),
    4726: ('用户账户被删除', 'warn'),
    4732: ('账户加入本地管理员组(提权)', 'bad'),
    4740: ('账户被锁定', 'warn'),
    6005: ('事件日志服务启动(开机)', ''),
    6006: ('事件日志服务停止(关机)', ''),
    6008: ('意外关机', 'bad'),
    7036: ('服务启动/停止', ''),
    7040: ('服务启动类型被修改', 'warn'),
    7045: ('新服务安装(勒索/后门最常用)', 'bad'),
    8001: ('WLAN连接(含AP的MAC)', 'usb'),
    8002: ('WLAN断开', ''),
    8003: ('WLAN连接失败', ''),
}
# 值得单独点名的时间线事件(取证金线索; 7040太吵不进时间线但保留在统计表)
NOTABLE = {41, 104, 1074, 1102, 1149, 1116, 20001, 4625, 4648, 4672,
           4698, 4720, 4724, 4726, 4732, 4740, 6008, 7045, 8001}

# 值类型 → (是否可解, 字节数或None)
_SZ = {0x00: 0, 0x01: None, 0x02: None, 0x03: 1, 0x04: 1, 0x05: 2, 0x06: 2,
       0x07: 4, 0x08: 4, 0x09: 8, 0x0a: 8, 0x0b: 4, 0x0c: 8, 0x0d: 2,
       0x0e: None, 0x0f: 16, 0x10: 4, 0x11: 8, 0x12: 28, 0x14: 4, 0x15: 8}


def is_evtx(path):
    try:
        with open(path, 'rb') as f:
            return f.read(8) == FILE_MAGIC
    except OSError:
        return False


def _ft_to_dt(ft):
    """FILETIME(100ns since 1601) → UTC datetime"""
    if not ft:
        return None
    sec = (ft - EPOCH_DIFF) / 1e7
    if not (-62135596800 <= sec <= 253402300800):
        return None
    try:
        return datetime.fromtimestamp(sec, tz=timezone.utc)
    except (OSError, ValueError):
        return None


# ---------- 模板/字符串表 ----------

def _string(ch, off, cache):
    """块内字符串: next u32 / hash u16 / len u16(字符数) / UTF-16LE"""
    s = cache.get(off)
    if s is None:
        try:
            ln = struct.unpack_from('<H', ch, off + 6)[0]
            s = ch[off + 8: off + 8 + 2 * ln].decode('utf-16le', 'ignore')
        except (struct.error, IndexError):
            s = ''
        cache[off] = s
    return s


def _str_len(ch, off):
    """驻留NameString的占地长度: next u32+hash u16+len u16+UTF16(2N)+2 (python-evtx同款'2字节未入账')"""
    n = struct.unpack_from('<H', ch, off + 6)[0]
    return 2 * n + 10


def _val_skip(ch, o):
    """内联值(0x05): [token][type u8][data] → 跳过后的偏移 或 None(解不了)"""
    t = ch[o + 1]
    if t == 0x00:
        return o + 2
    if t in (0x01, 0x02, 0x0e):            # u16长度 串/串/二进制
        n = struct.unpack_from('<H', ch, o + 2)[0]
        if t == 0x01:
            n *= 2
        return o + 4 + n
    n = _SZ.get(t)
    if n is None or t in (0x13, 0x21, 0x81):
        return None
    return o + 2 + n


def _template_eventid_index(ch, toff, strings):
    """走模板binxml(24B头后, dlen字节) → <EventID>元素的替换(u16下标, u8类型) 或 None
    字节账经python-evtx节点树逐字节校准:
      元素0x01: [token][unk u16][size u32][string_off u32](11B) + 驻留名串(2N+10) + flags&0x40则再+4
      属性0x06: [token][string_off u32](5B) + 驻留名串 + 一个值节点(0x05或替换) — 无终结符
      0x02/0x03/0x04 各1字节; 替换0x0d/0x0e = 4B; StreamStart 0x0f01 = 4B
    """
    dlen = struct.unpack_from('<I', ch, toff + 20)[0]
    if dlen <= 0 or dlen > 65536:
        return None
    o = toff + 24
    end = o + dlen
    if o < end and ch[o] & 0x0F == 0x0F:
        o += 4                             # fragment头
    stack = []
    while o < end:
        t = ch[o]
        low = t & 0x0F
        if low == 0x01:                    # open element
            ereal = o
            sofs = struct.unpack_from('<I', ch, o + 7)[0]
            o += 11
            if sofs > ereal:               # 名串驻留在元素头后
                stack.append(_string(ch, sofs, strings))
                o += _str_len(ch, sofs)
            else:
                stack.append(_string(ch, sofs, strings))
            if t & 0x40:                   # flags & 0x04 → 额外4字节
                o += 4
        elif low == 0x02:                  # close start tag
            o += 1
        elif low == 0x03:                  # close empty element
            o += 1
            if stack:
                stack.pop()
        elif low == 0x04:                  # close element
            o += 1
            if stack:
                stack.pop()
        elif low == 0x05:                  # value
            o = _val_skip(ch, o)
            if o is None:
                return None
        elif low == 0x06:                  # attribute
            areal = o
            sofs = struct.unpack_from('<I', ch, o + 1)[0]
            o += 5
            if sofs > areal:               # 属性名串驻留
                o += _str_len(ch, sofs)
            vt = ch[o] if o < end else 0
            if vt == 0x05:                 # 属性值 = 值节点
                o = _val_skip(ch, o)
                if o is None:
                    return None
            elif vt & 0x0F in (0x0d, 0x0e):
                o += 4
            else:
                return None
        elif low in (0x0d, 0x0e):          # Normal/ConditionalSubstitution: 4B
            if stack and stack[-1] == 'EventID':
                return struct.unpack_from('<HB', ch, o + 1)
            o += 4
        elif low == 0x0f:
            o += 4
        elif low == 0x00:
            break
        else:                              # 陌生token(嵌套模板/cdata等) — 放弃该模板
            return None
    return None


def _record_eid(ch, rec, size, strings, tmpl_cache):
    """单条记录取事件ID(每处失败都fail-soft → None)"""
    try:
        o = rec + 24
        if ch[o] & 0x0F == 0x0F:
            o += 4
        if ch[o] != 0x0C:
            return None
        tid, toff = struct.unpack_from('<II', ch, o + 2)
        idx = tmpl_cache.get(tid)
        if idx is None:
            idx = _template_eventid_index(ch, toff, strings) or False
            tmpl_cache[tid] = idx
        if not idx:
            return None
        index, typ = idx
        if toff > rec:                     # 驻留模板: 子数组跟在模板数据后
            dlen = struct.unpack_from('<I', ch, toff + 20)[0]
            sub = toff + 24 + dlen
        else:                              # 引用模板: 子数组紧跟实例节点(10B)
            sub = o + 10
        cnt = struct.unpack_from('<I', ch, sub)[0]
        if not (0 < cnt <= 5000) or index >= cnt:
            return None
        p = sub + 4 + 4 * cnt              # 值区起点
        for i in range(cnt):
            dsz, dtyp = struct.unpack_from('<HB', ch, sub + 4 + 4 * i)
            if i == index:
                raw = ch[p:p + dsz]
                if dtyp in (0x05, 0x07):   # i16/i32
                    return int.from_bytes(raw[:2], 'little', signed=True)
                if dtyp in (0x06, 0x08, 0x10, 0x14):   # u16/u32/size/hex32
                    return int.from_bytes(raw[:4], 'little')
                if dtyp in (0x09, 0x0a):   # i64/u64
                    return int.from_bytes(raw[:8], 'little', signed=(dtyp == 0x09))
                if dtyp in (0x03, 0x04):
                    return raw[0] if raw else None
                return None
            p += dsz
    except (struct.error, IndexError):
        return None
    return None


# ---------- 主解析 ----------

def parse(path, on_progress=None):
    """evtx → {'records', 'chunks', 'bad_chunks', 'first', 'last',
               'timeline': [(dt, rid, eid|None)](截断5000),
               'events': [{'id','name','tag','count'}]按次数降序,
               'notable': [(dt, rid, eid)]高价值事件(截断500)}"""
    out = {'records': 0, 'chunks': 0, 'bad_chunks': 0,
           'first': None, 'last': None, 'timeline': [],
           'event_counts': {}, 'notable': []}
    if not is_evtx(path):
        out['error'] = '非evtx格式'
        return out
    size = os.path.getsize(path)
    with open(path, 'rb') as f:
        head = f.read(128)
        chunk_off = struct.unpack_from('<Q', head, 0x40)[0] or 4096
        chunk_cnt = struct.unpack_from('<Q', head, 0x48)[0]
        pos = chunk_off
        while pos + CHUNK_SIZE <= size:
            if chunk_cnt and out['chunks'] >= chunk_cnt:
                break
            f.seek(pos)
            chunk = f.read(CHUNK_SIZE)
            if len(chunk) < CHUNK_SIZE or chunk[:8] != CHUNK_MAGIC:
                out['bad_chunks'] += 1
                pos += CHUNK_SIZE
                continue
            out['chunks'] += 1
            _parse_chunk(chunk, out)
            if on_progress:
                on_progress({'event': 'progress', 'chunks': out['chunks'],
                             'records': out['records']})
            pos += CHUNK_SIZE
    tl = out['timeline']
    if tl:
        out['first'] = tl[0][0]
        out['last'] = tl[-1][0]
    out['timeline'] = tl[:5000]               # 防巨库撑爆
    evs = []
    for eid, n in out['event_counts'].items():
        name, tag = EVENT_NAMES.get(eid, ('', ''))
        evs.append({'id': eid, 'name': name, 'tag': tag, 'count': n})
    evs.sort(key=lambda e: -e['count'])
    out['events'] = evs[:40]
    out['notable'] = out['notable'][:500]
    del out['event_counts']
    return out


def _parse_chunk(chunk, out):
    """块内记录遍历: 真实布局 魔数(2a2a0000)→size→rid→FILETIME; 尾部size回写校验"""
    strings = {}                              # 本块字符串缓存
    tmpl_cache = {}                           # tid → (index,type) | False
    pos = 0x200
    end = len(chunk)
    while pos + 28 <= end:
        if chunk[pos:pos + 4] != REC_MAGIC:
            break
        size = struct.unpack_from('<I', chunk, pos + 4)[0]
        if size < 28 or size > end - pos:
            break
        tail = struct.unpack_from('<I', chunk, pos + size - 4)[0]
        if tail != size:
            break
        rec_id = struct.unpack_from('<Q', chunk, pos + 8)[0]
        ft = struct.unpack_from('<Q', chunk, pos + 16)[0]
        dt = _ft_to_dt(ft)
        eid = _record_eid(chunk, pos, size, strings, tmpl_cache)
        out['records'] += 1
        if dt:
            out['timeline'].append((dt, rec_id, eid))
        if eid:
            out['event_counts'][eid] = out['event_counts'].get(eid, 0) + 1
            if eid in NOTABLE:
                out['notable'].append((dt, rec_id, eid))
        pos += size
