# -*- coding: utf-8 -*-
"""CHK文件识别与拆分 — chkdsk把丢失簇存成FOUND.00X/FILE0001.CHK
CHK常常是完整文件或多个文件的拼接。本模块: 魔数识别 + 内容边界定位 → 拆分提取。
识别表与边界规则(JPG/ZIP/PNG/PDF/MP4/RAR/7z/GZ可定位结尾; 其余识别不拆)。
"""
import os
import struct

# (名称, 后缀, 魔数, 结尾定位函数(整个缓冲,起点)→内容末尾或None)
def _jpg_end(buf, s):
    e = buf.find(b'\xff\xd9', s + 2)
    return e + 2 if e >= 0 else None


def _zip_end(buf, s):
    e = buf.rfind(b'PK\x05\x06')
    return e + 22 if e >= s else None


def _png_end(buf, s):
    e = buf.find(b'IEND', s)
    return e + 8 if e >= 0 else None


def _pdf_end(buf, s):
    e = buf.rfind(b'%%EOF')
    return e + 5 if e >= s else None


def _mp4_end(buf, s):
    """逐box走到头"""
    pos = s
    end = len(buf)
    while pos + 8 <= end:
        size = struct.unpack_from('>I', buf, pos)[0]
        fourcc = buf[pos + 4:pos + 8]
        if size == 1:
            size = struct.unpack_from('>Q', buf, pos + 8)[0]
        if size < 8 or pos + size > end:
            return pos if pos > s else None
        if fourcc == b'mdat' and size == 0:
            return None
        pos += size
    return pos if pos > s else None


def _rar_end(buf, s):
    return buf.rfind(b'\xc2\x71\x1c\x04') + 8 or None   # RAR5结束块(尽力)


SNIFFS = (
    ('JPEG', '.jpg', b'\xff\xd8\xff', _jpg_end),
    ('PNG', '.png', b'\x89PNG\r\n\x1a\n', _png_end),
    ('ZIP/Office', '.zip', b'PK\x03\x04', _zip_end),
    ('PDF', '.pdf', b'%PDF', _pdf_end),
    ('MP4', '.mp4', b'', None),          # 特判ftyp
    ('RAR5', '.rar', b'Rar!\x1a\x07\x01\x00', _rar_end),
    ('7z', '.7z', b'7z\xbc\xaf\x27\x1c', None),
    ('GZIP', '.gz', b'\x1f\x8b', None),
    ('SQLite', '.db', b'SQLite format 3\x00', None),
    ('GIF', '.gif', b'GIF8', None),
    ('RIFF/WAV-AVI', '.bin', b'RIFF', None),
    ('MP3', '.mp3', b'ID3', None),
    ('MP3(raw)', '.mp3', b'\xff\xfb', None),
    ('旧Office(OLE)', '.bin', b'\xd0\xcf\x11\xe0', None),
    ('ELF', '.bin', b'\x7fELF', None),
)


def _sniff(buf, s):
    for name, ext, magic, endfn in SNIFFS:
        if name == 'MP4':
            if s + 12 <= len(buf) and buf[s + 4:s + 8] == b'ftyp':
                return name, ext, _mp4_end(buf, s)
            continue
        if buf.startswith(magic, s):
            end = endfn(buf, s) if endfn else None
            return name, ext, end
    return None, None, None


def identify(path, max_size=2 * 1024 * 1024 * 1024):
    """CHK文件 → {'parts': [{offset,name,end}], 'head_type', 'coverage'}"""
    size = os.path.getsize(path)
    if size > max_size:
        return {'parts': [], 'head_type': None, 'coverage': 0,
                'error': '文件过大'}
    with open(path, 'rb') as f:
        buf = f.read()
    parts = []
    pos = 0
    while pos < len(buf):
        name, ext, end = _sniff(buf, pos)
        if name:
            if end is None or end > len(buf) or end <= pos:
                end = len(buf)                      # 边界不可靠→到文件尾
            parts.append({'offset': pos, 'end': end, 'type': name,
                          'ext': ext, 'size': end - pos})
            pos = end
        else:
            # 从下一个魔数候选跳(扫几个常见起始)
            nxt = len(buf)
            for name2, ext2, magic, _ in SNIFFS:
                if name2 == 'MP4' or not magic:
                    continue
                i = buf.find(magic, pos + 1)
                if 0 <= i < nxt:
                    nxt = i
            if nxt >= len(buf):
                break
            pos = nxt
    coverage = sum(p['size'] for p in parts)
    return {'parts': parts, 'head_type': parts[0]['type'] if parts else None,
            'coverage': coverage / max(len(buf), 1), 'size': len(buf)}


def extract(path, out_dir, info=None, on_progress=None):
    """CHK → 拆分落盘 → [输出文件列表]"""
    info = info or identify(path)
    base = os.path.splitext(os.path.basename(path))[0]
    os.makedirs(out_dir, exist_ok=True)
    with open(path, 'rb') as f:
        out = []
        for i, p in enumerate(info['parts']):
            f.seek(p['offset'])
            data = f.read(p['size'])
            dest = os.path.join(out_dir, '%s_%02d%s' % (base, i + 1, p['ext']))
            k = 1
            while os.path.exists(dest):
                dest = os.path.join(out_dir, '%s_%02d_%d%s' % (base, i + 1, k, p['ext']))
                k += 1
            with open(dest, 'wb') as w:
                w.write(data)
            out.append(dest)
            if on_progress:
                on_progress({'event': 'progress', 'extracted': i + 1,
                             'total': len(info['parts'])})
    return out


def scan_dir(directory, out_dir=None, on_progress=None):
    """目录下全部.CHK → 识别(+可选提取)"""
    results = []
    n = 0
    for dirpath, _, files in os.walk(directory):
        for fn in files:
            if not fn.lower().endswith('.chk'):
                continue
            p = os.path.join(dirpath, fn)
            info = identify(p)
            rec = {'file': p, 'head_type': info['head_type'],
                   'parts': [{'type': x['type'], 'size': x['size']}
                             for x in info['parts']],
                   'coverage': round(info['coverage'], 3)}
            if out_dir and info['parts']:
                rec['extracted'] = extract(p, out_dir, info)
            results.append(rec)
            n += 1
            if on_progress and n % 20 == 0:
                on_progress({'event': 'progress', 'files': n})
    return results
