# -*- coding: utf-8 -*-
"""微信图片.dat解密
微信PC端把缓存图片整文件单字节XOR加密。原始格式头已知(JPEG/PNG/GIF/BMP/WebP/TIFF),
用 dat头 XOR magic头 反推密钥(3字节一致即命中), 再整文件异或还原。
兼容3.x/4.x的XOR型dat; 探测不中则报告"非加密dat或未知格式"。
"""
import os

# (magic前缀, 扩展名, 需要的最少字节数)——RIFF/WebP单列处理, 不入此表
MAGICS = [
    (bytes([0xFF, 0xD8, 0xFF]), 'jpg', 3),
    (bytes([0x89, 0x50, 0x4E, 0x47]), 'png', 4),          # .PNG
    (bytes([0x47, 0x49, 0x46, 0x38]), 'gif', 4),          # GIF8
    (bytes([0x42, 0x4D]), 'bmp', 2),                      # BM
    (bytes([0x49, 0x49, 0x2A, 0x00]), 'tif', 4),          # II*
    (bytes([0x4D, 0x4D, 0x00, 0x2A]), 'tif', 4),          # MM*
]
RIFF = bytes([0x52, 0x49, 0x46, 0x46])
WEBP = bytes([0x57, 0x45, 0x42, 0x50])


def detect(head):
    """head: dat文件头字节(>=12字节更稳) → (key, ext) 或 None"""
    if not head or len(head) < 3:
        return None
    # RIFF/WEBP特判: 前缀XOR反推key, 用key解出8..12处应为WEBP(否则可能是wav/avi, 不猜)
    if len(head) >= 12:
        k = head[0] ^ RIFF[0]
        if (head[1] ^ RIFF[1]) == k and (head[2] ^ RIFF[2]) == k and (head[3] ^ RIFF[3]) == k:
            probe = bytes(b ^ k for b in head[8:12])
            if probe == WEBP:
                return (k, 'webp')
    for magic, ext, need in MAGICS:
        if len(head) < need:
            continue
        k = head[0] ^ magic[0]
        ok = True
        for i in range(1, len(magic)):
            if (head[i] ^ magic[i]) != k:
                ok = False
                break
        if ok:
            return (k, ext)
    return None


def decrypt_file(src, dst, prefer_key=None):
    """解密单个dat → dst(不带扩展名, 函数自己加). 返回 (ext, key) 或 None(探测失败)"""
    with open(src, 'rb') as f:
        head = f.read(16)
    hit = None
    if prefer_key is not None and len(head) >= 3:
        # 快路径: 先试上次成功的key
        plain = bytes(b ^ prefer_key for b in head[:4])
        for magic, ext, need in MAGICS:
            if plain[:len(magic)] == magic:
                hit = (prefer_key, ext)
                break
        if hit is None and len(head) >= 12 and plain[:4] == RIFF \
                and bytes(b ^ prefer_key for b in head[8:12]) == WEBP:
            hit = (prefer_key, 'webp')
    if hit is None:
        hit = detect(head)
    if hit is None:
        return None
    key, ext = hit
    out_path = dst + '.' + ext
    with open(src, 'rb') as f, open(out_path, 'wb') as w:
        while True:
            b = f.read(1024 * 1024)
            if not b:
                break
            w.write(bytes(x ^ key for x in b))
    return (ext, key)


def decrypt_dir(indir, outdir, on_progress=None):
    """批量解密目录树下的*.dat → outdir保持相对结构.
    返回 {total, ok, skip, fail, exts:{}, key}"""
    dats = []
    for dp, _, names in os.walk(indir):
        for n in names:
            if n.lower().endswith('.dat'):
                dats.append((dp, n))
    total = len(dats)
    ok = skip = fail = 0
    exts = {}
    last_key = None
    for i, (dp, n) in enumerate(dats):
        src = os.path.join(dp, n)
        rel = os.path.relpath(dp, indir)
        od = os.path.join(outdir, rel) if rel != '.' else outdir
        os.makedirs(od, exist_ok=True)
        try:
            r = decrypt_file(src, os.path.join(od, n[:-4]), prefer_key=last_key)
            if r:
                ext, k = r
                last_key = k
                ok += 1
                exts[ext] = exts.get(ext, 0) + 1
            else:
                skip += 1
        except OSError:
            fail += 1
        if on_progress and (i % 20 == 0 or i == total - 1):
            on_progress({'event': 'progress', 'files_done': i + 1, 'files_total': total,
                         'ok': ok, 'skip': skip, 'fail': fail})
    return {'total': total, 'ok': ok, 'skip': skip, 'fail': fail, 'exts': exts, 'key': last_key}
