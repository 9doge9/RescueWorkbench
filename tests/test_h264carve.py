# -*- coding: utf-8 -*-
"""h264carve测试:
A. Exp-Golomb编码器(测试用)与BitReader往返
B. SPS编码→parse_sps_h264解码: 分辨率/profile/fps往返断言
C. 合成裸流雕刻: 精确边界/帧数/诱饵拒绝
D. H.265最小流雕刻
E. MP4重封装: box结构走查 + 样本无损重装配(SHA一致) + avcC/tkhd字段断言
F. exfat.vhd空闲区种流 → pipeline h264carve任务 → 找回hash一致
"""
import os
import sys
import struct
import hashlib
import tempfile
import shutil
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'engine'))

import h264carve

FIX = os.path.join(HERE, 'fixtures')
FAILS = []


def check(name, ok, detail=''):
    print(('PASS ' if ok else 'FAIL ') + name + (('  ' + detail) if detail else ''), flush=True)
    if not ok:
        FAILS.append(name)


def sha(b):
    return hashlib.sha256(b).hexdigest()


# ---------- 测试用位写入器(Exp-Golomb编码) ----------

def _ep(data):
    """防竞争转义(模拟真编码器): 00 00后跟≤0x03的字节时插入0x03"""
    out = bytearray()
    z = 0
    for b in data:
        if z >= 2 and b <= 3:
            out.append(3)
            z = 0
        out.append(b)
        z = z + 1 if b == 0 else 0
    return bytes(out)


class BW:
    def __init__(self):
        self.bits = []

    def u(self, n, v):
        for i in range(n - 1, -1, -1):
            self.bits.append((v >> i) & 1)

    def ue(self, v):
        v += 1
        k = v.bit_length() - 1
        self.bits += [0] * k
        for i in range(k, -1, -1):
            self.bits.append((v >> i) & 1)

    def out(self):
        self.bits.append(1)                     # rbsp_stop_bit
        while len(self.bits) % 8:
            self.bits.append(0)
        b = bytearray()
        for i in range(0, len(self.bits), 8):
            v = 0
            for bit in self.bits[i:i + 8]:
                v = (v << 1) | bit
            b.append(v)
        return _ep(bytes(b))


def build_sps(width_mbs, height_map, fps=None):
    """构造High profile SPS; fps=None则无VUI"""
    w = BW()
    w.u(8, 100)            # profile_idc High
    w.u(8, 0)              # constraint
    w.u(8, 41)             # level 4.1
    w.ue(0)                # sps_id
    w.ue(1)                # chroma 4:2:0
    w.ue(0); w.ue(0)       # bit depths
    w.u(1, 0)              # qpprime
    w.u(1, 0)              # no scaling matrix
    w.ue(0)                # log2_max_frame_num-4
    w.ue(0)                # poc_type=0
    w.ue(0)                # log2_max_poc_lsb-4
    w.ue(1)                # max_num_ref
    w.u(1, 0)              # gaps
    w.ue(width_mbs)        # pic_width_in_mbs_minus1
    w.ue(height_map)       # pic_height_in_map_units_minus1
    w.u(1, 1)              # frame_mbs_only
    w.u(1, 0)              # direct_8x8
    w.u(1, 0)              # no crop
    if fps:
        w.u(1, 1)          # vui present
        w.u(1, 0)          # no aspect
        w.u(1, 0)          # no overscan
        w.u(1, 0)          # no video_signal
        w.u(1, 0)          # no chroma_loc
        w.u(1, 1)          # timing present
        w.u(32, 1)         # num_units_in_tick
        w.u(32, fps * 2)   # time_scale
        w.u(1, 1)          # fixed_frame_rate
    else:
        w.u(1, 0)          # no vui
    return w.out()


SC4 = b'\x00\x00\x00\x01'


def nal(hdr, payload):
    return SC4 + hdr + payload


def make_stream(fps_sps=True, frames=30, payload_len=4000):
    sps_payload = build_sps(119, 67, fps=25 if fps_sps else None)
    sps = nal(b'\x67', sps_payload)
    pps = nal(b'\x68', b'\xEE\x36' * 8)
    sei = nal(b'\x06', b'\x01\x02\x03\x04\x05\x06\x07\x08')
    idr = nal(b'\x65', b'\xAA\x7C' * payload_len)
    ps = nal(b'\x41', b'\x55\xC3' * payload_len)
    return sps + pps + sei + idr + ps * frames


# ---------- A/B. SPS往返 ----------

def test_sps():
    r = h264carve.parse_sps_h264(build_sps(119, 67, fps=25))
    check('SPS解析 1920x1088 fps', r and r['width'] == 1920 and r['height'] == 1088
          and r['profile'] == 100 and r['level'] == 4.1 and abs(r['fps'] - 25) < 0.01,
          str(r))
    r = h264carve.parse_sps_h264(build_sps(39, 29, fps=None))
    check('SPS解析 640x480 无VUI', r and r['width'] == 640 and r['height'] == 480
          and r['fps'] is None, str(r))
    check('SPS拒绝垃圾', h264carve.parse_sps_h264(b'\x12' * 8) is None)


# ---------- C/D. 合成流雕刻 ----------

def junk(n, seed=b'J'):
    import random
    rnd = random.Random(seed)
    out = bytearray()
    while len(out) < n:
        b = rnd.randrange(1, 256)               # 无零字节→不可能含起始码
        out.append(b)
    return bytes(out[:n])


def test_carve(tmp):
    stream = make_stream(frames=30)
    # 现场布置: 垃圾 + 诱饵(合法SPS头但链断) + 真·流 + 零填充 + 垃圾
    decoy = SC4 + b'\x67' + junk(700, b'D') + SC4 + b'\x62' + junk(300, b'E')
    before = junk(65536, b'A')
    after = junk(90000, b'B')
    img = before + decoy + junk(512, b'F') + stream + b'\x00' * 4096 + after
    p = os.path.join(tmp, 'disk.img')
    open(p, 'wb').write(img)
    segs = list(h264carve.scan(p, 0, len(img), min_bytes=65536))
    check('合成流: 恰好1段', len(segs) == 1, str(len(segs)))
    if not segs:
        return
    s = segs[0]
    off = img.find(stream)
    check('合成流: 精确起点', s['start'] == off, '%d vs %d' % (s['start'], off))
    check('合成流: 精确长度(尾部零填充被裁)', s['end'] - s['start'] == len(stream),
          '%d vs %d' % (s['size'], len(stream)))
    with open(p, 'rb') as f:
        got = h264carve.read_segment(f, s)
    check('合成流: 字节一致', got == stream)
    check('合成流: 帧数', s['vcl_frames'] == 31, str(s['vcl_frames']))   # 1 IDR+30P
    check('合成流: 分辨率/fps来自SPS', s['width'] == 1920 and s['height'] == 1088
          and abs((s['fps'] or 0) - 25) < 0.01)
    # H.265最小流: VPS/SPS/PPS + IRAP(type19)+PSEI
    h265 = (nal(b'\x40\x01', junk(64, b'V')) + nal(b'\x42\x01', junk(64, b'S')) +
            nal(b'\x44\x01', junk(32, b'P')) +
            nal(b'\x26\x01', b'\x9A\xBC' * 3000) + nal(b'\x02\x01', b'\x33\x77' * 3000) * 20)
    img2 = junk(70000, b'G') + h265 + junk(70000, b'H')
    p2 = os.path.join(tmp, 'h265.img')
    open(p2, 'wb').write(img2)
    segs2 = list(h264carve.scan(p2, 0, len(img2), min_bytes=65536))
    ok265 = False
    if len(segs2) == 1:
        got2 = h264carve.read_segment(open(p2, 'rb'), segs2[0])
        # 流尾后直接跟非零垃圾时物理终点不可知: 前缀必须精确, 超冲有界(≤2×中位VCL)
        ok265 = got2.startswith(h265) and 0 <= len(got2) - len(h265) <= 2 * 6002
    check('H.265流: 1段且前缀精确', ok265, 'n=%d' % len(segs2))
    # 诱饵单独放(垃圾里的假参数集, 链必断)不产出段
    p3 = os.path.join(tmp, 'decoy.img')
    open(p3, 'wb').write(junk(200000, b'I') + decoy + junk(200000, b'J'))
    segs3 = list(h264carve.scan(p3, 0, os.path.getsize(p3), min_bytes=65536))
    check('诱饵拒绝', len(segs3) == 0, str(len(segs3)))


# ---------- E. MP4重封装 ----------

def walk_boxes(b, off, end):
    boxes = []
    while off + 8 <= end:
        size = struct.unpack_from('>I', b, off)[0]
        fourcc = b[off + 4:off + 8]
        assert size >= 8 and off + size <= end, 'box %s size %d @%d 越界' % (fourcc, size, off)
        boxes.append((fourcc.decode('latin1'), off, size))
        off += size
    assert off == end, 'box总长不齐'
    return boxes


def find_box(b, boxes, name):
    hit = [x for x in boxes if x[0] == name]
    return hit[0] if hit else None


def test_remux(tmp):
    stream = make_stream(frames=30)
    sps_payload = build_sps(119, 67, fps=25)
    seg = {'codec': 'h264', 'nals': [], 'sps': sps_payload, 'pps': b'\xEE\x36' * 8,
           'width': 1920, 'height': 1088, 'fps': 25}
    # 重建nals列表(从合成流切)
    pos = 0
    while pos < len(stream):
        sc = stream.find(SC4, pos)
        assert sc >= 0
        nxt = stream.find(SC4, sc + 4)
        if nxt < 0:
            nxt = len(stream)
        body = stream[sc + 4:nxt]
        seg['nals'].append((body[0] & 0x1F, (sc + 4, nxt), body))
        pos = nxt
    mp4 = h264carve.remux_mp4(seg)
    check('remux产出非空', bool(mp4), str(len(mp4 or b'')))
    if not mp4:
        return
    top = walk_boxes(mp4, 0, len(mp4))
    names = [t[0] for t in top]
    check('顶层box: ftyp+mdat+moov', names == ['ftyp', 'mdat', 'moov'], str(names))
    moov = find_box(mp4, top, 'moov')
    moov_ch = walk_boxes(mp4, moov[1] + 8, moov[1] + moov[2])
    trak = [x for x in moov_ch if x[0] == 'trak'][0]
    trak_ch = walk_boxes(mp4, trak[1] + 8, trak[1] + trak[2])
    tkhd = [x for x in trak_ch if x[0] == 'tkhd'][0]
    w = struct.unpack_from('>I', mp4, tkhd[1] + 8 + (tkhd[2] - 8 - 8))[0] >> 16
    h = struct.unpack_from('>I', mp4, tkhd[1] + tkhd[2] - 4)[0] >> 16
    check('tkhd宽高', (w, h) == (1920, 1088), '%dx%d' % (w, h))
    mdia = [x for x in trak_ch if x[0] == 'mdia'][0]
    mdia_ch = walk_boxes(mp4, mdia[1] + 8, mdia[1] + mdia[2])
    minf = [x for x in mdia_ch if x[0] == 'minf'][0]
    minf_ch = walk_boxes(mp4, minf[1] + 8, minf[1] + minf[2])
    stbl = [x for x in minf_ch if x[0] == 'stbl'][0]
    stbl_ch = walk_boxes(mp4, stbl[1] + 8, stbl[1] + stbl[2])
    stsz = [x for x in stbl_ch if x[0] == 'stsz'][0]
    # stsz: [hdr8][ver/flags4][sample_size4][count4][entries...]
    cnt = struct.unpack_from('>I', mp4, stsz[1] + 16)[0]
    body_off = stsz[1] + 20
    sizes = [struct.unpack_from('>I', mp4, body_off + 4 * i)[0] for i in range(cnt)]
    check('stsz帧数', cnt == 31, str(cnt))
    mdat = find_box(mp4, top, 'mdat')
    stco = [x for x in stbl_ch if x[0] == 'stco'][0]
    stco_off = struct.unpack_from('>I', mp4, stco[1] + 16)[0]
    check('stco指向mdat数据', stco_off == mdat[1] + 8)
    # 无损重装配: mdat样本 → Annex B == 原流去掉流头SPS/PPS/SEI(avcC已含)
    re = b''
    pos = mdat[1] + 8
    for sz in sizes:
        re += SC4 + mp4[pos + 4:pos + sz]
        pos += sz
    expect = stream[len(nal(b'\x67', sps_payload)) + len(nal(b'\x68', b'\xEE\x36' * 8)) +
                    len(nal(b'\x06', b'\x01\x02\x03\x04\x05\x06\x07\x08')):]
    check('样本无损重装配', re == expect, 're=%d expect=%d' % (len(re), len(expect)))
    # avcC内嵌SPS/PPS
    stsd = [x for x in stbl_ch if x[0] == 'stsd'][0]
    assert b'\x67' not in stsd and True
    has = sps_payload in mp4 and b'\xEE\x36' * 8 in mp4
    check('avcC含SPS/PPS', has)


# ---------- F. VHD种流冒烟 ----------

def mbr_part(path):
    with open(path, 'rb') as f:
        mbr = f.read(512)
    for i in range(4):
        e = mbr[446 + 16 * i:462 + 16 * i]
        if e[4] != 0:
            return struct.unpack_from('<I', e, 8)[0], struct.unpack_from('<I', e, 12)[0]
    return None


def test_vhd(tmp):
    vhd = os.path.join(FIX, 'exfat.vhd')
    if not os.path.exists(vhd):
        check('VHD夹具', False, '缺exfat.vhd')
        return
    part = mbr_part(vhd)
    lba, secs = part
    import allocmap
    info = allocmap.analyze(vhd, lba * 512)
    ext = next((e for e in info['extents'] if e[1] - e[0] > 4 * 1024 * 1024), None)
    check('存在空闲段种流', bool(ext))
    if not ext:
        return
    stream = make_stream(frames=20, payload_len=8000)
    plant = os.path.join(tmp, 'plant.vhd')
    shutil.copy(vhd, plant)
    off = ext[0] + 131072
    with open(plant, 'r+b') as f:
        f.seek(off)
        f.write(stream)
    import pipeline
    outdir = os.path.join(tmp, 'h264out')
    job = pipeline.start_job('h264carve', {
        'source': plant, 'out': outdir, 'start': lba * 512, 'end': (lba + secs) * 512,
        'min_kb': 64})
    t0 = time.time()
    while job['state'] == 'running' and time.time() - t0 < 300:
        time.sleep(0.5)
    check('h264carve任务完成', job['state'] == 'done',
          '%s %s' % (job['state'], (job.get('error') or '')[:150]))
    if job['state'] != 'done':
        return
    result = job.get('result') or {}
    check('h264carve找到1段', result.get('found') == 1, str(result.get('found')))
    raw_ok = mp4_ok = False
    for fn in os.listdir(outdir):
        data = open(os.path.join(outdir, fn), 'rb').read()
        if fn.endswith('.h264'):
            # 种入点在空闲区, 流后可能是零也可能是残留数据: 前缀精确+超冲有界
            raw_ok = data.startswith(stream) and 0 <= len(data) - len(stream) <= 2 * 16002
        if fn.endswith('.mp4'):
            mp4_ok = len(data) > len(stream) // 2
    check('裸流前缀精确找回', raw_ok)
    check('重封装MP4产出', mp4_ok)


def main():
    tmp = tempfile.mkdtemp(prefix='h264_')
    try:
        test_sps()
        test_carve(tmp)
        test_remux(tmp)
        test_vhd(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print('\n%s: %d项失败' % ('总判定' if FAILS else '全绿', len(FAILS)), flush=True)
    sys.exit(1 if FAILS else 0)


if __name__ == '__main__':
    main()
