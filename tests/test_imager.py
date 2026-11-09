# -*- coding: utf-8 -*-
"""imager v2 测试: 坏区注入(二分下探) + 部分续跑"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'engine'))

import imager as im
from mapfile import MapFile, FINISHED, BAD_SECTOR

SRC = os.path.join(os.path.dirname(HERE), '..', '工具评估', 'SectorForge', 'cartest.img')
SRC = os.path.abspath(SRC)
TOTAL = os.path.getsize(SRC)

# 在8-9MB处注入1KB读坏区
BAD_START, BAD_LEN = 8 * 1024 * 1024, 1024

_orig_open = im.open_source


class FlakyFile:
    """读 [BAD_START, BAD_START+BAD_LEN) 时抛IOError"""

    def __init__(self, f):
        self._f = f
        self.name = f.name

    def seek(self, *a):
        return self._f.seek(*a)

    def tell(self):
        return self._f.tell()

    def read(self, n):
        pos = self._f.tell()
        if pos < BAD_START + BAD_LEN and pos + n > BAD_START:
            # 与坏区相交 → 读到坏区边界即报错
            if pos < BAD_START:
                b = self._f.read(BAD_START - pos)
                self._f.seek(BAD_START)      # 游标停在坏区口
                return b if b else None
            raise OSError('simulated bad sector @%d' % pos)
        return self._f.read(n)

    def close(self):
        self._f.close()


flaky_on = {'vrf': False}       # vrf(第二校验句柄)不注入, 只注入主读


def patched_open(path):
    f = _orig_open(path)
    return FlakyFile(f) if not flaky_on['vrf'] else f


im.open_source = patched_open

out = tempfile.mktemp(suffix='.img')
img = im.VerifiedImager(SRC, out, chunk=4 * 1024 * 1024)
res = img.run()
print('结果:', res)

ok = True
# 1. 坏区应精确落在注入范围(扇区粒度)
bad_lo = min(b[0] for b in res['bad_regions']) if res['bad_regions'] else None
bad_hi = max(b[0] + b[1] for b in res['bad_regions']) if res['bad_regions'] else None
print('坏区范围: [%s, %s) 期望 [%d, %d)' % (bad_lo, bad_hi, BAD_START, BAD_START + BAD_LEN))
if not (res['bad_regions'] and bad_lo == BAD_START and bad_hi <= BAD_START + BAD_LEN):
    print('FAIL 坏区定位不准'); ok = False

# 2. map: 坏区'-', 其余'+'
m = MapFile.load(out + '.map', expect_size=TOTAL)
t = m.totals()
print('map totals:', t)
if t[FINISHED] + t[BAD_SECTOR] != TOTAL or t[BAD_SECTOR] != bad_hi - bad_lo:
    print('FAIL map覆盖/坏区字节数不对'); ok = False

# 3. 镜像内容: 坏区外逐字节一致
with open(SRC, 'rb') as fs, open(out, 'rb') as fd:
    fs.seek(0); head_s = fs.read(BAD_START)
    fd.seek(0); head_d = fd.read(BAD_START)
    fs.seek(BAD_START + BAD_LEN); tail_s = fs.read()
    fd.seek(BAD_START + BAD_LEN); tail_d = fd.read()
if head_s != head_d or tail_s != tail_d:
    print('FAIL 好区内容不一致'); ok = False
else:
    print('好区逐字节一致')

# 4. 续跑: map全重置(含坏区, 因注入已解除) → 重读全盘 → 恢复一致
with open(out + '.map', 'w') as f:
    f.write('# test\n0x0     ?\n0x00000000  0x%08X  +\n' % (TOTAL // 2))
    f.write('0x%08X  0x%08X  ?\n' % (TOTAL // 2, TOTAL - TOTAL // 2))
im.open_source = _orig_open
res2 = im.VerifiedImager(SRC, out).run()
same = open(SRC, 'rb').read() == open(out, 'rb').read()
print('续跑后整体一致:', same, 'finished=', res2['finished_bytes'])
if not same or res2['finished_bytes'] != TOTAL:
    print('FAIL 续跑'); ok = False

os.unlink(out)
os.unlink(out + '.map')
print('\n结果:', '全部通过' if ok else '有失败项')
sys.exit(0 if ok else 1)
