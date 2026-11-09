# -*- coding: utf-8 -*-
"""验证并分类恢复出的文件:
- zip: zipfile.testzip()校验 + 按内容识别 docx/xlsx/pptx/jar/apk 改名
- 图片: 结构完整性(PNG chunk/JPEG marker已由carver保证), 尺寸提取
- 其他: 基本头尾复查
输出: verified_summary.csv + 重命名后的文件
"""
import os, sys, csv, zipfile, struct, hashlib, json

def classify_zip(path):
    try:
        zf = zipfile.ZipFile(path)
        names = zf.namelist()
        ct = ''
        for n in names:
            if n.endswith('[Content_Types].xml'):
                ct = zf.read(n)[:4096].decode('utf-8', 'ignore')
                break
        if 'wordprocessingml' in ct: return 'docx'
        if 'spreadsheetml' in ct: return 'xlsx'
        if 'presentationml' in ct: return 'pptx'
        if any(n.endswith('.jar') for n in names[:3]): return 'jar'
        if 'AndroidManifest.xml' in names: return 'apk'
        if any(n.lower().endswith('.odt') for n in names[:5]): return 'odt'
        return 'zip'
    except Exception:
        return None

def zip_ok(path):
    try:
        zf = zipfile.ZipFile(path)
        bad = zf.testzip()
        return bad is None
    except Exception:
        return False

def jpg_size(path):
    with open(path, 'rb') as f:
        d = f.read(256*1024)
    pos = 2
    while pos + 4 < len(d):
        if d[pos] != 0xff: return None
        tag = d[pos+1]
        if 0xc0 <= tag <= 0xcf and tag not in (0xc4, 0xc8, 0xcc):
            h, w = struct.unpack('>HH', d[pos+5:pos+9])
            return (w, h)
        if tag in (0xd8, 0xd9) or 0xd0 <= tag <= 0xd7: pos += 2; continue
        seglen = struct.unpack('>H', d[pos+2:pos+4])[0]
        pos += 2 + seglen
    return None

def png_size(path):
    with open(path, 'rb') as f:
        h = f.read(33)
    if len(h) >= 33 and h[:8] == b'\x89PNG\r\n\x1a\n':
        w, ht = struct.unpack('>II', h[16:24])
        return (w, ht)
    return None

def main(indir):
    rows = []
    summary = {}
    for fn in os.listdir(indir):
        path = os.path.join(indir, fn)
        if not os.path.isfile(path) or fn == 'report.csv': continue
        low = fn.lower()
        ext = low.rsplit('.', 1)[-1] if '.' in low else ''
        ok = True; note = ''; dims = ''
        newext = ext
        if ext == 'zip':
            if zip_ok(path):
                newext = classify_zip(path)
                if newext is None: ok, note = False, 'zip损坏'
            else:
                ok, note, newext = False, 'zip校验失败', 'zip'
        elif ext == 'jpg':
            s = jpg_size(path)
            if s: dims = f'{s[0]}x{s[1]}'
            else: ok, note = False, '无SOF帧头'
        elif ext == 'png':
            s = png_size(path)
            if s: dims = f'{s[0]}x{s[1]}'
            else: ok, note = False, 'IHDR异常'
        if newext != ext and newext:
            newfn = fn[:-len(ext)] + newext
            os.rename(path, os.path.join(indir, newfn))
            fn = newfn
        key = (newext, ok)
        summary[key] = summary.get(key, 0) + 1
        rows.append([fn, newext, 'OK' if ok else 'BAD', note, dims])
    with open(os.path.join(indir, 'verified_summary.csv'), 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(['file', 'type', 'status', 'note', 'dims'])
        w.writerows(rows)
    print('== 分类统计 ==')
    for (e, ok), n in sorted(summary.items(), key=lambda x: -x[1]):
        print(f'{e:8s} {"OK" if ok else "BAD":3s} {n}')

if __name__ == '__main__':
    main(sys.argv[1])
