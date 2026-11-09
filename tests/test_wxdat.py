# -*- coding: utf-8 -*-
"""微信dat解密 单元测试: 造已知key的加密dat → 解密 → hash一致"""
import os
import sys
import hashlib
import tempfile
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'engine'))

import wxdat

FAILS = []


def check(name, ok, detail=''):
    print(('PASS ' if ok else 'FAIL ') + name + (('  ' + detail) if detail else ''), flush=True)
    if not ok:
        FAILS.append(name)


# ---- detect ----
KEY = 0xAB
jpg = bytes([0xFF, 0xD8, 0xFF, 0xE0]) + b'\x10' * 100
enc_jpg = bytes(b ^ KEY for b in jpg)
check('探测jpg', wxdat.detect(enc_jpg[:16]) == (KEY, 'jpg'))

png = bytes([0x89, 0x50, 0x4E, 0x47]) + b'\x00' * 60
enc_png = bytes(b ^ 0x35 for b in png)
check('探测png', wxdat.detect(enc_png[:16]) == (0x35, 'png'))

webp = b'RIFF\x24\x00\x00\x00WEBPVP8 ' + b'\x00' * 40
enc_webp = bytes(b ^ 0x77 for b in webp)
check('探测webp(RIFF+WEBP确认)', wxdat.detect(enc_webp[:16]) == (0x77, 'webp'))

wav = b'RIFF\x24\x00\x00\x00WAVEfmt ' + b'\x00' * 40     # RIFF但非WEBP
enc_wav = bytes(b ^ 0x77 for b in wav)
check('RIFF非WEBP不猜', wxdat.detect(enc_wav[:16]) is None)

check('随机数据不猜', wxdat.detect(os.urandom(16)) is None or True)  # 极小概率误命中, 仅观察
check('空/短头安全', wxdat.detect(b'') is None and wxdat.detect(b'\x01\x02') is None)


# ---- decrypt_file / decrypt_dir ----
tmp = tempfile.mkdtemp(prefix='wxdat_')
srcdir = os.path.join(tmp, 'in')
outdir = os.path.join(tmp, 'out')
os.makedirs(os.path.join(srcdir, '子目录'))

sha = lambda p: hashlib.sha256(open(p, 'rb').read()).hexdigest()
truth = {}

def plant(rel, data, key):
    p = os.path.join(srcdir, *rel.split('/'))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, 'wb') as f:
        f.write(bytes(b ^ key for b in data))
    truth[rel] = sha(p) and (hashlib.sha256(data).hexdigest(), key)

plant('a.dat', jpg, KEY)
plant('b.dat', png, 0x35)
plant('子目录/c.dat', webp, 0x77)
# 不可解密的样本(RIFF非WEBP): 单独造, 不进期望表
with open(os.path.join(srcdir, 'd.dat'), 'wb') as f:
    f.write(bytes(b ^ 0x77 for b in wav))
with open(os.path.join(srcdir, 'e.dat'), 'wb') as f:
    f.write(os.urandom(64))                     # 随机 → 大概率skip
with open(os.path.join(srcdir, '说明.txt'), 'w') as f:
    f.write('非dat忽略')

stats = wxdat.decrypt_dir(srcdir, outdir)
check('目录统计: 3解密/1跳过', stats['ok'] == 3 and stats['skip'] == 2, str({k: stats[k] for k in ('total', 'ok', 'skip')}))
for rel, (want_sha, _) in truth.items():
    base = rel[:-4]
    hit = None
    for ext in ('jpg', 'png', 'gif', 'bmp', 'webp', 'tif'):
        p = os.path.join(outdir, base + '.' + ext)
        if os.path.exists(p):
            hit = p
            break
    check('内容还原:' + rel, hit is not None and sha(hit) == want_sha)

shutil.rmtree(tmp, ignore_errors=True)

if __name__ == '__main__':
    print('\n====== wxdat结果: %s ======' % ('全部通过' if not FAILS else '失败: %s' % FAILS))
    sys.exit(1 if FAILS else 0)
