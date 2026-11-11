# -*- coding: utf-8 -*-
"""跨Windows文件名坑的统一守卫
坑源(微软官方Naming Files文档 + 实测):
  1. 保留设备名: CON PRN AUX NUL COM1-9 LPT1-9(含上标变体) — 带扩展名也拦(CON.txt也炸)
  2. 尾部点/空格被Win32静默剥离 — "name." 与 "name" 撞名, 也造成安全旁路
  3. 非法字符与控制字符
恢复场景里被恢复的文件完全可能就叫 CON.txt 或 "报表." — 落盘前必须过这一关。
"""
import os

RESERVED = {'CON', 'PRN', 'AUX', 'NUL'} | {
    '%s%d' % (p, n) for p in ('COM', 'LPT') for n in range(1, 10)} | {
    'COM¹', 'COM²', 'COM³', 'LPT¹', 'LPT²', 'LPT³'}
ILLEGAL = '\\/:*?"<>|'


def safe_name(name, fallback='recovered', maxlen=120):
    """源盘里挖出的文件名 → 可安全落在NTFS/FAT的名字
    处理: 控制字符/非法字符→删, 尾部点空格→剥, 保留名→加前缀_, 空→fallback"""
    s = ''.join(c for c in str(name) if c not in ILLEGAL and ord(c) >= 32)[:maxlen]
    # 尾部点与空格: Win32会静默剥离导致撞名/旁路, 提前剥掉
    s = s.rstrip('. ')
    if not s:
        return fallback
    stem = s.split('.')[0]
    if stem.upper() in RESERVED:
        s = '_' + s                      # _CON.txt 合法且肉眼可认
    return s


def safe_base(src_path, fallback='recovered'):
    """取文件主名(去扩展)并过安全命名 — 用于生成输出文件名"""
    return safe_name(os.path.splitext(os.path.basename(src_path))[0], fallback)
