# -*- coding: utf-8 -*-
"""海康(.dav)PS流剥离 — 监控录像转裸H.264/H.265 + MP4重封装
场景: 海康DVR/NVR导出的 .dav(部分型号 .mpg) = MPEG-PS 包装:
      pack头(00 00 01 BA) / 系统头(BB) / PSM(BC) / PES包(E0=视频, C0-C1=音频, BD=海康私有索引)。
      视频PES载荷连起来就是标准Annex-B裸流 → 直接喂给h264carve走查/重封装。
诚实边界: PS是通用容器, 本模块面向海康式布局(BA+BB+PES循环), 花活变体(未包PES的裸TS)不认;
          文件尾的海康索引块不是PS, 走查到失配即自然停止。
"""
import os
import struct

SC = b'\x00\x00\x01'
# PES流id分类
VIDEO_IDS = set(range(0xE0, 0xF0))
AUDIO_IDS = set(range(0xC0, 0xE0))
LENGTH_CODED = {0xBB, 0xBC, 0xBD, 0xBE, 0xBF}      # 系统头/PSM/私有流/填充: 都带u16长度


def is_dav(path):
    """PS pack头(00 00 01 BA)在前4KB内 → 认作海康式PS录像"""
    try:
        if os.path.getsize(path) < 1024:
            return False
        with open(path, 'rb') as f:
            head = f.read(4096)
        return head.find(SC + b'\xBA') >= 0
    except OSError:
        return False


def _next_pes_pos(f, pos, size):
    """从pos起找下一个PS级边界(00 00 01 + 流码)的绝对位置。
    判据: SC后字节∈{BA,BB..BF}∪[C0,EF]才是PS起始码——这些值≥0xB?且最高位为1
    (0xBA..0xBF首字节bit7=1, 0xC0..0xEF两位为1), 而合法H.264/H.265 NAL头最高位
    是禁用位恒0(≤0x7F), 二者不冲突 → 载荷内的NAL起始码不会被误认。"""
    step = 4 << 20
    while pos < size:
        f.seek(pos)
        buf = f.read(step + 4)
        if not buf:
            break
        i = 0
        while True:
            i = buf.find(SC, i)
            if i < 0:
                break
            code = buf[i + 3] if i + 3 < len(buf) else 0x100
            if code == 0xBA or code in LENGTH_CODED or 0xC0 <= code <= 0xEF:
                return pos + i
            i += 1
        pos += step
    return None


def extract_video(src, out_raw, on_progress=None, cancel=None):
    """剥离PS → 视频PES载荷连写out_raw → {'packs','video_pes','audio_pes','bytes_out'}
    容错: 任何失配处重新搜起始码(垃圾/索引块自然跳过)。"""
    size = os.path.getsize(src)
    stats = {'packs': 0, 'video_pes': 0, 'audio_pes': 0, 'bytes_out': 0}
    with open(src, 'rb') as f, open(out_raw, 'wb') as w:
        pos = 0
        next_report = 0
        while pos < size:
            if cancel is not None and cancel.is_set():
                stats['cancelled'] = True
                return stats
            if pos >= next_report:
                next_report = pos + (8 << 20)
                if on_progress:
                    on_progress({'event': 'progress', 'bytes_done': pos,
                                 'bytes_total': size, 'pes': stats['video_pes']})
            f.seek(pos)
            buf = f.read(64)
            if len(buf) < 4 or buf[:3] != SC:
                nxt = _next_pes_pos(f, pos + 1, size)
                if nxt is None:
                    break
                pos = nxt
                continue
            code = buf[3]
            if code == 0xBA:                       # pack头: 14B + 填充字节
                if len(buf) < 14:
                    break
                stuffing = buf[13] & 0x07
                stats['packs'] += 1
                pos += 14 + stuffing
            elif code in LENGTH_CODED:              # 带 u16 长度的段
                if len(buf) < 6:
                    break
                ln = struct.unpack_from('<H', buf, 4)[0]
                pos += 6 + ln
            elif code in VIDEO_IDS or code in AUDIO_IDS:
                if len(buf) < 9:
                    break
                plen = struct.unpack_from('<H', buf, 4)[0]
                hdl = buf[8]
                pstart = pos + 9 + hdl
                if plen >= 3 + hdl:
                    pend = pstart + (plen - 3 - hdl)
                else:
                    # 未定长PES(视频偶见): 载荷到下一个起始码为止
                    nxt = _next_pes_pos(f, pstart, size)
                    if nxt is None:
                        nxt = size
                    pend = min(nxt, size)
                if pend > pstart:
                    if code in VIDEO_IDS:
                        f.seek(pstart)
                        left = pend - pstart
                        while left > 0:
                            chunk = f.read(min(1 << 20, left))
                            if not chunk:
                                break
                            w.write(chunk)
                            left -= len(chunk)
                        stats['video_pes'] += 1
                        stats['bytes_out'] += (pend - pstart) - left
                    else:
                        stats['audio_pes'] += 1
                pos = max(pend, pos + 9)
            else:                                   # 陌生码: 跳过这枚起始码再搜
                pos += 4
        if on_progress:
            on_progress({'event': 'progress', 'bytes_done': size, 'bytes_total': size,
                         'pes': stats['video_pes']})
    return stats


def remux(raw_path, out_mp4):
    """对剥离出的裸流走查+重封装 → (seg信息dict, mp4字节) 或 (None, None)
    走查/SPS解析/MP4封装全部复用 h264carve(同一套校验, 不另起炉灶)。"""
    import h264carve
    size = os.path.getsize(raw_path)
    with open(raw_path, 'rb') as f:
        head = f.read(65536)
        i = head.find(SC)
        if i < 0:
            return None, None
        seg = h264carve.walk_stream(f, i, size)     # find命中的已是内层3字节SC
        if not seg:
            return None, None
        h264carve._annotate(seg)
        try:
            mp4 = h264carve.remux_mp4(seg)
        except Exception:
            mp4 = None
        if mp4:
            with open(out_mp4, 'wb') as w:
                w.write(mp4)
        info = {'codec': seg['codec'], 'vcl_frames': seg['vcl_frames'],
                'size': seg['size'], 'mp4': bool(mp4)}
        info.update({k: seg.get(k) for k in ('width', 'height', 'fps')})
        return info, mp4
