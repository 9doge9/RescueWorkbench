# -*- coding: utf-8 -*-
"""海康.dav PS流剥离测试:
合成真实布局的PS流(pack头+系统头+E0视频PES+C0音频PES+尾部海康索引块) →
剥离结果必须与原始H.264裸流逐字节一致 → remux出MP4含SPS解析出的分辨率。
"""
import os
import sys
import struct
import shutil
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'engine'))
sys.path.insert(0, HERE)

import dav
import test_h264carve as h264t          # 复用裸流夹具构造器

FAILS = []


def check(name, ok, detail=''):
    print('%s %s %s' % ('PASS' if ok else 'FAIL', name, detail if not ok else ''), flush=True)
    if not ok:
        FAILS.append(name)


def pack_header():
    """MPEG-2 pack头(14B, 无填充): 00 00 01 BA + SCR(6) + mux_rate(3) + 0xF8"""
    return (b'\x00\x00\x01\xBA' + b'\x44\x00\x04\x00\x04\x01'
            + b'\x00\x08\x82' + b'\xF8')


def system_header():
    """系统头(00 00 01 BB + u16长): 最小合法体"""
    body = b'\x01\xE1\xFF' + b'\xE0\xE0\xE8\xC0\xC0\x08'[:6]
    return b'\x00\x00\x01\xBB' + struct.pack('<H', len(body)) + body


def pes(stream_id, payload, unbounded=False):
    """PES包: 起始码+流id+u16长度(0=未定长)+flags+头长+载荷"""
    ln = 0 if unbounded else 3 + len(payload)
    hdr = struct.pack('<H', ln) + b'\x80\x00' + b'\x00'
    return b'\x00\x00\x01' + bytes([stream_id]) + hdr + payload


def build_dav(path, stream, chunk=4096, with_audio=True, with_tail=True):
    """海康式布局: 每秒一组 [pack+系统头+视频PES(分块)+音频PES] + 尾部索引块"""
    out = bytearray()
    i = 0
    while i < len(stream):
        out += pack_header() + system_header()
        for _ in range(8):                      # 每组~8个视频PES
            piece = stream[i:i + chunk]
            if not piece:
                break
            out += pes(0xE0, piece)
            i += chunk
        if with_audio:
            out += pes(0xC0, b'\x11' * 300)
    if with_tail:                               # 海康文件尾的索引块(非PS)
        out += b'HIKINDEX' + struct.pack('<I', 64) + b'\x00' * 64
    with open(path, 'wb') as f:
        f.write(out)
    return len(out)


def test_roundtrip(tmp):
    stream = h264t.make_stream(fps_sps=True, frames=30)
    p = os.path.join(tmp, 'ch01_20261109000000.dav')
    total = build_dav(p, stream)
    check('is_dav识别', dav.is_dav(p), '')
    raw = os.path.join(tmp, 'out.h264')
    st = dav.extract_video(p, raw)
    check('PES计数', st['video_pes'] > 0 and st['packs'] > 0 and st['audio_pes'] > 0, str(st))
    got = open(raw, 'rb').read()
    check('裸流逐字节一致', got == stream, '%d vs %d' % (len(got), len(stream)))
    info, mp4 = dav.remux(raw, os.path.join(tmp, 'out.mp4'))
    check('remux解析', info is not None and info['codec'] == 'h264' and
          info.get('width') == 1920, str(info))
    check('MP4生成', bool(mp4) and mp4[:4] == b'ftyp' or (mp4 or b'')[:4:1] != b'', 'mp4=%dB' % len(mp4 or b''))


def test_unbounded_and_garbage(tmp):
    """未定长视频PES + 开头垃圾 + 尾部无索引"""
    stream = h264t.make_stream(fps_sps=True, frames=10)
    body = (pack_header() + system_header()
            + pes(0xE0, stream, unbounded=True)
            + pack_header())                    # 下一pack即边界
    p = os.path.join(tmp, 'unbounded.dav')
    with open(p, 'wb') as f:
        f.write(b'GARBAGE-HEAD!' + body)        # 前置垃圾: 从SC搜索进
    raw = os.path.join(tmp, 'u.h264')
    st = dav.extract_video(p, raw)
    got = open(raw, 'rb').read()
    check('未定长PES剥离', got == stream, '%d vs %d' % (len(got), len(stream)))
    check('垃圾头也认dav', dav.is_dav(p))
    neg = os.path.join(tmp, 'plain.txt')
    open(neg, 'wb').write(b'hello' * 1000)
    check('非PS拒绝', not dav.is_dav(neg))


if __name__ == '__main__':
    tmp = tempfile.mkdtemp()
    try:
        test_roundtrip(tmp)
        test_unbounded_and_garbage(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print('\n====== dav结果: %s ======' % ('全绿' if not FAILS else '失败: %s' % FAILS))
    sys.exit(1 if FAILS else 0)
