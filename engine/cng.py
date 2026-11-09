# -*- coding: utf-8 -*-
"""Windows CNG (bcrypt.dll) 加密桥 — AES-XTS / AES-CCM, ctypes零依赖
BitLocker解锁的密码学原语全走系统库: 扇区解密(XTS)是C速度, CCM用于VMK/FVEK。
XTS的IV语义: 16字节tweak(数据单元号) —— BitLocker即扇区号小端16字节。
"""
import ctypes
from ctypes import wintypes

bcrypt = ctypes.windll.bcrypt

STATUS_SUCCESS = 0
BCRYPT_AES_ALGORITHM = 'AES'
BCRYPT_CHAINING_MODE = 'ChainingMode'
BCRYPT_CHAIN_MODE_XTS = 'ChainingModeXTS'
BCRYPT_CHAIN_MODE_CCM = 'ChainingModeCCM'


class _AUTH_INFO(ctypes.Structure):
    """BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO (x64布局)"""
    _fields_ = [
        ('cbSize', wintypes.ULONG),
        ('dwInfo', wintypes.ULONG),
        ('pbNonce', ctypes.c_void_p),
        ('cbNonce', wintypes.ULONG),
        ('pbAuthData', ctypes.c_void_p),
        ('cbAuthData', wintypes.ULONG),
        ('pbTag', ctypes.c_void_p),
        ('cbTag', wintypes.ULONG),
        ('pbMacContext', ctypes.c_void_p),
        ('cbMacContext', wintypes.ULONG),
        ('cbAAD', wintypes.ULONG),
        ('cbData', ctypes.c_ulonglong),
        ('dwFlags', wintypes.ULONG),
    ]


def _check(status, what):
    if status != STATUS_SUCCESS:
        raise OSError('CNG %s失败: 0x%08X' % (what, status & 0xFFFFFFFF))


def _open(mode):
    h = ctypes.c_void_p()
    _check(bcrypt.BCryptOpenAlgorithmProvider(
        ctypes.byref(h), BCRYPT_AES_ALGORITHM, None, 0), 'OpenAlgorithm')
    if mode is not None:
        mode_b = ctypes.create_unicode_buffer(mode)
        _check(bcrypt.BCryptSetProperty(
            h, BCRYPT_CHAINING_MODE, mode_b, ctypes.sizeof(mode_b), 0), 'SetChaining')
    return h


def _key(hAlg, keybytes):
    hKey = ctypes.c_void_p()
    _check(bcrypt.BCryptGenerateSymmetricKey(
        hAlg, ctypes.byref(hKey), None, 0, keybytes, len(keybytes), 0), 'GenKey')
    return hKey


def _close_alg(h):
    bcrypt.BCryptCloseAlgorithmProvider(h, 0)


def _close_key(h):
    bcrypt.BCryptDestroyKey(h)


def _ecb(key, data, decrypt=False):
    """裸ECB(不设链模式即默认ECB) — XTS的底层原语"""
    hAlg = _open(None)
    try:
        hKey = _key(hAlg, key)
        try:
            out = ctypes.create_string_buffer(len(data))
            done = wintypes.ULONG(0)
            fn = bcrypt.BCryptDecrypt if decrypt else bcrypt.BCryptEncrypt
            _check(fn(hKey, data, len(data), None, None, 0,
                      out, len(out), ctypes.byref(done), 0),
                   'ECB' + ('解密' if decrypt else '加密'))
            return out.raw[:done.value]
        finally:
            _close_key(hKey)
    finally:
        _close_alg(hAlg)


def _xts_tweaks(t_int, n):
    """GF(2^128)小端约定下的连续双倍 → n个128位整数"""
    MASK = (1 << 128) - 1
    out = []
    t = t_int
    for _ in range(n):
        out.append(t)
        t = (t << 1) & MASK
        if out[-1] >> 127:
            t ^= 0x87
    return out


def xts_crypt(key64, tweak16, data, decrypt=True):
    """AES-XTS(IEEE P1619, 密钥=数据密钥‖tweak密钥) → bytes
    本机CNG的XTS-AES提供程序不可用(encrypt恒拒), 故用ECB手搓标准XTS:
    C_i = E_K1(P_i⊕T_i)⊕T_i, T序列=ECB_K2(tweak)的GF双倍链。"""
    if len(key64) not in (32, 64):
        raise ValueError('XTS密钥须32/64字节')
    if len(tweak16) != 16 or len(data) % 16:
        raise ValueError('tweak须16字节/数据须16字节对齐')
    half = len(key64) // 2
    k1, k2 = key64[:half], key64[half:]
    t0 = int.from_bytes(_ecb(k2, bytes(tweak16)), 'little')
    blocks = len(data) // 16
    ts = _xts_tweaks(t0, blocks)

    def xor_t(buf):
        out = bytearray(blocks * 16)
        for i in range(blocks):
            p = int.from_bytes(buf[i * 16:(i + 1) * 16], 'little') ^ ts[i]
            out[i * 16:(i + 1) * 16] = p.to_bytes(16, 'little')
        return bytes(out)

    if decrypt:
        # P = D_K1(C⊕T) ⊕ T
        mid = _ecb(k1, xor_t(data), decrypt=True)
        return xor_t(mid)
    # C = E_K1(P⊕T) ⊕ T
    mid = _ecb(k1, xor_t(data))
    return xor_t(mid)


def _xor(a, b):
    return bytes(x ^ y for x, y in zip(a, b))


def ccm_crypt(key, nonce12, mac16, data, decrypt=True):
    """AES-CCM (NIST SP 800-38C, 无AAD) — 本机CNG的CCM不可用, 基于ECB手搓
    decrypt: MAC不符返回None(密码错); encrypt: → (密文, tag)"""
    if len(nonce12) != 12 or len(mac16) != 16:
        raise ValueError('CCM nonce须12字节/mac须16字节')
    if len(key) not in (16, 24, 32):
        raise ValueError('CCM密钥须16/24/32字节')
    q = 3                                     # 15 - nonce12
    t = len(mac16)
    flags_mac = ((t - 2) // 2 << 3) | (q - 1)
    flags_ctr = q - 1
    s0 = _ecb(key, bytes([flags_ctr]) + nonce12 + (0).to_bytes(q, 'big'))

    def cbc_mac(payload_len, payload):
        b0 = bytes([flags_mac]) + nonce12 + payload_len.to_bytes(q, 'big')
        m = b0 + payload
        m += b'\x00' * (-len(m) % 16)
        y = bytes(16)
        for i in range(0, len(m), 16):
            y = _ecb(key, _xor(y, m[i:i + 16]))
        return _xor(y[:t], s0[:t])

    def ctr_stream(length):
        out = bytearray()
        ctr = 1
        while len(out) < length:
            out += _ecb(key, bytes([flags_ctr]) + nonce12 + ctr.to_bytes(q, 'big'))
            ctr += 1
        return bytes(out[:length])

    if decrypt:
        # 先CTR解回明文, CBC-MAC按明文计算, 比对tag
        pt = _xor(data, ctr_stream(len(data)))
        tag = cbc_mac(len(pt), pt)
        if tag != bytes(mac16):
            return None
        return pt
    tag = cbc_mac(len(data), data)
    return _xor(data, ctr_stream(len(data))), tag
