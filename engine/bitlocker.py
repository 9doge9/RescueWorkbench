# -*- coding: utf-8 -*-
"""BitLocker解锁 — FVE元数据解析 + 密码/恢复口令/清除密钥 → AES-XTS解密镜像
格式常量经dislocker源码+libbde格式文档双重校准(2026-11):
  卷头: '-FVE-FS-'@3, sector_size@0xB, 元数据偏移information_off[3]@0xB0
  元数据块: '-FVE-FS-'(8) size@8(×16当version=2) version@0xA
            encrypted_volume_size@0x10 dataset@0x40 数据条目@0x70
  datum: size u16, entry u16(2=VMK 3=FVEK 6=启动密钥), value u16(1=KEY
         3=STRETCH 5=AES_CCM 8=VMK 15=虚拟化), err u16
  cipher: 0x8004/0x8005=Xts-AES-128/256; VMK保护域=nonce[10:12]小端u16
  拉伸(Kornblum链): 结构=last[32]+initial[32]+salt[16]+count u64(88B打包),
         initial=SHA256(SHA256(UTF16LE(密码))) 或 SHA256(128位恢复中间键),
         循环0x100000次: last=SHA256(结构), count++
  恢复口令: 48位数字8组×6, 每组÷11 → u16(小端拼128位) — 组不整除11即非法
诚实边界: 格式与算法按上述权威源实现; 与真实加密盘的兼容性以实测为准
(合成盘构造器在tests/内, 自建自解验证全链路逻辑)。
"""
import struct
import hashlib

import cng

SIG = b'-FVE-FS-'

ENTRY_VMK = 2
ENTRY_FVEK = 3
ENTRY_STARTUP_KEY = 6

VALUE_KEY = 1
VALUE_STRETCH = 3
VALUE_AES_CCM = 5
VALUE_VMK = 8
VALUE_VIRTUALIZATION = 15

CIPHER_XTS128 = 0x8004
CIPHER_XTS256 = 0x8005

HDR_KEY = 8 + 4                  # datum头8B + KEY的algo/padd 4B
HDR_STRETCH = 8 + 4 + 16         # + salt16
HDR_AESCCM = 8 + 12 + 16         # + nonce12 + mac16
HDR_VMK = 8 + 16 + 12            # + guid16 + nonce12


def parse_volume_header(f, base):
    f.seek(base)
    vh = f.read(512)
    if len(vh) < 512 or vh[3:11] != SIG:
        return None
    out = {
        'sector_size': struct.unpack_from('<H', vh, 0xB)[0] or 512,
        'info_off': [struct.unpack_from('<Q', vh, 0xB0 + 8 * i)[0] for i in range(3)],
        'boot_backup': struct.unpack_from('<Q', vh, 0x38)[0],
    }
    return out


def parse_metadata_block(f, base, off):
    """一个元数据块 → dict或None(按dataset size截取)"""
    f.seek(base + off)
    blk = f.read(0x10000)
    if len(blk) < 0x70 or blk[:8] != SIG:
        return None
    size = struct.unpack_from('<H', blk, 8)[0]
    version = struct.unpack_from('<H', blk, 0xA)[0]
    if version >= 2:
        size *= 16
    blk = blk[:min(size or 0x10000, len(blk))]
    out = {
        'offset': off, 'version': version,
        'curr_state': struct.unpack_from('<H', blk, 0xC)[0],
        'next_state': struct.unpack_from('<H', blk, 0xE)[0],
        'volume_size': struct.unpack_from('<Q', blk, 0x10)[0],
        'algorithm': struct.unpack_from('<H', blk, 0x40 + 0x24)[0],
        'datums': b'', 'next_counter': struct.unpack_from('<I', blk, 0x40 + 0x20)[0],
    }
    ds_size = struct.unpack_from('<I', blk, 0x40)[0]
    end = min(0x40 + (ds_size or 0), len(blk))
    out['datums'] = blk[0x70:end]
    return out


def iter_datums(buf, off=0, end=None):
    """顺序遍datum → (绝对off, entry, value, 全部字节)"""
    end = len(buf) if end is None else end
    while off + 8 <= end:
        dsize, entry, value = struct.unpack_from('<HHH', buf, off)
        if dsize < 8 or off + dsize > end:
            return
        yield off, entry, value, buf[off:off + dsize]
        off += dsize


def _nested(datum_bytes, hdr_len):
    """datum负载里的嵌套datum遍历"""
    yield from iter_datums(datum_bytes[hdr_len:])


def load(source, part_lba=0):
    """读卷 → {'vh', 'blocks'(有效副本), 'vmks': [...], 'fvek_datum': raw}"""
    with open(source, 'rb') as f:
        vh = parse_volume_header(f, part_lba)
        if not vh:
            return {'ok': False, 'error': '非BitLocker卷'}
        blocks = []
        for off in vh['info_off']:
            if not off:
                continue
            b = parse_metadata_block(f, part_lba, off)
            if b:
                b['end'] = off + (b['datums'] and 0x10000 or 0)
                b['size'] = len(b['datums']) and 0x10000
                blocks.append(b)
        if not blocks:
            return {'ok': False, 'error': '元数据块损坏'}
        # 取next_counter最大的副本(最新)
        blocks.sort(key=lambda b: b['next_counter'], reverse=True)
        blk = blocks[0]
        vmks = []
        fvek_datum = None
        virt = None
        for _, entry, value, raw in iter_datums(blk['datums']):
            if entry == ENTRY_VMK and value == VALUE_VMK:
                vmks.append(raw)
            elif entry == ENTRY_FVEK and value == VALUE_AES_CCM:
                fvek_datum = raw
            elif entry in (1, 5) and value == VALUE_VIRTUALIZATION:
                virt = raw
        protectors = []
        for raw in vmks:
            protectors.append(_describe_protector(raw))
        out = {
            'ok': True, 'vh': vh, 'block': blk,
            'volume_size': blk['volume_size'], 'algorithm': blk['algorithm'],
            'sector_size': vh['sector_size'],
            'version': blk['version'], 'curr_state': blk['curr_state'],
            'protectors': protectors, 'fvek_datum': fvek_datum,
            'vmks': vmks, 'virtualization': virt,
        }
        return out


def _describe_protector(raw):
    """VMK条目 → {'range', 'kind', 'raw'}"""
    nonce = raw[HDR_VMK - 12:HDR_VMK]
    rng = struct.unpack_from('<H', nonce, 10)[0]
    kind = {0x0000: '清除密钥', 0x0100: 'TPM', 0x0200: 'TPM+PIN',
            0x0400: '启动密钥(.BEK)', 0x0500: 'TPM+启动密钥',
            0x0800: '恢复口令', 0x2000: '用户密码'}.get(
        rng & 0xFF00, '未知(0x%04x)' % rng)
    has_clear = False
    for _, _, v, r in _nested(raw, HDR_VMK):
        if v == VALUE_KEY:
            has_clear = True
    return {'range': rng, 'kind': kind, 'clear_key': has_clear, 'raw': raw}


# ---------- 密钥拉伸 ----------

def stretch(initial_sha256, salt):
    """Kornblum链式拉伸 ×0x100000轮 → 32字节"""
    buf = bytearray(88)
    buf[32:64] = initial_sha256
    buf[64:80] = salt
    # last=0, count=0
    sha = hashlib.sha256
    for i in range(0x100000):
        buf[0:32] = sha(bytes(buf)).digest()
        struct.pack_into('<Q', buf, 80, i + 1)
    return bytes(buf[0:32])


def password_intermediate(password):
    return hashlib.sha256(hashlib.sha256(
        password.encode('utf-16-le')).digest()).digest()


def recovery_intermediate(recovery_password):
    """48位数字(允许连字符) → 8组×(组值÷11)=u16小端 → SHA256"""
    digits = ''.join(c for c in recovery_password if c.isdigit())
    if len(digits) != 48:
        raise ValueError('恢复口令须48位数字')
    key = bytearray(16)
    for g in range(8):
        group = int(digits[g * 6:(g + 1) * 6])
        if group % 11:
            raise ValueError('第%d组校验失败(须能被11整除)' % (g + 1))
        struct.pack_into('<H', key, g * 2, group // 11)
    return hashlib.sha256(bytes(key)).digest(), bytes(key)


# ---------- 解锁 ----------

def _ccm_datums(vmk_raw):
    """VMK条目 → (stretch_datum或None, ccm_datum, key_datum或None)"""
    stretch = ccm = keyd = None
    for _, _, v, r in _nested(vmk_raw, HDR_VMK):
        if v == VALUE_STRETCH:
            stretch = r
        elif v == VALUE_AES_CCM:
            ccm = r
        elif v == VALUE_KEY:
            keyd = r
    return stretch, ccm, keyd


def _decrypt_aes_ccm(ccm_raw, key):
    """AES_CCM datum → 解密payload(KEY datum); 认证失败None"""
    nonce = ccm_raw[8:20]
    mac = ccm_raw[20:36]
    payload = ccm_raw[36:]
    return cng.ccm_crypt(key, nonce, mac, payload, decrypt=True)


def _key_from_datum(key_raw):
    """KEY datum → (algo u16, key bytes)"""
    algo = struct.unpack_from('<H', key_raw, 8)[0]
    return algo, key_raw[HDR_KEY:]


def unlock(source, part_lba, password=None, recovery=None):
    """解锁 → {'ok','vmk','fvek','algo','fvek_key'}"""
    info = load(source, part_lba)
    if not info.get('ok'):
        return {'ok': False, 'error': info.get('error')}
    candidates = []
    if password is not None:
        candidates.append(('密码', password_intermediate(password)))
    if recovery is not None:
        inter, _ = recovery_intermediate(recovery)
        candidates.append(('恢复口令', inter))
    # 清除密钥: 直接嵌套KEY datum
    for prot in info['protectors']:
        _, _, keyd = _ccm_datums(prot['raw'])
        if keyd:
            candidates.append(('清除密钥(明文VMK)', None))
            break
    for prot in info['protectors']:
        stretch_raw, ccm_raw, keyd = _ccm_datums(prot['raw'])
        if not ccm_raw:
            continue
        if keyd:
            # 明文VMK
            vmk_algo, vmk_key = _key_from_datum(keyd)
            return _finish_unlock(info, vmk_key, '清除密钥')
        if not stretch_raw:
            continue
        salt = stretch_raw[HDR_STRETCH - 16:HDR_STRETCH]
        for label, inter in candidates:
            if inter is None:
                continue
            key = stretch(inter, salt)
            dec = _decrypt_aes_ccm(ccm_raw, key)
            if dec and len(dec) >= HDR_KEY and \
                    struct.unpack_from('<H', dec, 4)[0] == VALUE_KEY:
                vmk_algo, vmk_key = _key_from_datum(dec)
                return _finish_unlock(info, vmk_key, label)
    return {'ok': False, 'error': '无法解锁: 密码/恢复口令不匹配, 且无清除密钥'}


def _finish_unlock(info, vmk_key, via):
    fvek_datum = info.get('fvek_datum')
    if not fvek_datum:
        return {'ok': False, 'error': 'FVEK条目缺失'}
    dec = _decrypt_aes_ccm(fvek_datum, vmk_key)
    if not dec or len(dec) < HDR_KEY:
        return {'ok': False, 'error': 'FVEK解密失败(VMK不正确?)'}
    algo, fvek_key = _key_from_datum(dec)
    return {'ok': True, 'via': via, 'vmk': vmk_key.hex(),
            'algo': algo, 'fvek_key': fvek_key,
            'volume_size': info['volume_size'],
            'sector_size': info['sector_size']}


# ---------- 解密镜像 ----------

def decrypt_range(source, part_lba, fvek_key, algo, out_path,
                  on_progress=None, cancel=None):
    """整卷解密 → out_path
    明文区: 扇区0(卷头)+三处元数据块(64KB)原样保留;
    卷头0x38指向的原引导扇区备份解密后放扇区0(其余扇区按自身扇区号解密)"""
    with open(source, 'rb') as f:
        import os
        vh = parse_volume_header(f, part_lba)
        size = os.path.getsize(source) - part_lba
        ss = vh['sector_size']
        meta_zones = []
        for off in vh['info_off']:
            if off:
                meta_zones.append((off, off + 0x10000))
        total = size // ss
        xts_key = fvek_key if len(fvek_key) in (32, 64) else fvek_key[:64]
        boot_backup = vh['boot_backup']
        out = open(out_path, 'wb')
        try:
            CH = 4 * 1024 * 1024              # 4MB批
            pos = 0
            while pos < size:
                if cancel is not None and cancel.is_set():
                    return None
                f.seek(part_lba + pos)
                data = f.read(CH)
                if not data:
                    break
                buf = bytearray(data)
                # 逐扇区: 明文区跳过
                nsec = len(data) // ss
                for i in range(nsec):
                    s = pos // ss + i
                    in_meta = any(m[0] <= s * ss < m[1] for m in meta_zones)
                    if in_meta:
                        continue
                    tweak = struct.pack('<QQ', s, 0)
                    buf[i * ss:(i + 1) * ss] = cng.xts_crypt(
                        xts_key, tweak, bytes(buf[i * ss:(i + 1) * ss]))
                # 扇区0: 用原引导扇区备份的密文解密放回
                if pos == 0 and boot_backup:
                    f.seek(part_lba + boot_backup)
                    bsec = f.read(ss)
                    if len(bsec) == ss:
                        buf[0:ss] = cng.xts_crypt(
                            xts_key, struct.pack('<QQ', boot_backup // ss, 0), bsec)
                out.write(buf)
                pos += len(data)
                if on_progress and pos % (64 * 1024 * 1024) < CH:
                    on_progress({'event': 'progress', 'bytes_done': pos,
                                 'bytes_total': size})
            return out_path
        finally:
            out.close()
