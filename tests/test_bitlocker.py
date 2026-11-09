# -*- coding: utf-8 -*-
"""BitLocker解锁测试(合成夹具tests/bitlockerimg.py, 自建自解闭环):
A. CNG桥: XTS往返+CCM认证失败返回None
B. 恢复口令蒸馏: 48位↔16字节中间键(÷11规则校验)
C. 卷识别: 卷头/元数据/保护器清单(密码+恢复口令)
D. 密码解锁 → VMK/FVEK/算法0x8005; 错误密码拒绝
E. 恢复口令解锁
F. 整卷解密: 明文区外逐字节与原始一致(扇区0从引导备份还原, 元数据区保留)
"""
import os
import sys
import struct
import hashlib
import tempfile
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'engine'))
sys.path.insert(0, HERE)

import cng
import bitlocker as bl
import bitlockerimg as bli

FAILS = []


def check(name, ok, detail=''):
    print(('PASS ' if ok else 'FAIL ') + name + (('  ' + detail) if detail else ''), flush=True)
    if not ok:
        FAILS.append(name)


def test_cng():
    key = os.urandom(64)
    data = os.urandom(512)
    tweak = struct.pack('<QQ', 1234, 0)
    enc = cng.xts_crypt(key, tweak, data, decrypt=False)
    dec = cng.xts_crypt(key, tweak, enc, decrypt=True)
    check('XTS往返', dec == data)
    dec2 = cng.xts_crypt(key, struct.pack('<QQ', 1235, 0), enc, decrypt=True)
    check('XTS不同tweak不同结果', dec2 != data)
    # CCM: 与cryptography库交叉验证(外部裁判) + 往返 + 认证失败
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESCCM
        k = os.urandom(32)
        n = os.urandom(12)
        msg = b'VMK-KEY-DATUM-TEST-DATA' * 3
        ct_ref = AESCCM(k).encrypt(n, msg, None)       # ct‖tag16
        ct, mac = cng.ccm_crypt(k, n, b'\x00' * 16, msg, decrypt=False)
        check('CCM与cryptography库一致', ct == ct_ref[:-16] and mac == ct_ref[-16:],
              'got %s.. ref %s..' % (mac.hex()[:8], ct_ref[-16:].hex()[:8]))
    except ImportError:
        print('SKIP CCM外部对照(无cryptography库)')
    k = os.urandom(32)
    n = os.urandom(12)
    ct, mac = cng.ccm_crypt(k, n, b'\x00' * 16, b'HELLO-VMK-TEST', decrypt=False)
    pt = cng.ccm_crypt(k, n, mac, ct, decrypt=True)
    check('CCM往返', pt == b'HELLO-VMK-TEST')
    bad = cng.ccm_crypt(k, n, bytes(b ^ 1 for b in mac), ct, decrypt=True)
    check('CCM认证失败返回None', bad is None)


def test_recovery_distill():
    key = os.urandom(16)
    # 逆推合法口令: q→组=q*11
    qs = [1000, 2000, 3000, 4000, 5000, 6000, 12345, 65535 // 11 * 11]
    rp = ''.join('%06d' % (q // 11 * 11) if False else '%06d' % ((q // 11) * 11) for q in [x * 11 for x in (1000, 2000, 3000, 4000, 5000, 60000, 12345, 65535 // 11 // 11 * 11 * 11)])
    rp = ''.join('%06d' % (g // 11 * 11) if False else '%06d' % g
                 for g in [q * 11 for q in (1000, 2000, 3000, 4000, 5000, 60000, 12345, 6553)])
    inter, raw = bl.recovery_intermediate(rp)
    expect = b''.join(struct.pack('<H', q) for q in
                      (1000, 2000, 3000, 4000, 5000, 60000, 12345, 6553))
    check('恢复口令→128位(÷11小端)', raw == expect, '%s vs %s' % (raw.hex(), expect.hex()))
    check('恢复口令SHA256中间键', inter == hashlib.sha256(expect).digest())
    try:
        bl.recovery_intermediate('123451-123451-123451-123451-123451-123451-123451-123451')
        ok = False
    except ValueError:
        ok = True
    check('不整除11的组拒绝', ok)


def test_unlock():
    img = os.path.join(TMP, 'bl.img')
    facts = bli.build(img)
    info = bl.load(img, 0)
    check('卷识别', info.get('ok') and info['algorithm'] == 0x8005 and
          info['volume_size'] == bli.VOL_SIZE,
          str(info.get('error') or info.get('algorithm')))
    kinds = {p['kind'] for p in info['protectors']}
    check('保护器清单', kinds == {'用户密码', '恢复口令'}, str(kinds))
    r = bl.unlock(img, 0, password=facts['password'])
    check('密码解锁', r.get('ok') and r['via'] == '密码' and r['algo'] == 0x8005 and
          r['fvek_key'].hex() == facts['fvek'],
          str(r)[:120])
    r2 = bl.unlock(img, 0, password='错误密码')
    check('错误密码拒绝', not r2.get('ok'), str(r2.get('error'))[:80])
    r3 = bl.unlock(img, 0, recovery=facts['recovery'])
    check('恢复口令解锁', r3.get('ok') and r3['via'] == '恢复口令' and
          r3['fvek_key'].hex() == facts['fvek'], str(r3)[:100])


def test_decrypt():
    img = os.path.join(TMP, 'bl.img')
    facts = bli.build(img)
    r = bl.unlock(img, 0, password=facts['password'])
    out = os.path.join(TMP, 'dec.img')
    p = bl.decrypt_range(img, 0, r['fvek_key'], r['algo'], out)
    check('解密产出', p == out)
    got = open(out, 'rb').read()
    # 期望: 除元数据区外逐扇区==构造器明文; 扇区0=原引导扇区(PLAINHEAD特征)
    check('扇区0=原引导扇区还原', got[0:16] == b'PLAINHEAD-SECTOR', got[0:16].hex())
    ok_all = True
    bad = 0
    for s in range(1, bli.VOL_SIZE // 512):
        off = s * 512
        if any(m <= off < m + bli.META_SIZE for m in bli.META_OFFS) or \
                bli.BOOT_BACKUP <= off < bli.BOOT_BACKUP + 512:
            continue
        if got[off:off + 9] != struct.pack('<IIB', s, 0xAA55AA55, s & 0xFF):
            ok_all = False
            bad += 1
    check('数据区逐扇区一致', ok_all, 'bad=%d' % bad)
    check('解密镜像首扇区有明文特征', _looks_like_fs(got))


def _looks_like_fs(got):
    """解密后的镜像首扇区应有可识别结构特征(本合成盘=PLAINHEAD)"""
    return got[0:16] == b'PLAINHEAD-SECTOR'


TMP = tempfile.mkdtemp(prefix='bltest_')


def main():
    try:
        test_cng()
        test_recovery_distill()
        test_unlock()
        test_decrypt()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print('\n%s: %d项失败' % ('总判定' if FAILS else '全绿', len(FAILS)), flush=True)
    sys.exit(1 if FAILS else 0)


if __name__ == '__main__':
    main()
