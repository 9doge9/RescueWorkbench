# -*- coding: utf-8 -*-
"""按文件内部元数据重命名: 照片EXIF时间 / MP3 ID3 / Office内部标题
   新名 = 有意义名 + _原偏移名(唯一性)。输出 rename_map.csv"""
import os, sys, csv, struct, zipfile, re

BAD = r'\/:*?"<>|' + ''.join(chr(c) for c in range(32))

def clean(s, maxlen=70):
    s = ''.join('_' if c in BAD else c for c in s).strip(' .')
    return s[:maxlen] if s else ''

def jpg_exif_dt(path):
    try:
        with open(path, 'rb') as f:
            head = f.read(512*1024)
        pos = 2
        while pos + 4 < len(head):
            if head[pos] != 0xff: return None
            tag = head[pos+1]
            if tag == 0xd9 or tag == 0xda: return None
            if tag in (0x01, 0xd8) or 0xd0 <= tag <= 0xd7: pos += 2; continue
            ln = struct.unpack('>H', head[pos+2:pos+4])[0]
            if tag == 0xe1 and head[pos+4:pos+10] == b'Exif\x00\x00':
                t = head[pos+10:pos+10+8]
                if t[:2] == b'II': en = '<'; off0 = struct.unpack('<I', t[4:8])[0]
                elif t[:2] == b'MM': en = '>'; off0 = struct.unpack('>I', t[4:8])[0]
                else: return None
                base = pos + 10
                # IFD0
                n = struct.unpack(en+'H', head[base+off0:base+off0+2])[0]
                exif_ifd_off = None
                for i in range(n):
                    e = base + off0 + 2 + i*12
                    tg = struct.unpack(en+'H', head[e:e+2])[0]
                    if tg == 0x8769:
                        exif_ifd_off = struct.unpack(en+'I', head[e+8:e+12])[0]
                        break
                if exif_ifd_off is None: return None
                e = base + exif_ifd_off
                n2 = struct.unpack(en+'H', head[e:e+2])[0]
                for i in range(n2):
                    ee = e + 2 + i*12
                    tg = struct.unpack(en+'H', head[ee:ee+2])[0]
                    if tg in (0x9003, 0x0132):  # 拍摄时间/修改时间
                        cnt = struct.unpack(en+'I', head[ee+4:ee+8])[0]
                        vo = struct.unpack(en+'I', head[ee+8:ee+12])[0]
                        if cnt*4 > 4:
                            p = base + exif_ifd_off + vo
                            raw = head[p:p+20]
                        else:
                            raw = head[ee+8:ee+8+20]
                        try:
                            dt = raw.split(b'\x00')[0].decode('ascii')
                            return dt.replace(':', '').replace(' ', '_')
                        except Exception:
                            return None
                return None
            pos += 2 + ln
    except Exception:
        return None
    return None

def mp3_id3(path):
    try:
        sz = os.path.getsize(path)
        with open(path, 'rb') as f:
            head = f.read(min(sz, 1024*1024))
        title = artist = None
        if head[:3] == b'ID3':
            ver = head[3]
            size = (head[6]&0x7f)<<21 | (head[7]&0x7f)<<14 | (head[8]&0x7f)<<7 | (head[9]&0x7f)
            body = head[10:10+size]
            if ver >= 3:
                i = 0
                while i + 10 < len(body):
                    fid = body[i:i+4]
                    if not all(65 <= c <= 90 for c in fid): break
                    fsz = struct.unpack('>I', body[i+4:i+8])[0]
                    enc = body[i+10]
                    data = body[i+10+1:i+10+fsz]
                    def dec(d, e):
                        try:
                            if e in (0, 3): return d.split(b'\x00')[0].decode('utf-8' if e==3 else 'gbk', 'ignore')
                            if e == 1: return d.decode('utf-16-le', 'ignore').split('\x00')[0]
                            if e == 2: return d.decode('utf-16-be', 'ignore').split('\x00')[0]
                        except Exception: return ''
                        return ''
                    if fid == b'TIT2': title = dec(data, enc)
                    if fid == b'TPE1': artist = dec(data, enc)
                    i += 10 + fsz
        if title:
            return clean((artist + ' - ' if artist else '') + title)
    except Exception:
        pass
    return None

def ooxml_title(path):
    try:
        z = zipfile.ZipFile(path)
        for n in ('docProps/core.xml', 'docProps/app.xml'):
            try:
                x = z.read(n).decode('utf-8', 'ignore')
                m = re.search(r'<dc:title>(.*?)</dc:title>', x)
                if m and m.group(1).strip():
                    return clean(m.group(1))
                m = re.search(r'<Title>(.*?)</Title>', x)
                if m and m.group(1).strip():
                    return clean(m.group(1))
            except KeyError:
                continue
    except Exception:
        pass
    return None

def mp4_title(path):
    try:
        with open(path, 'rb') as f:
            head = f.read(4*1024*1024)
        # 找 udta→meta→ilst→©nam→data
        def walk(b, start, end, depth=0):
            if depth > 6: return None
            pos = start
            while pos + 8 <= end:
                sz = struct.unpack('>I', b[pos:pos+4])[0]
                typ = b[pos+4:pos+8]
                if sz < 8 or pos + sz > end: return None
                if typ in (b'moov', b'udta', b'ilst', b'\xa9nam', b'meta'):
                    inner = pos + 8
                    if typ == b'meta': inner += 4  # version/flags
                    if typ == b'\xa9nam':
                        # 里面找 data atom
                        p2 = inner
                        while p2 + 16 <= pos + sz:
                            dsz = struct.unpack('>I', b[p2:p2+4])[0]
                            if b[p2+4:p2+8] == b'data' and dsz >= 16:
                                return b[p2+16:p2+dsz].split(b'\x00')[0].decode('utf-8', 'ignore')
                            p2 += max(dsz, 8)
                        return None
                    r = walk(b, inner, pos + sz, depth + 1)
                    if r: return r
                pos += sz
            return None
        t = walk(head, 0, len(head))
        if t: return clean(t)
    except Exception:
        pass
    return None

def main(root):
    mapping = []
    counts = {'exif': 0, 'id3': 0, 'title': 0, 'mp4title': 0, 'keep': 0}
    for sub in os.listdir(root):
        d = os.path.join(root, sub)
        if not os.path.isdir(d): continue
        for fn in os.listdir(d):
            if fn in ('恢复清单.csv', 'rename_map.csv'): continue
            p = os.path.join(d, fn)
            if not os.path.isfile(p): continue
            ext = fn.lower().rsplit('.', 1)[-1] if '.' in fn else ''
            stem = fn.rsplit('.', 1)[0]
            new = None
            if ext == 'jpg':
                dt = jpg_exif_dt(p)
                if dt: new = 'IMG_' + dt; counts['exif'] += 1
            elif ext == 'mp3':
                t = mp3_id3(p)
                if t: new = t; counts['id3'] += 1
            elif ext in ('docx', 'pptx', 'xlsx'):
                t = ooxml_title(p)
                if t: new = t; counts['title'] += 1
            elif ext == 'mp4':
                t = mp4_title(p)
                if t: new = t; counts['mp4title'] += 1
            if new:
                newfn = new + '_' + stem + '.' + ext
                try:
                    os.rename(p, os.path.join(d, newfn))
                    mapping.append([sub, fn, newfn])
                except OSError:
                    counts['keep'] += 1
                    continue
            else:
                counts['keep'] += 1
    with open(os.path.join(root, 'rename_map.csv'), 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(['目录', '原偏移名', '新名'])
        w.writerows(mapping)
    print('重命名统计:', counts)
    print(f'共重命名 {len(mapping)} 个, 对照表: rename_map.csv')

if __name__ == '__main__':
    main(sys.argv[1])
