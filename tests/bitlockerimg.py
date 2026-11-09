# -*- coding: utf-8 -*-
"""合成BitLocker卷构造器(测试夹具) — 与engine/bitlocker.py同一格式约定
自建自解: 加密侧用同一个CNG桥(XTS/CCM)与拉伸算法, 验证解析/解锁/解密全链路。
布局(单分区镜像): 扇区0=卷头, 元数据×3@0x2000/0x22000/0x42000(各64KB),
引导扇区备份(密文)@BOOT_BACKUP, 其余=XTS加密的FS数据。
"""
import os
import struct
import hashlib
import uuid

import cng

SS = 512
META_OFFS = (0x2000, 0x22000, 0x42000)
META_SIZE = 0x10000
BOOT_BACKUP = 0x10000                    # 64KB: 元数据0结束处后
VOL_SIZE = 12 * 1024 * 1024
PASSWORD = '测试Password123'
RECOVERY_Q = [12345, 23456, 34567, 45678, 56789, 61234, 32109, 43210]   # 商q → 组=q*11

CIPHER = 0x8005                          # XTS-AES-256


def datum(entry, value, payload):
    return struct.pack('<HHHH', 8 + len(payload), entry, value, 0) + payload


def build(path):
    """→ {'password','recovery','vmk','fvek'}"""
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
    import bitlocker as bl

    rng = os.urandom
    vmk = rng(32)                        # VMK本身
    fvek = rng(64)                       # XTS-256: 数据密钥32+调频密钥32
    salt_pw = rng(16)
    salt_rk = rng(16)

    # 原始明文FS区(带可识别结构): exFAT风格MBR区+数据
    plain = bytearray(VOL_SIZE)
    plain[0:16] = b'PLAINHEAD-SECTOR00'                  # 扇区0特征
    for i in range(1, VOL_SIZE // SS):
        plain[i * SS:i * SS + 8] = struct.pack('<IIB', i, 0xAA55AA55, i & 0xFF)

    def key_datum_payload(algo, key):
        # CCM负载里是完整KEY datum(含8B datum头), 不只是algo+key
        return datum(2, 1, struct.pack('<HH', algo, 0) + key)

    # 密码保护VMK: STRETCH + AES_CCM(拉伸钥匙加密的KEY datum(VMK))
    inter_pw = bl.password_intermediate(PASSWORD)
    stretched_pw = bl.stretch(inter_pw, salt_pw)
    vmk_key_datum = key_datum_payload(0x2003, vmk)       # KEY datum(algo=VMK=0x2003)
    nonce = rng(12)
    enc_vmk, mac_vmk = cng.ccm_crypt(stretched_pw, nonce, b'\x00' * 16,
                                     vmk_key_datum, decrypt=False)
    stretch_datum = datum(2, 3, struct.pack('<HH', 0x1000, 0) + salt_pw)
    ccm_datum = datum(2, 5, nonce + mac_vmk + enc_vmk)
    prot_nonce = bytearray(12)
    struct.pack_into('<H', prot_nonce, 10, 0x2000)       # 保护域=用户密码
    vmk_datum_pw = datum(2, 8, uuid.uuid4().bytes + bytes(prot_nonce) +
                         stretch_datum + ccm_datum)

    # 恢复口令保护VMK
    rkey = ''.join('%06d' % (q * 11) for q in RECOVERY_Q)
    inter_rk, _ = bl.recovery_intermediate(rkey)
    stretched_rk = bl.stretch(inter_rk, salt_rk)
    nonce2 = rng(12)
    enc2, mac2 = cng.ccm_crypt(stretched_rk, nonce2, b'\x00' * 16,
                               vmk_key_datum, decrypt=False)
    vmk_datum_rk = datum(2, 8, uuid.uuid4().bytes + b'\x00' * 10 +
                         struct.pack('<H', 0x0800) +     # 保护域=恢复口令
                         datum(2, 3, struct.pack('<HH', 0x1000, 0) + salt_rk) +
                         datum(2, 5, nonce2 + mac2 + enc2))

    # FVEK datum: AES_CCM(VMK加密的KEY datum(algo=0x8005, fvek64))
    nonce3 = rng(12)
    enc3, mac3 = cng.ccm_crypt(vmk, nonce3, b'\x00' * 16,
                               key_datum_payload(CIPHER, fvek), decrypt=False)
    fvek_datum = datum(3, 5, nonce3 + mac3 + enc3)

    # ---- 元数据块 ----
    def meta_block(counter):
        datums_body = vmk_datum_pw + vmk_datum_rk + fvek_datum
        ds = bytearray(0x30)
        struct.pack_into('<I', ds, 0, 0x30 + len(datums_body))
        struct.pack_into('<I', ds, 4, 1)
        struct.pack_into('<I', ds, 8, 0x30)
        struct.pack_into('<I', ds, 0xC, 0x30 + len(datums_body))
        ds[0x10:0x20] = uuid.uuid4().bytes
        struct.pack_into('<I', ds, 0x20, counter)
        struct.pack_into('<H', ds, 0x24, CIPHER)
        blk = bytearray(0x70 + len(datums_body))
        blk[0:8] = b'-FVE-FS-'
        struct.pack_into('<H', blk, 8, 0x1000)           # size×16=64KB
        struct.pack_into('<H', blk, 0xA, 2)              # version 2
        struct.pack_into('<H', blk, 0xC, 2)              # curr_state=2(已加密)
        struct.pack_into('<Q', blk, 0x10, VOL_SIZE)
        for i, off in enumerate(META_OFFS):
            struct.pack_into('<Q', blk, 0x20 + 8 * i, off)
        struct.pack_into('<Q', blk, 0x38, BOOT_BACKUP)
        blk[0x40:0x70] = ds                              # dataset @0x40
        blk[0x70:0x70 + len(datums_body)] = datums_body  # datum @0x70
        return bytes(blk)

    # ---- 组装密文卷 ----
    img = bytearray(VOL_SIZE)
    # 加密数据区(除扇区0与元数据区; 引导备份单独处理)
    def in_meta(pos):
        return any(m <= pos < m + META_SIZE for m in META_OFFS)

    for s in range(VOL_SIZE // SS):
        if s == 0 or in_meta(s * SS) or (BOOT_BACKUP <= s * SS < BOOT_BACKUP + SS):
            continue
        tweak = struct.pack('<QQ', s, 0)
        img[s * SS:(s + 1) * SS] = cng.xts_crypt(
            fvek, tweak, bytes(plain[s * SS:(s + 1) * SS]), decrypt=False)
    # 扇区0密文 → 引导备份位(用备份位的扇区号做tweak)
    img[BOOT_BACKUP:BOOT_BACKUP + SS] = cng.xts_crypt(
        fvek, struct.pack('<QQ', BOOT_BACKUP // SS, 0),
        bytes(plain[0:SS]), decrypt=False)
    # 元数据副本
    for i, off in enumerate(META_OFFS):
        blk = meta_block(counter=10 + i)
        img[off:off + len(blk)] = blk
    # 卷头(扇区0)
    vh = bytearray(SS)
    vh[0:3] = b'\xEB\x52\x90'
    vh[3:11] = b'-FVE-FS-'
    struct.pack_into('<H', vh, 0xB, SS)
    struct.pack_into('<Q', vh, 0x38, BOOT_BACKUP)
    for i, off in enumerate(META_OFFS):
        struct.pack_into('<Q', vh, 0xB0 + 8 * i, off)
    vh[510:512] = b'\x55\xaa'
    img[0:SS] = vh

    with open(path, 'wb') as f:
        f.write(img)
    return {'password': PASSWORD, 'recovery': rkey, 'vmk': vmk.hex(),
            'fvek': fvek.hex()}
