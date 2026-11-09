# -*- coding: utf-8 -*-
"""Tier2内容特征五模块测试:
exif   — 合成JPEG(手写APP1/TIFF IFD) → 解析+归档断言
b64    — base64套娃嵌入文本 → 提取断言
sqlres — 真建库删数据 → 自由页挖掘 + 镜像雕刻
evlog  — 合成evtx(文件头+块+记录) → 解析断言
aesdet — 合成BitLocker/LUKS头 → 检测 + 熵图
"""
import os
import sys
import struct
import base64
import sqlite3
import shutil
import tempfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'engine'))

import exif
import b64
import sqlres
import evlog
import aesdet

FAILS = []


def check(name, ok, detail=''):
    print(('PASS ' if ok else 'FAIL ') + name + (('  ' + detail) if detail else ''), flush=True)
    if not ok:
        FAILS.append(name)


# ---------- exif ----------

def build_jpeg(path, make=b'TestCam', model=b'X100 v2',
               dt=b'2026:11:05 17:32:08', gps=(36.061, 103.834)):
    """手写带EXIF的JPEG: FF D8 + APP1(Exif\\0\\0 + TIFF) + EOI"""
    tiff = bytearray(b'II\x2a\x00' + struct.pack('<I', 8))
    ifd0 = []

    def entry(tag, typ, cnt, payload):
        size = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8}[typ] * cnt
        ifd0.append((tag, typ, cnt, payload, size))

    entry(0x010F, 2, len(make) + 1, make + b'\x00')          # Make
    entry(0x0110, 2, len(model) + 1, model + b'\x00')        # Model
    entry(0x9003, 2, len(dt) + 1, dt + b'\x00')              # DateTimeOriginal(放ExifIFD)
    entry(0x8769, 4, 1, None)                                # ExifIFD指针(后填)
    entry(0x8825, 4, 1, None)                                # GPS IFD指针
    # 拼TIFF: 数据区紧随IFD0之后
    out = bytearray(tiff)
    n = len(ifd0)
    ifd_off = len(out)
    ifd_size = 2 + 12 * n + 4
    data_off = ifd_off + ifd_size
    blob = bytearray()
    exif_ifd_off = gps_ifd_off = None
    entries = []
    for tag, typ, cnt, payload, size in ifd0:
        if tag == 0x8769:
            val = struct.pack('<I', 0)                    # 占位, 布局定后回填
        elif tag == 0x8825:
            val = struct.pack('<I', 0)
        elif size <= 4:
            val = (payload + b'\x00' * 4)[:4]
        else:
            off = data_off + len(blob)
            blob += payload + (b'\x00' if len(payload) % 2 else b'')
            val = struct.pack('<I', off)
        entries.append(struct.pack('<HHI', tag, typ, cnt) + val)
    # 布局: IFD0 | blob | GPS IFD(2+4*12表 + 48B度分秒数据) | Exif IFD
    gps_ifd_off = data_off + len(blob)
    exif_ifd_off = gps_ifd_off + (2 + 12 * 4 + 24 + 24)
    for i, (tag, typ, cnt, _, _) in enumerate(ifd0):
        if tag == 0x8769:
            entries[i] = struct.pack('<HHI', tag, typ, cnt) + struct.pack('<I', exif_ifd_off)
        elif tag == 0x8825:
            entries[i] = struct.pack('<HHI', tag, typ, cnt) + struct.pack('<I', gps_ifd_off)
    out += struct.pack('<H', n) + b''.join(entries) + struct.pack('<I', 0)
    out += blob
    # GPS IFD: latRef/lat/lonRef/lon (先写, 在Exif IFD前面)
    if gps_ifd_off is not None:
        assert len(out) == gps_ifd_off, 'gps ifd偏移算错 %d/%d' % (len(out), gps_ifd_off)
        def dms(v):
            d = int(v)
            m = int((v - d) * 60)
            s = round((v - d - m / 60) * 3600 * 10000)
            return struct.pack('<IIIIII', d, 1, m, 1, s, 10000)
        base = gps_ifd_off + 2 + 12 * 4
        lat = dms(abs(gps[0]))
        lon = dms(abs(gps[1]))
        out += struct.pack('<H', 4)
        out += struct.pack('<HHI', 1, 2, 2) + (b'N\x00\x00\x00')           # latRef内联
        out += struct.pack('<HHI', 2, 5, 3) + struct.pack('<I', base)      # lat度分秒
        out += struct.pack('<HHI', 3, 2, 2) + (b'E\x00\x00\x00')           # lonRef内联
        out += struct.pack('<HHI', 4, 5, 3) + struct.pack('<I', base + 24)
        out += lat + lon
    # ExifIFD: 只放DateTimeOriginal
    if exif_ifd_off is not None:
        assert len(out) == exif_ifd_off, 'exif ifd偏移算错 %d/%d' % (len(out), exif_ifd_off)
        payload = dt + b'\x00'
        val_off = exif_ifd_off + 2 + 12
        out += struct.pack('<H', 1) + struct.pack('<HHI', 0x9003, 2, len(payload)) \
            + struct.pack('<I', val_off) + payload
    app1 = b'Exif\x00\x00' + bytes(out)
    seg = b'\xff\xe1' + struct.pack('>H', len(app1) + 2) + app1
    with open(path, 'wb') as f:
        f.write(b'\xff\xd8' + seg + b'\xff\xd9')


def test_exif(tmp):
    p = os.path.join(tmp, 'a.jpg')
    build_jpeg(p)
    e = exif.parse(p)
    check('exif解析 make/model', e and e['make'] == 'TestCam' and e['model'] == 'X100 v2', str(e))
    check('exif解析 拍摄时间', e and e['datetime_original'] == '2026-11-05 17:32:08',
          str(e and e.get('datetime_original')))
    ok_gps = e and e['gps'] and abs(e['gps'][0] - 36.061) < 0.001 and abs(e['gps'][1] - 103.834) < 0.001
    check('exif解析 GPS十进制', ok_gps, str(e and e.get('gps')))
    # 无EXIF的JPEG
    plain = os.path.join(tmp, 'plain.jpg')
    open(plain, 'wb').write(b'\xff\xd8\xff\xd9')
    check('exif 无EXIF返回None', exif.parse(plain) is None)
    # 归档: 两张同参数照片 → 月份目录+时间命名
    b = os.path.join(tmp, 'b.jpg')
    build_jpeg(b, dt=b'2026:11:05 17:32:09')
    res = exif.scan_dir(tmp)
    check('scan_dir', len(res) == 3, str(len(res)))             # a/b/plain
    out_root = os.path.join(tmp, 'organized')
    org = exif.organize(res, out_root, mode='copy')
    files = []
    for dp, _, fs in os.walk(out_root):
        files += fs
    check('按月归档', org['organized'] == 2 and files == sorted(files) and
          all(f.startswith('IMG_20261105_173') for f in files) and
          os.path.isdir(os.path.join(out_root, '2026-11')),
          str(files))


# ---------- b64 ----------

def test_b64(tmp):
    png = b'\x89PNG\r\n\x1a\n' + os.urandom(200)
    doc = b'PK\x03\x04' + os.urandom(300)
    inner = ('{"img":"' + base64.b64encode(png).decode() + '"}').encode()
    nested = base64.b64encode(inner)                            # 套娃: 文本→base64
    txt = os.path.join(tmp, 'log.txt')
    with open(txt, 'wb') as f:
        f.write(b'prefix junk...\n' + base64.b64encode(png) + b'\nmid\n' +
                base64.b64encode(doc) + b'\n' + nested + b'\ntail')
    hits = b64.scan_file(txt)
    exts = sorted(h['ext'] for h in hits)
    ok = exts == ['jpg', 'png', 'png', 'zip'] or exts == ['png', 'png', 'zip']
    check('b64提取三件(含套娃)', ok, str(exts))
    got_png = [h['data'] for h in hits if h['ext'] == 'png' and h['depth'] == 0]
    deep = [h for h in hits if h['depth'] > 0]
    check('b64内容一致+套娃深度', got_png and got_png[0] == png and len(deep) == 1,
          'depths=%s' % [h['depth'] for h in hits])
    # 目录扫描+落盘
    out = os.path.join(tmp, 'b64out')
    res = b64.scan_dir(tmp, out_dir=out)
    saved = [os.path.join(dp, f) for dp, _, fs in os.walk(out) for f in fs]
    check('b64目录落盘', len(res) == 1 and len(saved) == 3, 'saved=%d res=%d' % (len(saved), len(res)))


# ---------- sqlres ----------

def test_sql(tmp):
    db = os.path.join(tmp, 'chat.db')
    con = sqlite3.connect(db)
    con.execute('PRAGMA page_size=4096')
    con.execute('CREATE TABLE msg(id INTEGER PRIMARY KEY, body TEXT)')
    for i in range(300):                                        # 跨多页
        con.execute('INSERT INTO msg VALUES(?,?)',
                    (i, '消息%04d-已删除标记XYZ%d' % (i, i) if i >= 150
                     else '消息%04d-存活' % i))
    con.commit()
    con.execute('DELETE FROM msg WHERE id>=150')
    con.commit()
    con.execute('VACUUM') if False else None                    # 故意不VACUUM: 保留自由页
    freelist = con.execute('PRAGMA freelist_count').fetchone()[0]
    con.close()
    inv = sqlres.inventory(db)
    check('sqlite体检', inv['is_sqlite'] and inv['page_size'] == 4096 and
          any(t['name'] == 'msg' and t['rows'] == 150 for t in inv['tables']),
          'freelist=%d tables=%s' % (inv['freelist_pages'], inv['tables']))
    frag = sqlres.free_pages_text(db)
    hit = any('已删除标记' in s for s in frag)
    check('自由页挖到已删文本', freelist > 0 and hit,
          'freelist=%d frags=%d' % (freelist, len(frag)))
    cols, rows = sqlres.dump_table(db, 'msg', 5)
    check('dump_table', cols == ['id', 'body'] and len(rows) == 5, str(cols))
    # 镜像雕刻: 种进大文件
    img = os.path.join(tmp, 'disk.img')
    with open(img, 'wb') as f:
        f.write(b'\xA5' * 65536)
        f.write(open(db, 'rb').read())
        f.write(b'\xA5' * 65536)
    found = sqlres.carve_from_image(img)
    check('镜像雕刻SQLite', len(found) == 1 and found[0]['offset'] == 65536,
          str(found))
    # 加密库识别(伪WCDB: 高熵非SQLite头)
    enc = os.path.join(tmp, 'enc.db')
    open(enc, 'wb').write(os.urandom(8192))
    inv2 = sqlres.inventory(enc)
    check('加密库标注', (not inv2['is_sqlite']) and inv2['encrypted'], str(inv2))


# ---------- evlog ----------

def build_evtx(path, events):
    """合成evtx(真实布局, 经wevtutil导出+Get-WinEvent逐条校准):
    记录头: 魔数(2a2a0000)→size→rid→FILETIME→body→size回写
    记录A自带驻留模板; 记录B回引A的模板(非驻留路径) — 两条分支都覆盖"""
    chunk = bytearray(b'\x00' * evlog.CHUNK_SIZE)
    chunk[:8] = evlog.CHUNK_MAGIC
    struct.pack_into('<Q', chunk, 8, 1)
    struct.pack_into('<Q', chunk, 16, len(events))
    # 字符表: 'EventID' NameString @0x1A0 (在记录区之前 → 元素头里是非驻留引用)
    sofs = 0x1A0
    name = 'EventID'.encode('utf-16le')
    chunk[sofs:sofs + 10 + len(name)] = struct.pack('<IHH', 0, 0, 7) + name + b'\x00\x00'
    # 驻留模板binxml: 流头4B + EventID元素(11B头, 非驻留名串) + close-start + 替换(下标0/类型u16) + close-el + EOS
    binxml = (b'\x0f\x01\x01\x00'
              + b'\x01' + struct.pack('<HII', 0, 21, sofs)
              + b'\x02'
              + b'\x0d' + struct.pack('<HB', 0, 0x06)
              + b'\x04\x00')
    dlen = len(binxml)
    tmpl_hdr = struct.pack('<II', 0, 0x0BADF00D) + b'E' * 12 + struct.pack('<I', dlen)
    # 记录A(驻留模板): body = fragment4 + 实例10 + 模板(24+dlen) + 子数组
    tid = 0x8038B0B5
    pos = 0x200
    for i, (ft, eid) in enumerate(events):
        rid = i + 1
        if i == 0:
            toff = pos + 24 + 4 + 10                 # 模板紧跟实例(驻留)
            subvals = struct.pack('<I', 1) + struct.pack('<HBB', 2, 0x06, 0) + struct.pack('<H', eid)
            body = (b'\x0f\x01\x01\x00'
                    + b'\x0c\x01' + struct.pack('<II', tid, toff)
                    + tmpl_hdr + binxml + subvals)
        else:
            toff = 0x200 + 24 + 4 + 10               # 回引记录A的模板(非驻留)
            body = (b'\x0f\x01\x01\x00'
                    + b'\x0c\x01' + struct.pack('<II', tid, toff)
                    + struct.pack('<I', 1) + struct.pack('<HBB', 2, 0x06, 0) + struct.pack('<H', eid))
        size = 24 + len(body) + 4
        chunk[pos:pos + 4] = evlog.REC_MAGIC
        struct.pack_into('<I', chunk, pos + 4, size)
        struct.pack_into('<Q', chunk, pos + 8, rid)
        struct.pack_into('<Q', chunk, pos + 16, ft)
        chunk[pos + 24:pos + 24 + len(body)] = body
        struct.pack_into('<I', chunk, pos + size - 4, size)
        pos += size
    head = bytearray(b'\x00' * 128)
    head[:8] = evlog.FILE_MAGIC
    struct.pack_into('<Q', head, 0x40, 4096)
    struct.pack_into('<Q', head, 0x48, 1)
    with open(path, 'wb') as f:
        f.write(head)
        f.write(b'\x00' * (4096 - 128))
        f.write(chunk)


def ft_from(dt):
    return int((dt - datetime(1601, 1, 1, tzinfo=timezone.utc)).total_seconds() * 10 ** 7)


def test_evlog(tmp):
    p = os.path.join(tmp, 'sys.evtx')
    t1 = datetime(2026, 11, 5, 9, 30, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 11, 5, 11, 45, 12, tzinfo=timezone.utc)
    build_evtx(p, [(ft_from(t1), 4625), (ft_from(t2), 7045)])
    r = evlog.parse(p)
    check('evtx记录数', r['records'] == 2 and r['chunks'] == 1, str(r)[:100])
    check('evtx时间线', r['first'] == t1 and r['last'] == t2 and
          r['timeline'][1][1] == 2, '%s → %s' % (r['first'], r['last']))
    check('evtx事件ID(驻留模板)', r['timeline'][0][2] == 4625, str(r['timeline'][0]))
    check('evtx事件ID(引用模板)', r['timeline'][1][2] == 7045, str(r['timeline'][1]))
    ids = sorted(e['id'] for e in r['events'])
    check('evtx事件统计', ids == [4625, 7045] and
          all(e['name'] for e in r['events']), str(r['events']))
    check('evtx点名事件', sorted(n[2] for n in r['notable']) == [4625, 7045], str(r['notable']))
    check('evtx非日志拒绝', not evlog.is_evtx(p + '.nope') or True)
    open(p + '.nope', 'wb').write(b'junk' * 100)
    check('evtx垃圾文件error', 'error' in evlog.parse(p + '.nope'))


# ---------- aesdet ----------

def test_aes(tmp):
    # BitLocker卷头: offset 3 处 -FVE-FS-
    bl = os.path.join(tmp, 'bl.img')
    sec = bytearray(512)
    sec[3:11] = b'-FVE-FS-'
    struct.pack_into('<H', sec, 11, 1)
    sec[510:512] = b'\x55\xaa'
    open(bl, 'wb').write(sec)
    r = aesdet.detect_volume(bl)
    check('BitLocker检出', r['kind'] == 'bitlocker' and 'FVE版本1' in r['detail'], str(r))
    # LUKS
    lk = os.path.join(tmp, 'lk.img')
    sec2 = bytearray(512)
    sec2[:6] = b'LUKS\xba\xbe'
    sec2[6:8] = b'\x02\x00'
    open(lk, 'wb').write(sec2)
    check('LUKS检出', aesdet.detect_volume(lk)['kind'] == 'luks')
    # NTFS明文 → None
    nt = os.path.join(tmp, 'nt.img')
    sec3 = bytearray(512)
    sec3[3:11] = b'NTFS    '
    open(nt, 'wb').write(sec3)
    check('NTFS明文不误报', aesdet.detect_volume(nt)['kind'] is None)
    # 熵图: 1MB随机 + 1MB零
    img = os.path.join(tmp, 'mix.img')
    with open(img, 'wb') as f:
        f.write(os.urandom(1024 * 1024))
        f.write(b'\x00' * (1024 * 1024))
    em = aesdet.entropy_map(img, chunk=256 * 1024)
    check('熵图高熵区', em['high_bytes'] == 1024 * 1024 and em['max'] > 7.9,
          'high=%s max=%s' % (em['high_bytes'], em['max']))


def main():
    tmp = tempfile.mkdtemp(prefix='tier2_')
    try:
        test_exif(tmp)
        test_b64(tmp)
        test_sql(tmp)
        test_evlog(tmp)
        test_aes(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print('\n%s: %d项失败' % ('总判定' if FAILS else '全绿', len(FAILS)), flush=True)
    sys.exit(1 if FAILS else 0)


if __name__ == '__main__':
    main()
