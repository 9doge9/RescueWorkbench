# -*- coding: utf-8 -*-
"""H.264/H.265 裸流雕刻 — 监控(海康等)录像恢复
场景: 监控盘/导出流损坏后无容器, 只有Annex B裸流(00 00 01起始码+NAL单元),
      传统magic签名雕刻抓不到。
方法: 按SPS/PPS/IDR(或VPS/SPS/PPS/IRAP)序列定位流起点 → 沿NAL链走(严格类型
      校验防误链) → 解析SPS拿分辨率/fps(Exp-Golomb) → 输出裸流 + 纯Python
      重封装MP4(裸流原件始终保留)。
局限: 海康.dav(PS流)/DHFS文件系统在第三梯队; B帧不重排时间戳(可播但顺序
      可能不对, 结果如实标注fps来源); H.265 SPS分辨率v1不解析(命名不带尺寸)。
"""
import struct

SC = b'\x00\x00\x01'
CHUNK = 8 * 1024 * 1024
MAX_NAL = 8 * 1024 * 1024          # 单个NAL超过此长度视为垃圾
MAX_LOOKAHEAD = 512 * 1024         # NAL间找不到起始码超过此距离判流终止
MAX_SEG = 2 * 1024 * 1024 * 1024   # 单段上限(超出切part, 播放器也啃不动更大)

# NAL类型白名单(严格, 控制误报): 收到白名单外类型即断流
H264_OK = {1, 5, 6, 7, 8, 9, 12}   # P/IDR/SEI/SPS/PPS/AUD/filler
H265_OK = {0, 1, 2, 3, 16, 17, 18, 19, 20, 21, 32, 33, 34, 35, 39, 40}
H264_HEADS = (0x67, 0x27, 0x68, 0x28, 0x65, 0x45, 0x25)   # SPS/PPS/IDR(含idc=1变体)
H265_HEADS = (0x40, 0x42, 0x44, 0x26, 0x27, 0x28, 0x29)   # VPS/SPS/PPS/IRAP
VCL_H264 = {1, 5}
VCL_H265 = {0, 1, 2, 3, 16, 17, 18, 19, 20, 21}


# ---------- 位读取 + Exp-Golomb ----------

class BitReader:
    """RBSP位读取器(需先做00 00 03去转义)"""

    def __init__(self, data):
        self.d = data
        self.pos = 0

    def u(self, n):
        v = 0
        for _ in range(n):
            if self.pos >= len(self.d) * 8:
                raise ValueError('SPS越界')
            v = (v << 1) | ((self.d[self.pos >> 3] >> (7 - (self.pos & 7))) & 1)
            self.pos += 1
        return v

    def ue(self):
        z = 0
        while self.u(1) == 0:
            z += 1
            if z > 31:
                raise ValueError('Exp-Golomb异常')
        return (1 << z) - 1 + (self.u(z) if z else 0)

    def se(self):
        k = self.ue()
        return (k + 1) // 2 if k & 1 else -(k // 2)


def _rbsp_unescape(data):
    if b'\x00\x00\x03' not in data:
        return data
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        if i + 2 < n and data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 3:
            out += b'\x00\x00'
            i += 3
        else:
            out.append(data[i])
            i += 1
    return bytes(out)


# ---------- SPS解析(H.264) ----------

def parse_sps_h264(payload):
    """H.264 SPS → {width,height,profile,level,fps|None}; 失败返回None
    既当强校验(随机数据解不出合理分辨率), 也用于命名和fps推断"""
    try:
        r = BitReader(_rbsp_unescape(payload))
        profile = r.u(8)
        r.u(8)                                     # constraint flags
        level = r.u(8)
        if profile not in (66, 77, 88, 100, 110, 118, 122, 244, 44, 83, 86,
                           128, 138, 139, 134, 135):
            return None
        if not (10 <= level <= 62):
            return None
        r.ue()                                     # sps_id
        if profile in (100, 110, 118, 122, 244, 44, 83, 86, 138, 139, 134, 135):
            chroma = r.ue()
            if chroma == 3:
                r.u(1)
            if chroma > 3:
                return None
            r.ue(); r.ue()                         # bit_depth luma/chroma
            r.u(1)                                 # qpprime
            if r.u(1):                             # scaling_matrix → 放弃(校验已足够)
                return None
        r.ue()                                     # log2_max_frame_num_minus4
        poc = r.ue()
        if poc == 0:
            r.ue()
        elif poc == 1:
            r.u(1); r.se(); r.se(); r.ue()
            for _ in range(4):
                r.se()
        else:
            return None
        if r.ue() > 16:                            # max_num_ref_frames
            return None
        r.u(1)                                     # gaps_allowed
        w_mbs = r.ue()
        h_map = r.ue()
        if w_mbs > 268 or h_map > 268:             # >4320px不合理
            return None
        frame_mbs_only = r.u(1)
        if not frame_mbs_only:
            r.u(1)
        r.u(1)                                     # direct_8x8
        crop = [0, 0, 0, 0]
        if r.u(1):
            crop = [r.ue() for _ in range(4)]
        width = (w_mbs + 1) * 16 - (crop[0] + crop[1]) * 2
        height = (2 - frame_mbs_only) * (h_map + 1) * 16 - (crop[2] + crop[3]) * 2
        if not (16 <= width <= 4320 and 16 <= height <= 4320):
            return None
        fps = None
        if r.u(1):                                 # vui_present
            if r.u(1):                             # aspect_ratio
                r.u(8)
                if r.u(1):
                    r.u(16); r.u(16)
            r.u(1)                                 # overscan
            if r.u(1):                             # video_signal_type
                r.u(3); r.u(1)
                if r.u(1):
                    r.u(8)
            if r.u(1):                             # chroma_loc
                r.u(2); r.u(2)
            if r.u(1):                             # timing_info_present
                num = r.u(32)
                den = r.u(32)
                if num and den and r.u(1):        # fixed_frame_rate
                    fps = den / (2 * num)
                    if not (1 <= fps <= 240):
                        fps = None
        return {'width': width, 'height': height,
                'profile': profile, 'level': level / 10.0, 'fps': fps}
    except (ValueError, IndexError):
        return None


# ---------- NAL遍历 ----------

class _Win:
    """滑动窗口读取器: 顺序访问时缓存命中, 大读直读"""

    def __init__(self, f):
        self.f = f
        self.buf = b''
        self.base = 0

    def read(self, pos, n):
        try:
            if not (self.base <= pos and pos + n <= self.base + len(self.buf)):
                self.f.seek(pos)
                self.buf = self.f.read(max(n, CHUNK))
                self.base = pos
            off = pos - self.base
            return self.buf[off:off + n]
        except OSError:
            return b''


def _next_sc(win, frm, limit):
    """从frm找下一个起始码(严格不越过limit) → (sc_pos, payload_end=前导零回退后)
    没有则返回 (None, None)。注意: 匹配点也必须在limit内, 否则长距离跳跃会
    把随机数据误链成流。"""
    pos = frm
    while pos < limit:
        buf = win.read(pos, CHUNK)
        if not buf:
            return None, None
        scan_end = min(len(buf), limit - pos + 2)
        i = buf.find(SC, 1, scan_end)
        if i >= 0 and pos + i < limit:
            j = i
            while j > 0 and buf[j - 1] == 0:
                j -= 1
            payload_end = pos + (j if j > 0 else i)
            return pos + i, payload_end
        pos += max(scan_end - 2, 1)
    return None, None


def walk_stream(f, pos, end):
    """从pos(某个00 00 01起始码处)沿NAL链走 → 段dict或None
    注: cur始终指向3字节起始码(00 00 01)的起点, NAL头在cur+3;
        4字节码(00 00 00 01)的前导零只回退到段起点报告, 不影响遍历。"""
    win = _Win(f)
    head = win.read(pos + 3, 1)
    if not head:
        return None
    b0 = head[0]
    if b0 in H264_HEADS:
        codec, ok, vcl = 'h264', H264_OK, VCL_H264
    elif b0 in H265_HEADS:
        codec, ok, vcl = 'h265', H265_OK, VCL_H265
    else:
        return None
    seg_start = pos
    if pos > 0 and win.read(pos - 1, 1) == b'\x00':
        seg_start = pos - 1                    # 贴住4字节起始码的前导零

    nals = []           # (nal_type, (payload起,payload止), payload含NAL头)
    cur = pos
    vcl_sizes = []      # 已见VCL载荷尺寸(流尾启发式用)
    while cur < end:
        hdr_len = 2 if codec == 'h265' else 1
        nb = win.read(cur + 3, hdr_len)
        if len(nb) < hdr_len or nb[0] & 0x80:
            break
        if codec == 'h264':
            ntype = nb[0] & 0x1F
            if ntype not in ok or (ntype in (5, 7, 8) and not (nb[0] >> 5) & 3):
                break
        else:
            ntype = (nb[0] >> 1) & 0x3F
            if ntype not in ok:
                break
        sc_next, pl_end = _next_sc(win, cur + 3 + hdr_len, cur + 3 + MAX_LOOKAHEAD)
        if sc_next is not None:
            payload_end = pl_end
        else:
            # 流尾(前方无起始码): 物理终点不可知, 按码率均匀性封顶(CCTV典型特征)
            if ntype in vcl and vcl_sizes:
                vcl_sizes.sort()
                med = vcl_sizes[len(vcl_sizes) // 2]
                cap = max(4096, min(med * 2, 1024 * 1024))
            else:
                cap = 65536
            payload_end = min(cur + 3 + cap, cur + 3 + MAX_NAL, end)
        plen = payload_end - (cur + 3)
        if plen <= hdr_len or plen > MAX_NAL:
            break
        body = win.read(cur + 3, plen)
        if len(body) < plen:
            break
        # 防竞争转义保证: 合法NAL载荷内不可能出现00 00 00。
        # 三连零=已越过流边界(进入零填充/残留区) → 本NAL在此截断, 链终止。
        z = body.find(b'\x00\x00\x00')
        chain_end = False
        if z >= 0:
            body = body[:z]
            payload_end = cur + 3 + z
            chain_end = True
            if len(body) <= hdr_len:
                break
        if ntype in vcl and nals and payload_end - seg_start > MAX_SEG:
            break                                   # 段过大: 在上一NAL收尾
        nals.append((ntype, (cur + 3, payload_end), body))
        if ntype in vcl:
            vcl_sizes.append(len(body))
        if sc_next is None or chain_end:
            break
        cur = sc_next
    if not nals:
        return None
    last_end = nals[-1][1][1]
    seg_len = last_end - seg_start
    vcl_n = sum(1 for t, _, _ in nals if t in vcl)
    vcl_bytes = sum(len(b) for t, _, b in nals if t in vcl)
    if vcl_n < 1 or len(nals) < 4 or vcl_bytes < seg_len * 0.4:
        return None
    sps = pps = vps = None
    for t, _, body in nals:
        if codec == 'h264':
            if t == 7 and sps is None:
                sps = body[1:]
            elif t == 8 and pps is None:
                pps = body[1:]
        else:
            if t == 33 and sps is None:
                sps = body[2:]
            elif t == 34 and pps is None:
                pps = body[2:]
            elif t == 32 and vps is None:
                vps = body[2:]
    return {'start': seg_start, 'end': last_end, 'codec': codec, 'nals': nals,
            'vcl_frames': vcl_n, 'sps': sps, 'pps': pps, 'vps': vps,
            'size': seg_len}


# ---------- 主扫描 ----------

def scan(source, start=0, end=None, min_bytes=65536, on_progress=None, cancel=None):
    """扫描镜像范围 → 逐段yield合法视频段dict"""
    import os
    if end is None:
        end = os.path.getsize(source)
    with open(source, 'rb') as f:
        consumed_to = start
        pos = start
        found = 0
        while pos < end - 4:
            if cancel is not None and cancel.is_set():
                return
            try:
                f.seek(pos)
                buf = f.read(CHUNK)
            except OSError:
                pos += CHUNK
                continue
            if not buf:
                break
            n = len(buf)
            i = 0
            while i < n - 4:
                if cancel is not None and cancel.is_set():
                    return
                i = buf.find(SC, i)
                if i < 0 or i >= n - 4:
                    break
                cand = pos + i
                b0 = buf[i + 3]
                if cand < consumed_to or not (b0 in H264_HEADS or b0 in H265_HEADS):
                    i += 1
                    continue
                seg = walk_stream(f, cand, end)
                if seg and seg['size'] >= min_bytes:
                    _annotate(seg)
                    found += 1
                    yield seg
                    consumed_to = max(consumed_to, seg['end'])
                else:
                    consumed_to = max(consumed_to, cand + 4)
                jump = min(max(consumed_to - pos, i + 1), n)
                i = jump
            nxt = pos + max(n - 2, 1)
            if nxt <= pos:
                break
            pos = nxt
            if on_progress and pos % (64 * 1024 * 1024) < CHUNK:
                on_progress({'event': 'scan_progress',
                             'bytes_scanned': min(pos, end), 'bytes_total': end,
                             'found': found})


def _annotate(seg):
    seg['width'] = seg['height'] = seg['fps'] = seg['profile'] = None
    if seg['sps'] and seg['codec'] == 'h264':
        info = parse_sps_h264(seg['sps'])
        if info:
            seg.update(width=info['width'], height=info['height'],
                       fps=info['fps'], profile=info['profile'])


# ---------- MP4重封装(纯Python, Annex B → AVCC) ----------

def _box(fourcc, payload):
    if isinstance(fourcc, str):
        fourcc = fourcc.encode('latin1')
    return struct.pack('>I', 8 + len(payload)) + fourcc + payload


def _full(fourcc, ver, flags, payload):
    return _box(fourcc, bytes([ver]) + flags.to_bytes(3, 'big') + payload)


_MATRIX = struct.pack('>9i', 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)


def _access_units(nals, codec):
    """NAL列表 → 访问单元(帧)分组: 新VCL且当前帧已有VCL时切帧"""
    vcl = VCL_H264 if codec == 'h264' else VCL_H265
    aus, cur, has_vcl = [], [], False
    for t, _, body in nals:
        if t in vcl:
            if has_vcl:
                aus.append(cur)
                cur = []
            cur.append((t, body))
            has_vcl = True
        elif cur or aus:
            cur.append((t, body))                  # 流头孤立参数集丢弃(avcC里已有)
    if cur:
        aus.append(cur)
    return aus


def _avcc(sps, pps):
    return (b'\x01' + sps[1:4] + b'\xFF\xE1' +
            struct.pack('>H', len(sps)) + sps + b'\x01' +
            struct.pack('>H', len(pps)) + pps)


def _hvcc(vps, sps, pps):
    """HEVC hvcC: 通用字段最小化, 参数集放NAL arrays(且内联保留双保险)"""
    head = (b'\x01' +                # configurationVersion
            b'\x00' * 12 +           # profile_space/tier/idc + compat + constraint + level
            b'\xF0\x00' +            # reserved(4)+min_spatial_seg(12)
            b'\xFC' +                # reserved(6)+parallelismType(2)=0
            b'\xFD' +                # reserved(6)+chromaFormat(2)=1(4:2:0)
            b'\xF8\xF8' +            # reserved(3)+bitDepthLuma-8(5)=8bit ×2
            b'\x00\x00' +            # avgFrameRate(未知)
            b'\x0F')                 # constFR(6)=0 numTempLayers(3)=1 nested(1)=1 lenSize-1(2)=3
    # array头: array_completeness(1)+reserved(6)+NAL_unit_type(6) → 0x80|type
    arrays = b''
    count = 0
    for tid, nal in ((32, vps), (33, sps), (34, pps)):
        if nal:
            arrays += bytes([0x80 | tid]) + b'\x01' + \
                struct.pack('>H', len(nal)) + nal
            count += 1
    return _box('hvcC', head + bytes([count]) + arrays)


def remux_mp4(seg, default_fps=25.0):
    """段 → MP4字节串; 需SPS+PPS否则None。时长按fps估, B帧不重排(如实标注)"""
    if not (seg.get('sps') and seg.get('pps')):
        return None
    codec = seg['codec']
    aus = _access_units(seg['nals'], codec)
    if not aus:
        return None
    fps = seg.get('fps') or default_fps
    timescale, delta = 90000, max(1, round(90000 / fps))
    samples = [b''.join(struct.pack('>I', len(b)) + b for _, b in au) for au in aus]
    frames = len(samples)
    dur = frames * delta
    width = seg.get('width') or 0
    height = seg.get('height') or 0

    # ---- 布局: ftyp | mdat | moov (moov在尾, 本地文件播放无碍) ----
    fourcc_brand = b'avc1' if codec == 'h264' else b'hvc1'
    ftyp = _box('ftyp', b'isom' + struct.pack('>I', 512) + b'isomiso2' + fourcc_brand)
    mdat_payload = b''.join(samples)
    mdat_data_off = len(ftyp) + 8                   # mdat头8字节后即样本数据

    conf = _box('avcC', _avcc(seg['sps'], seg['pps'])) if codec == 'h264' \
        else _hvcc(seg.get('vps'), seg['sps'], seg['pps'])
    entry = 'avc1' if codec == 'h264' else 'hvc1'
    # VisualSampleEntry
    entry_payload = (b'\x00' * 6 + b'\x00\x01' +    # reserved + data_reference_index=1
                     b'\x00' * 16 +                 # pre_defined/reserved
                     struct.pack('>HH', width, height) +
                     struct.pack('>II', 0x00480000, 0x00480000) +   # 72dpi
                     b'\x00' * 4 +                  # reserved
                     struct.pack('>H', 1) +         # frame_count
                     b'\x00' * 32 +                 # compressorname(空)
                     struct.pack('>h', 24) +        # depth
                     struct.pack('>h', -1))         # pre_defined = -1
    stsd = _full('stsd', 0, 0,
                 struct.pack('>I', 1) + _box(entry, entry_payload + conf))
    stts = _full('stts', 0, 0,
                 struct.pack('>I', 1) + struct.pack('>II', frames, delta))
    stsc = _full('stsc', 0, 0,
                 struct.pack('>I', 1) + struct.pack('>III', 1, frames, 1))
    stsz = _full('stsz', 0, 0, struct.pack('>II', 0, frames) +
                 b''.join(struct.pack('>I', len(s)) for s in samples))
    stco = _full('stco', 0, 0,
                 struct.pack('>I', 1) + struct.pack('>I', mdat_data_off))
    stbl = _box('stbl', stsd + stts + stsc + stsz + stco)
    vmhd = _full('vmhd', 0, 1, b'\x00' * 8)
    dinf = _box('dinf', _full('dref', 0, 0,
                              struct.pack('>I', 1) + _box('url ', b'\x00\x00\x00\x01')))
    minf = _box('minf', vmhd + dinf + stbl)
    mdhd = _full('mdhd', 0, 0,
                 struct.pack('>IIII', 0, 0, timescale, dur) + b'\x55\xc4\x00\x00')
    hdlr = _full('hdlr', 0, 0, b'\x00' * 4 + b'vide' + b'\x00' * 12 + b'video\x00')
    mdia = _box('mdia', mdhd + hdlr + minf)
    tkhd = _full('tkhd', 0, 7,
                 struct.pack('>IIIII', 0, 0, 1, 0, dur) +   # c/m/id/res/duration
                 b'\x00' * 8 +                    # reserved
                 struct.pack('>hhhh', 0, 0, 0, 0) +  # layer/alt/volume/res
                 _MATRIX +
                 struct.pack('>II', width << 16, height << 16))
    trak = _box('trak', tkhd + mdia)
    mvhd = _full('mvhd', 0, 0,
                 struct.pack('>IIII', 0, 0, 1000, round(dur * 1000 / timescale)) +
                 struct.pack('>IH', 0x00010000, 0x0100) +    # rate / volume
                 b'\x00' * 10 +                              # reserved
                 _MATRIX + b'\x00' * 24 +                    # pre_defined
                 struct.pack('>I', 2))                       # next_track_ID
    moov = _box('moov', mvhd + trak)
    mdat = struct.pack('>I', 8 + len(mdat_payload)) + b'mdat' + mdat_payload
    return ftyp + mdat + moov


def seg_names(seg):
    """段 → (裸流文件名, mp4文件名)"""
    dim = ('%dx%d' % (seg['width'], seg['height'])) if seg.get('width') else 'x'
    base = '%s_%s_%012d' % (seg['codec'], dim, seg['start'])
    return (base + ('.h264' if seg['codec'] == 'h264' else '.h265'),
            base + '_remux.mp4')


def read_segment(f, seg):
    """从源读出段的原始字节(裸流原件, 与磁盘逐字节一致)"""
    f.seek(seg['start'])
    return f.read(seg['size'])
