# -*- coding: utf-8 -*-
"""NTFS LZNT1解压缩 — 通过Windows内核API RtlDecompressBuffer
比手工实现更可靠(内核实现)且更快(无Python循环)
"""
import ctypes
import struct

ntdll = ctypes.windll.ntdll

COMPRESSION_FORMAT_LZNT1 = 2
STATUS_SUCCESS = 0
STATUS_INVALID_PARAMETER = 0xC000000D
STATUS_BAD_COMPRESSION_BUFFER = 0xC0000242
STATUS_UNSUPPORTED_COMPRESSION = 0xC000025F

# 函数签名
_ntdll_decompress = ntdll.RtlDecompressBuffer
_ntdll_decompress.argtypes = [
    ctypes.c_ushort,          # CompressionFormat
    ctypes.c_void_p,          # DestinationBuffer (PVOID)
    ctypes.c_ulong,           # DestinationBufferSize
    ctypes.c_void_p,          # SourceBuffer (PVOID)
    ctypes.c_ulong,           # SourceBufferSize
    ctypes.POINTER(ctypes.c_ulong)  # FinalUncompressedSize (PULONG)
]


def decompress(data, output_size=None):
    """LZNT1解压 → bytes或None(失败)
    data: 磁盘上读到的压缩数据(含chunk headers)
    output_size: 可选,期望的解压后大小
    """
    if not data:
        return b''
    # 给足够的输出空间(压缩比最大约16x)
    max_out = max(len(data) * 16, output_size or 0, 4096)
    dst = ctypes.create_string_buffer(max_out)
    final_size = ctypes.c_ulong()
    status = _ntdll_decompress(
        COMPRESSION_FORMAT_LZNT1,
        dst, len(dst),
        data, len(data),
        ctypes.byref(final_size))
    if status != STATUS_SUCCESS:
        return None
    result = dst.raw[:final_size.value]
    if output_size and len(result) < output_size:
        result += b'\x00' * (output_size - len(result))
    return result


def decompress_attribute(data, uncompressed_size, compression_unit_size=65536):
    """解压NTFS压缩属性的$DATA(按压缩单元切块)
    data: 磁盘上读到的原始字节(所有压缩单元拼接)
    uncompressed_size: 解压后的real size
    compression_unit_size: 压缩单元字节数(通常=16*cluster_size)
    """
    out = bytearray()
    pos = 0
    while pos < len(data) and len(out) < uncompressed_size:
        remaining = uncompressed_size - len(out)
        cu_data = data[pos:pos + compression_unit_size * 2]  # 给余量
        if not cu_data:
            break
        # 尝试整块解压(Windows API能处理多个chunk)
        result = decompress(cu_data, min(compression_unit_size, remaining))
        if result is None:
            # 不可解压——可能是未压缩的单元,直接拷贝
            take = min(compression_unit_size, remaining, len(data) - pos)
            out.extend(data[pos:pos + take])
            pos += take
        else:
            take = min(len(result), remaining)
            out.extend(result[:take])
            # 计算实际消耗的输入字节数
            # (压缩后的实际大小需要从header推算或直接跳到下一个单元)
            pos += compression_unit_size  # 简化: 按压缩单元大小跳
        if len(out) >= uncompressed_size:
            break
    return bytes(out[:uncompressed_size])


def is_compressed(data):
    """快速判断数据是否是LZNT1压缩格式"""
    if len(data) < 2:
        return False
    header = struct.unpack_from('<H', data, 0)[0]
    # 合法header: 低12位=块大小+1, 高位=压缩标志
    block_size = (header & 0x0FFF) + 1
    if block_size < 1 or block_size > 4097:
        return False
    # 检查是否所有高位标志位都合理
    if header & 0x4000 != 0:  # bit14 must be 0
        return False
    return True
