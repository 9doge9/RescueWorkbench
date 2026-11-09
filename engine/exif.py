# -*- coding: utf-8 -*-
"""JPEG EXIF解析 — 照片按拍摄时间归档
场景: 恢复出的照片无原名(雕刻产物), 但EXIF里还有拍摄时间/相机型号/GPS——
      用真实拍摄时间重命名+按日期归档, 比文件恢复时间有意义得多。
纯手工解析(JPEG APP1→TIFF IFD), 零依赖; 坏数据一律静默跳过不抛。
"""
import os
import struct
import shutil

# TIFF类型 → 字节宽
_T_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}

TAG_MAKE = 0x010F
TAG_MODEL = 0x0110
TAG_DATETIME = 0x0132
TAG_EXIF_IFD = 0x8769
TAG_GPS_IFD = 0x8825
TAG_DT_ORIGINAL = 0x9003
TAG_DT_DIGITIZED = 0x9004
TAG_GPS_LATREF = 0x0001
TAG_GPS_LAT = 0x0002
TAG_GPS_LONREF = 0x0003
TAG_GPS_LON = 0x0004


def parse(path):
    """JPEG/TIFF → {make,model,datetime_original,datetime,gps:(lat,lon)|None}; 无EXIF返回None"""
    try:
        with open(path, 'rb') as f:
            head = f.read(2)
            if head == b'\xff\xd8':
                tiff = _jpeg_app1_exif(f)
            elif head in (b'II', b'MM'):
                f.seek(0)
                tiff = f.read(64 * 1024 * 1024)
            else:
                return None
            if not tiff:
                return None
            return _parse_tiff(tiff)
    except OSError:
        return None


def _jpeg_app1_exif(f):
    """遍历JPEG段找APP1(Exif)"""
    pos = 2
    while pos < 32 * 1024 * 1024:                  # 32MB内找不到就算没有
        f.seek(pos)
        seg = f.read(4)
        if len(seg) < 4 or seg[0] != 0xFF:
            return None
        marker, length = seg[1], struct.unpack('>H', seg[2:4])[0]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        if marker == 0xDA:                          # SOS: 后面是图像数据, 没有更多EXIF
            return None
        if marker == 0xE1:
            payload = f.read(length - 2)
            if payload[:6] == b'Exif\x00\x00':
                return payload[6:]
            return None
        pos += 2 + length
    return None


def _parse_tiff(tiff):
    if len(tiff) < 8:
        return None
    bo = tiff[:2]
    if bo == b'II':
        end, fmt = '<', 0x4949
    elif bo == b'MM':
        end, fmt = '>', 0x4D4D
    else:
        return None
    if struct.unpack_from(end + 'H', tiff, 2)[0] != 42:
        return None
    out = {'make': None, 'model': None, 'datetime_original': None,
           'datetime': None, 'gps': None}
    ifd0 = struct.unpack_from(end + 'I', tiff, 4)[0]
    _walk_ifd(tiff, end, ifd0, out, depth=0)
    if not (out['make'] or out['model'] or out['datetime_original'] or out['gps']):
        return None
    return out


def _walk_ifd(tiff, end, off, out, depth):
    """IFD遍历(含Exif/GPS子IFD), depth防环"""
    if depth > 3 or off + 2 > len(tiff):
        return
    n = struct.unpack_from(end + 'H', tiff, off)[0]
    if n > 512:
        return
    for i in range(n):
        e = off + 2 + i * 12
        if e + 12 > len(tiff):
            return
        tag, typ, cnt = struct.unpack_from(end + 'HHI', tiff, e)
        size = _T_SIZE.get(typ, 1) * cnt
        if size <= 4:
            raw = tiff[e + 8:e + 8 + size]
        else:
            vo = struct.unpack_from(end + 'I', tiff, e + 8)[0]
            if vo + size > len(tiff):
                continue
            raw = tiff[vo:vo + size]
        if tag == TAG_MAKE and typ == 2:
            out['make'] = raw.split(b'\x00')[0].decode('ascii', 'replace').strip()
        elif tag == TAG_MODEL and typ == 2:
            out['model'] = raw.split(b'\x00')[0].decode('ascii', 'replace').strip()
        elif tag == TAG_DATETIME and typ == 2:
            out['datetime'] = _fmt_dt(raw)
        elif tag == TAG_DT_ORIGINAL and typ == 2:
            out['datetime_original'] = _fmt_dt(raw)
        elif tag == TAG_DT_DIGITIZED and typ == 2 and not out['datetime_original']:
            out['datetime_original'] = _fmt_dt(raw)
        elif tag == TAG_EXIF_IFD:
            _walk_ifd(tiff, end, struct.unpack(end + 'I', raw[:4])[0] if len(raw) >= 4
                      else 0, out, depth + 1)
        elif tag == TAG_GPS_IFD:
            _walk_gps(tiff, end, struct.unpack(end + 'I', raw[:4])[0] if len(raw) >= 4 else 0, out)
    # 下一级IFD(通常不用), 忽略


def _walk_gps(tiff, end, off, out):
    if off + 2 > len(tiff):
        return
    n = struct.unpack_from(end + 'H', tiff, off)[0]
    if n > 64:
        return
    latref = lonref = None
    lat = lon = None
    for i in range(n):
        e = off + 2 + i * 12
        if e + 12 > len(tiff):
            return
        tag, typ, cnt = struct.unpack_from(end + 'HHI', tiff, e)
        size = _T_SIZE.get(typ, 1) * cnt
        if size <= 4:
            raw = tiff[e + 8:e + 8 + size]
        else:
            vo = struct.unpack_from(end + 'I', tiff, e + 8)[0]
            if vo + size > len(tiff):
                continue
            raw = tiff[vo:vo + size]
        if tag == TAG_GPS_LATREF and typ == 2:
            latref = raw[:1].decode('ascii', 'replace')
        elif tag == TAG_GPS_LONREF and typ == 2:
            lonref = raw[:1].decode('ascii', 'replace')
        elif tag == TAG_GPS_LAT and typ == 5:
            lat = _dms(raw, end)
        elif tag == TAG_GPS_LON and typ == 5:
            lon = _dms(raw, end)
    if lat is not None and lon is not None:
        if latref == 'S':
            lat = -lat
        if lonref == 'W':
            lon = -lon
        out['gps'] = (round(lat, 6), round(lon, 6))


def _dms(raw, end):
    """度分秒(3×RATIONAL) → 十进制"""
    if len(raw) < 24:
        return None
    d = struct.unpack_from(end + 'II', raw, 0)
    m = struct.unpack_from(end + 'II', raw, 8)
    s = struct.unpack_from(end + 'II', raw, 16)
    if d[1] == 0 or m[1] == 0 or s[1] == 0:
        return None
    return d[0] / d[1] + m[0] / m[1] / 60 + s[0] / s[1] / 3600


def _fmt_dt(raw):
    s = raw.split(b'\x00')[0].decode('ascii', 'replace').strip()
    if len(s) >= 19 and s[4] == ':' and s[7] == ':':
        return s.replace(':', '-', 2)               # 2024:01:02→2024-01-02
    return None


# ---------- 目录扫描与归档 ----------

def scan_dir(directory, on_progress=None):
    """扫描目录JPEG → [{file, exif|None}]"""
    results = []
    for dirpath, _, files in os.walk(directory):
        for fn in files:
            if not fn.lower().endswith(('.jpg', '.jpeg', '.tif', '.tiff', '.heic')):
                continue
            p = os.path.join(dirpath, fn)
            results.append({'file': p, 'exif': parse(p)})
            if on_progress and len(results) % 50 == 0:
                on_progress({'event': 'progress', 'files': len(results)})
    return results


def _target_name(info):
    """EXIF时间 > 文件修改时间 > 无时间"""
    ex = info['exif'] or {}
    dt = ex.get('datetime_original') or ex.get('datetime')
    if dt:
        return dt.replace('-', '').replace(' ', '_').replace(':', '')[:15], dt[:7]
    try:
        mt = os.path.getmtime(info['file'])
        import time as _t
        t = _t.localtime(mt)
        return '%04d%02d%02d_%02d%02d%02d' % t[:6], '%04d-%02d' % (t[0], t[1])
    except OSError:
        return None, None


def organize(results, out_root, mode='copy', on_progress=None):
    """按月份归档: out/2024-03/IMG_20240302_142530_序号.jpg; copy或move"""
    done = skipped = 0
    for i, info in enumerate(results):
        if not info.get('exif'):
            skipped += 1
            continue
        base, month = _target_name(info)
        if not base:
            skipped += 1
            continue
        dest_dir = os.path.join(out_root, month)
        os.makedirs(dest_dir, exist_ok=True)
        ext = os.path.splitext(info['file'])[1].lower() or '.jpg'
        dest = os.path.join(dest_dir, ('IMG_' + base + ext))
        n = 1
        while os.path.exists(dest):
            dest = os.path.join(dest_dir, 'IMG_%s_%02d%s' % (base, n, ext))
            n += 1
        try:
            if mode == 'move':
                shutil.move(info['file'], dest)
            else:
                shutil.copy2(info['file'], dest)
            done += 1
        except OSError:
            skipped += 1
        if on_progress and i % 50 == 0:
            on_progress({'event': 'progress', 'files': i, 'total': len(results)})
    return {'organized': done, 'skipped': skipped, 'out': out_root}
