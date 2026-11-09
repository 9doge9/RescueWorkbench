# -*- coding: utf-8 -*-
"""Base64递归提取 — 从文本/日志/聊天导出里挖内嵌文件
场景: 聊天记录JSON、日志、代码里嵌着base64的图片/压缩包(数据泄露取证、误存文件的找回)。
方法: 长base64串严格解码 → 按magic识别文件类型 → 对解码后的文本再递归(套娃)。
"""
import os
import re
import base64

RUN_RE = re.compile(rb'[A-Za-z0-9+/]{60,}={0,2}')
MAX_DEPTH = 3

MAGICS = (
    (b'\x89PNG\r\n\x1a\n', 'png', 'image/png'),
    (b'\xff\xd8\xff', 'jpg', 'image/jpeg'),
    (b'GIF87a', 'gif', 'image/gif'),
    (b'GIF89a', 'gif', 'image/gif'),
    (b'PK\x03\x04', 'zip', 'application/zip'),
    (b'%PDF', 'pdf', 'application/pdf'),
    (b'Rar!\x1a\x07', 'rar', 'application/rar'),
    (b'7z\xbc\xaf\x27\x1c', '7z', 'application/x-7z'),
    (b'\x1f\x8b', 'gz', 'application/gzip'),
    (b'RIFF', 'webp', 'application/octet-stream'),
)


def _sniff(data):
    for magic, ext, mime in MAGICS:
        if data.startswith(magic):
            if ext == 'webp' and data[8:12] != b'WEBP':
                continue
            return ext, mime
    return None, None


def extract_from_bytes(data, depth=0, base_name=''):
    """字节流 → [{ext, data, depth, offset}] 只收能识别magic的解码结果"""
    found = []
    if depth > MAX_DEPTH:
        return found
    for m in RUN_RE.finditer(data):
        blob = m.group()
        try:
            pad = b'=' * (-len(blob) % 4)
            dec = base64.b64decode(blob + pad, validate=True)
        except Exception:
            continue
        if len(dec) < 64:
            continue
        ext, _ = _sniff(dec)
        if ext:
            found.append({'ext': ext, 'data': dec, 'depth': depth,
                          'offset': m.start(), 'b64_len': len(blob)})
        elif depth < MAX_DEPTH and dec[:1] not in (b'\x00',):
            # 解出来像文本? 递归套娃
            printable = sum(32 <= b < 127 or b in (9, 10, 13) for b in dec[:256])
            if printable >= len(dec[:256]) * 0.95:
                found += extract_from_bytes(dec, depth + 1)
    return found


def scan_file(path):
    """单文件(≤64MB) → 提取结果"""
    try:
        if os.path.getsize(path) > 64 * 1024 * 1024:
            return []
        with open(path, 'rb') as f:
            data = f.read()
        return extract_from_bytes(data)
    except OSError:
        return []


def scan_dir(directory, out_dir=None, on_progress=None):
    """目录扫描; 给out_dir则落盘(按 原名.ext序号 命名)"""
    results = []
    n = 0
    for dirpath, _, files in os.walk(directory):
        for fn in files:
            p = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(p) > 64 * 1024 * 1024:
                    continue
            except OSError:
                continue
            hits = scan_file(p)
            if hits:
                saved = []
                if out_dir:
                    os.makedirs(out_dir, exist_ok=True)
                    stem = re.sub(r'[^\w\-.]', '_', os.path.splitext(fn)[0])[:40]
                    for i, h in enumerate(hits):
                        dest = os.path.join(out_dir, '%s_%02d.%s' % (stem, i, h['ext']))
                        k = 1
                        while os.path.exists(dest):
                            dest = os.path.join(out_dir, '%s_%02d_%d.%s' % (stem, i, k, h['ext']))
                            k += 1
                        with open(dest, 'wb') as w:
                            w.write(h['data'])
                        saved.append(dest)
                results.append({'file': p, 'hits': [
                    {'ext': h['ext'], 'size': len(h['data']), 'depth': h['depth'],
                     'path': saved[i] if saved else None}
                    for i, h in enumerate(hits)]})
            n += 1
            if on_progress and n % 100 == 0:
                on_progress({'event': 'progress', 'files': n})
    return results
