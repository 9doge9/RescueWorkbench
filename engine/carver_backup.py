# -*- coding: utf-8 -*-
"""
E盘镜像签名扫描恢复工具
- 流式分块扫描, 大窗口重叠, 断点续扫
- 每种格式独立解析器: 精确定长优先(PNG/MP4/WAV/BMP/7z), 结构化找尾(JPG/PDF/ZIP), 兜底截断(TIFF/RAR/DOC)
- SHA256去重, CSV报告, 恢复失败/可疑单独标记
"""
import sys, os, re, csv, hashlib, struct, json, time

CHUNK = 64 * 1024 * 1024          # 主扫描块
OVERLAP = 32 * 1024 * 1024        # 块重叠(跨块签名)
MAX_FILE = 2 * 1024**3            # 单文件上限2GB
MIN_FILE = 64                     # 小于此视为噪声

class Sig:
    __slots__ = ('name', 'head', 'parser')
    def __init__(self, name, head, parser):
        self.name, self.head, self.parser = name, head, parser

class CachedReader:
    """带滑动窗口缓存的镜像读取: 解析器的前向扫描不再逐次打磁盘"""
    def __init__(self, path, winsz=96*1024*1024):
        self.f = open(path, 'rb')
        self.winsz = winsz
        self.win = b''
        self.wstart = 0
        self.filesize = os.path.getsize(path)
    def _load(self, off, n):
        self.wstart = off
        want = min(self.winsz, max(n, 4*1024*1024))
        self.f.seek(off)
        self.win = self.f.read(want)
    def read(self, off, n):
        if off < 0 or off + n > self.filesize:
            return None
        if not (self.wstart <= off and off + n <= self.wstart + len(self.win)):
            self._load(off, n)
            if len(self.win) < n:
                return None
        return self.win[off-self.wstart : off-self.wstart+n]

CR = None  # 全局CachedReader, scan()时初始化

def read_at(f, off, n):
    # f参数保留兼容; 优先走缓存
    if CR is not None:
        return CR.read(off, n)
    f.seek(off)
    b = f.read(n)
    return b if len(b) == n else None

def cap(off, end_cap):
    return end_cap - off if end_cap > off else 0

# ---------- 各格式解析器: 返回 (length, ext) 或 None ----------
# buf: 文件头所在块的字节切片; f: 镜像文件对象; off: 文件头绝对偏移

def p_jpg(buf, f, off):
    if buf[:2] != b'\xff\xd8' or buf[2] != 0xff:
        return None
    # 走marker段结构到SOS, 然后找FFD9
    pos = 2
    sof_seen = False
    while pos + 4 < len(buf):
        m = buf[pos]
        if m != 0xff:
            return None
        tag = buf[pos+1]
        if tag in (0xd8, 0x01) or 0xd0 <= tag <= 0xd7:
            pos += 2; continue
        if tag == 0xd9:
            return None
        if pos + 4 > len(buf):
            # 头跨块, 补读
            nb = read_at(f, off + pos, 4096)
            if nb is None: return None
            buf2 = nb
            m = buf2[0]
            if m != 0xff: return None
            tag = buf2[1]
            seglen = struct.unpack('>H', buf2[2:4])[0] if len(buf2) >= 4 else 0
            if tag == 0xda:
                if not sof_seen: return None
                return _jpg_find_eoi(f, off, pos, sof_seen)
            if 0xc0 <= tag <= 0xcf and tag not in (0xc4, 0xc8, 0xcc) and len(buf2) >= 9:
                hh = struct.unpack('>H', buf2[5:7])[0]
                w = struct.unpack('>H', buf2[7:9])[0]
                if w == 0 or hh == 0 or w > 30000 or hh > 30000: return None
                sof_seen = True
            pos += 2 + seglen
            continue
        seglen = struct.unpack('>H', buf[pos+2:pos+4])[0]
        if seglen < 2: return None
        if tag == 0xda:  # SOS
            if not sof_seen: return None
            return _jpg_find_eoi(f, off, pos, sof_seen)
        if 0xc0 <= tag <= 0xcf and tag not in (0xc4, 0xc8, 0xcc):
            # SOF尺寸合法性: 过滤照片数据里嵌的假JPEG
            if pos + 9 > len(buf):
                nb = read_at(f, off + pos, 16)
                if nb is None or len(nb) < 9: return None
                hh = struct.unpack('>H', nb[5:7])[0]; w = struct.unpack('>H', nb[7:9])[0]
            else:
                hh = struct.unpack('>H', buf[pos+5:pos+7])[0]; w = struct.unpack('>H', buf[pos+7:pos+9])[0]
            if w == 0 or hh == 0 or w > 30000 or hh > 30000: return None
            sof_seen = True
        pos += 2 + seglen
    return None

def _jpg_find_eoi(f, off, sos_pos, sof_seen):
    # SOS段自身长度
    hdr = read_at(f, off + sos_pos, 4)
    if hdr is None: return None
    seglen = struct.unpack('>H', hdr[2:4])[0]
    data_start = sos_pos + 2 + seglen
    # 从熵数据起点流式找 FFD9 (搜索半径512MB, 防嵌套假JPEG拖死扫描)
    pos = off + data_start
    step = 8 * 1024 * 1024
    tail = b''
    EOI_LIMIT = 512 * 1024 * 1024
    while pos < off + EOI_LIMIT:
        blk = read_at(f, pos, step)
        if blk is None:
            blk = read_at(f, pos, 1)  # 到文件尾
            if blk is None or not blk:
                tail += b''
                break
            blk = read_at(f, pos, step) or b''
            if not blk: break
        search = tail + blk
        i = search.find(b'\xff\xd9')
        if i >= 0:
            total = (pos - off) - len(tail) + i + 2
            if total < MIN_FILE or total > MAX_FILE: return None
            return (total, 'jpg')
        tail = search[-1:]
        pos += len(blk)
    return None

def p_png(buf, f, off):
    if buf[:8] != b'\x89PNG\r\n\x1a\n': return None
    pos = 8
    while True:
        hdr = read_at(f, off + pos, 8) if pos + 8 > len(buf) else buf[pos:pos+8]
        if hdr is None or len(hdr) < 8: return None
        ln = struct.unpack('>I', hdr[:4])[0]
        typ = hdr[4:8]
        pos += 8 + ln + 4
        if typ == b'IEND':
            if pos > MAX_FILE or pos < MIN_FILE: return None
            return (pos, 'png')
        if ln > 0x7fffffff: return None
    return None

MP4_BRANDS = (b'mp41', b'mp42', b'isom', b'iso2', b'avc1', b'heic', b'heix', b'heim',
              b'msf1', b'hevc', b'qt  ', b'M4A ', b'M4V ', b'isom', b'iso4', b'iso6',
              b'mp42', b'3gp4', b'3gp5', b'3g2 ', b'mp4v', b'f4v ', b'CAEP', b'CMF ', b'dash')

def p_mp4(buf, f, off):
    # size(4) 'ftyp' brand...
    if buf[4:8] != b'ftyp' or buf[8:12] not in MP4_BRANDS: return None
    pos = 0
    last_end = 0
    iters = 0
    seen_moov = seen_mdat = False
    while pos < MAX_FILE and iters < 4096:
        iters += 1
        hdr = read_at(f, off + pos, 8) if pos + 8 > len(buf) else buf[pos:pos+8]
        if hdr is None or len(hdr) < 8: break
        sz, typ = struct.unpack('>I', hdr[:4])[0], hdr[4:8]
        hdrsz = 8
        if sz == 1:
            ext = read_at(f, off + pos + 8, 8)
            if ext is None: break
            sz = struct.unpack('>Q', ext)[0]
            hdrsz = 16
        elif sz == 0:
            break  # 原子延伸到文件尾: 以已走到的位置为文件末
        if sz < hdrsz or sz > MAX_FILE: break
        if not all(0x20 <= c < 0x7f for c in typ): break
        if typ == b'moov': seen_moov = True
        if typ == b'mdat': seen_mdat = True
        pos += sz
        last_end = pos
    # 真视频必须带moov或mdat原子, 上限1.5GB
    if last_end >= 4096 and (seen_moov or seen_mdat) and last_end <= 1536*1024*1024:
        return (last_end, 'mp4')
    return None

def p_mp4_wrap(buf, f, off):
    r = p_mp4(buf, f, off)
    if r: return r
    # ftyp不符合brand表但走atom结构成功也可能: 简化返回None
    return None

def p_riff(buf, f, off):
    # RIFF<size>WAVE / AVI / AVI_
    if buf[:4] != b'RIFF': return None
    kind = buf[8:12]
    if kind == b'WAVE': ext = 'wav'
    elif kind in (b'AVI ', b'AVIX'): ext = 'avi'
    elif kind == b'WEBP': ext = 'webp'
    else: return None
    sz = struct.unpack('<I', buf[4:8])[0]
    total = 8 + sz
    if MIN_FILE <= total <= MAX_FILE: return (total, ext)
    return None

def p_bmp(buf, f, off):
    if buf[:2] != b'BM': return None
    try:
        sz = struct.unpack('<I', buf[2:6])[0]
        x = struct.unpack('<i', buf[18:22])[0]
        y = struct.unpack('<i', buf[22:26])[0]
    except struct.error:
        return None
    if not (0 < x <= 30000 and -30000 <= y <= 30000): return None
    if 64 <= sz <= 512*1024**2 and sz < cap(off, off+MAX_FILE):
        return (sz, 'bmp')
    return None

def p_pdf(buf, f, off):
    if buf[:5] != b'%PDF-': return None
    # 找最后一个%%EOF(允许适度向后)
    pos = off
    last = -1
    step = 16 * 1024 * 1024
    tail = b''
    while pos < off + 512*1024**2:
        blk = read_at(f, pos, step)
        if not blk: break
        search = tail + blk
        i = 0
        while True:
            i = search.find(b'%%EOF', i)
            if i < 0: break
            last = pos - len(tail) + i
            i += 5
        tail = search[-5:]
        pos += len(blk)
    if last < 0: return None
    total = last + 5 - off
    if MIN_FILE <= total <= MAX_FILE: return (total, 'pdf')
    return None

def p_zip(buf, f, off):
    if buf[:4] != b'PK\x03\x04': return None
    # 本地头预校验: 压缩方法/DOS时间合理性, 过滤压缩数据里的假PK头
    try:
        method = struct.unpack('<H', buf[8:10])[0]
        dtime = struct.unpack('<H', buf[10:12])[0]
        ddate = struct.unpack('<H', buf[12:14])[0]
    except struct.error:
        return None
    if method not in (0, 1, 8, 9, 12, 14, 93, 95, 97, 98): return None
    year = (ddate >> 9) + 1980 if ddate else 0
    if not (1980 <= year <= 2100) and ddate != 0: return None
    if dtime == 0 and ddate == 0 and method == 0: pass
    # 正向找EOCD, 且EOCD的cd_offset+cd_size必须与本文件起点自洽
    pos = off
    step = 8 * 1024 * 1024
    tail = b''
    limit = off + 16 * 1024 * 1024
    while pos < limit:
        blk = read_at(f, pos, step)
        if not blk: break
        search = tail + blk
        i = 0
        while True:
            i = search.find(b'PK\x05\x06', i)
            if i < 0: break
            if len(search) >= i + 22:
                cd_size = struct.unpack('<I', search[i+12:i+16])[0]
                cd_off  = struct.unpack('<I', search[i+16:i+20])[0]
                cmt     = struct.unpack('<H', search[i+20:i+22])[0]
                eocd_abs = pos - len(tail) + i
                cd_end_abs = off + cd_off + cd_size
                # EOCD应紧跟central directory结尾(允许<=1024字节松量: 前置数据/zip64 locator)
                if 0 <= eocd_abs - cd_end_abs <= 1024:
                    total = eocd_abs + 22 + cmt - off
                    if 8192 <= total <= MAX_FILE:   # <8KB的zip是应用缓存垃圾, 跳过
                        return (total, 'zip')
            i += 4
        tail = search[-24:]
        pos += len(blk)
    return None

def p_rar(buf, f, off):
    if buf[:7] != b'Rar!\x1a\x07': return None
    # 无可靠结尾: 截到下一个主流签名或上限512MB(交给验证阶段过滤)
    return (None, 'rar')

def p_7z(buf, f, off):
    if buf[:6] != b'7z\xbc\xaf\x27\x1c': return None
    # 32字节签名头 = magic(6)+version(2)+crc(4)+startheader(nh_offset8+nh_size8+nh_crc4)
    try:
        nh_off, nh_size = struct.unpack('<QQ', buf[12:28])
    except struct.error:
        return None
    total = 32 + nh_off + nh_size
    if MIN_FILE <= total <= MAX_FILE: return (total, '7z')
    return None

def p_gif(buf, f, off):
    if buf[:4] not in (b'GIF8',): return None
    if buf[4:6] not in (b'7a', b'9a'): return None
    return (None, 'gif')

def p_tiff(buf, f, off):
    if buf[:4] not in (b'II*\x00', b'MM\x00*'): return None
    return (None, 'tiff')

def p_mp3(buf, f, off):
    if buf[:3] == b'ID3' and buf[3] < 5 and buf[4] < 0xff:
        return (None, 'mp3')
    # 裸MPEG音频帧 FFFB/FFF3/FFF2 + 版本/层校验
    if buf[0] == 0xff and (buf[1] & 0xe0) == 0xe0:
        ver = (buf[1] >> 3) & 3; lay = (buf[1] >> 1) & 3
        if ver != 1 and lay != 0:
            return (None, 'mp3')
    return None

def p_ole(buf, f, off):
    # MS Office 97-2003 / MSI
    if buf[:8] != b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1': return None
    return (None, 'doc')   # doc/xls/msi 统一先存doc后缀, 分类阶段用OLE目录名细分

def p_psd(buf, f, off):
    if buf[:4] != b'8BPS': return None
    if buf[4:6] != b'\x00\x01': return None
    return (None, 'psd')

def p_wmf(buf, f, off):
    if buf[:4] in (b'\xd7\xcd\xc6\x9a', b'\x01\x00\x00\x00') and buf[:2] != b'\x01\x00' :
        return (None, 'wmf')
    if buf[:2] == b'\x01\x00' and buf[40:46] == b'\x01\x00\x09\x00\x00\x03' if len(buf) >= 46 else False:
        return (None, 'wmf')
    return None

def p_sqlite(buf, f, off):
    if buf[:16] == b'SQLite format 3\x00':
        try:
            pg = struct.unpack('>H', buf[16:18])[0]
            if pg in (512,1024,2048,4096,8192,16384,32768,65536):
                return (None, 'sqlite')
        except struct.error:
            return None
    return None

SIGS = [
    Sig('jpg',   b'\xff\xd8\xff', p_jpg),
    Sig('png',   b'\x89PNG\r\n\x1a\n', p_png),
    Sig('mp4',   b'\x00\x00\x00.ftyp', p_mp4_wrap),   # 前缀仅示意, 实际用正则
    Sig('riff',  b'RIFF', p_riff),
    Sig('bmp',   b'BM', p_bmp),
    Sig('pdf',   b'%PDF-', p_pdf),
    Sig('zip',   b'PK\x03\x04', p_zip),
    Sig('rar',   b'Rar!\x1a\x07', p_rar),
    Sig('7z',    b'7z\xbc\xaf\x27\x1c', p_7z),
    Sig('gif',   b'GIF8', p_gif),
    Sig('tiff',  b'II*\x00', p_tiff),
    Sig('tiff2', b'MM\x00*', p_tiff),
    Sig('mp3',   b'ID3', p_mp3),
    Sig('ole',   b'\xd0\xcf\x11\xe0', p_ole),
    Sig('psd',   b'8BPS', p_psd),
    Sig('sqlite',b'SQLite format 3\x00', p_sqlite),
]

FTYP_RE = re.compile(rb'.{4}ftyp')

def scan(img, outdir, state_path):
    f = open(img, 'rb')
    total = os.path.getsize(img)
    state = {'pos': 0, 'found': 0}
    if os.path.exists(state_path):
        state = json.load(open(state_path))
    global CR
    CR = CachedReader(img)
    f.seek(state['pos'])
    os.makedirs(outdir, exist_ok=True)
    csvf = open(os.path.join(outdir, 'report.csv'), 'a', newline='', encoding='utf-8-sig')
    wr = csv.writer(csvf)
    if state['pos'] == 0:
        wr.writerow(['offset', 'size', 'ext', 'sha256', 'file', 'precise'])
    seen_hash = set()
    # 载入已有hash去重
    rep = os.path.join(outdir, 'report.csv')
    if os.path.exists(rep):
        try:
            for row in csv.reader(open(rep, encoding='utf-8-sig')):
                if len(row) >= 4 and row[3]: seen_hash.add(row[3])
        except Exception:
            pass

    buf = b''
    base = state['pos']
    skip_below = state['pos']   # 该位置之前的候选已在上一轮处理
    t0 = time.time()
    t_last = t0
    bytes_last = base
    while base < total:
        blk = f.read(CHUNK)
        if not blk: break
        buf = blk if not buf else buf + blk
        end = base + len(buf)
        candidates = []  # (off, sig)
        # 普通签名
        for s in SIGS:
            if s.name == 'mp4': continue
            i = 0
            while True:
                i = buf.find(s.head, i, min(len(buf), end - base) )
                if i < 0: break
                candidates.append((base + i, s))
                i += 1
        # ftyp
        for m in FTYP_RE.finditer(buf):
            candidates.append((base + m.start(), SIGS[2]))
        candidates.sort()
        parsed_count = {}
        for absoff, s in candidates:
            if absoff < skip_below: continue   # 重叠区已处理过
            # 垃圾森林防护: 同一块内单签名解析超预算则跳过剩余
            budget = 800 if s.name == 'zip' else 100000
            c = parsed_count.get(s.name, 0)
            if c >= budget: continue
            parsed_count[s.name] = c + 1
            hdr = read_at(f, absoff, 4096)
            if hdr is None: continue
            r = s.parser(hdr, f, absoff)
            if not r: continue
            ln, ext = r
            precise = ln is not None
            if ln is None:
                ln = guess_tail(f, absoff)
            if ln < MIN_FILE or ln > MAX_FILE: continue
            blob = read_at(f, absoff, ln)
            if blob is None:
                blob = b''
                f2 = open(img, 'rb'); f2.seek(absoff)
                while len(blob) < ln:
                    c = f2.read(min(16*1024**2, ln - len(blob)))
                    if not c: break
                    blob += c
                f2.close()
                if len(blob) < ln * 0.9: continue
                ln = len(blob)
            h = hashlib.sha256(blob).hexdigest()
            if h in seen_hash: continue
            seen_hash.add(h)
            fn = os.path.join(outdir, f'{s.name}_f{absoff:x}.{ext}')
            with open(fn, 'wb') as w:
                w.write(blob)
            wr.writerow([hex(absoff), ln, ext, h, os.path.basename(fn), 'Y' if precise else 'N'])
            state['found'] += 1
        csvf.flush()
        # 保留重叠
        keep = min(OVERLAP, len(buf))
        buf = buf[-keep:]
        base = end - keep
        skip_below = base
        state['pos'] = base
        json.dump(state, open(state_path, 'w'))
        now = time.time()
        if now - t_last >= 10:
            spd = (base - bytes_last) / (now - t_last) / 1024**2
            eta = (total - base) / max(spd,1) / 1024**2 / 60
            print(f'{base/1024**3:.1f}/{total/1024**3:.1f}GB  {spd:.0f}MB/s  剩余~{eta:.0f}分  已找到{state["found"]}', flush=True)
            t_last, bytes_last = now, base
        f.seek(end)
    print(f'完成: 共恢复 {state["found"]} 个文件', flush=True)
    csvf.close()

def guess_tail(f, off):
    """无结尾标志的格式: 取到下一个主流文件头, 或128MB上限"""
    NEXT_HEADS = [b'\xff\xd8\xff', b'\x89PNG', b'PK\x03\x04', b'%PDF-', b'RIFF',
                  b'Rar!\x1a\x07', b'\xd0\xcf\x11\xe0', b'GIF8']
    step = 8*1024*1024
    pos = off + 1024*1024   # 至少1MB, 避免文件内部误命中
    tail = b''
    while pos < off + 32*1024**2:
        f.seek(pos)
        blk = f.read(step)
        if not blk:
            return pos - off
        search = tail + blk
        best = -1
        for h in NEXT_HEADS:
            i = search.find(h)
            if i >= 0 and (best < 0 or i < best):
                best = i
        if best >= 0:
            ln = pos - off + best - len(tail)
            return max(ln, 1024*1024)
        tail = search[-16:]
        pos += len(blk)
    return 32*1024**2

if __name__ == '__main__':
    img, outdir, state = sys.argv[1], sys.argv[2], sys.argv[3]
    scan(img, outdir, state)
