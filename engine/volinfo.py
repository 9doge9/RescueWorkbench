# -*- coding: utf-8 -*-
"""卷用量: ctypes查每盘符的 已用/总量/百分比 + 分区→物理盘映射"""
import ctypes
import subprocess
import json


def get_volumes():
    """{盘符: {total, free, used, pct}} — 纯ctypes无外部依赖"""
    k32 = ctypes.windll.kernel32
    out = {}
    for c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
        root = c + ':\\'
        t = ctypes.c_ulonglong(0)
        total = ctypes.c_ulonglong(0)
        free = ctypes.c_ulonglong(0)
        if k32.GetDiskFreeSpaceExW(ctypes.c_wchar_p(root),
                                   ctypes.byref(t), ctypes.byref(total), ctypes.byref(free)):
            if total.value > 0:
                out[c] = {
                    'total': total.value,
                    'free': free.value,
                    'used': total.value - free.value,
                    'pct': round((total.value - free.value) * 100.0 / total.value, 1)
                }
    return out


def get_disk_letters():
    """{盘符: 物理盘号} — PowerShell一次性查"""
    try:
        r = subprocess.run(
            ['powershell', '-NoProfile', '-Command',
             'Get-Partition | Where-Object {$_.DriveLetter} | '
             'Select DriveLetter,DiskNumber | ConvertTo-Json -Compress'],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        data = json.loads(r.stdout.strip())
        if isinstance(data, dict):
            data = [data]
        return {str(d['DriveLetter']): d['DiskNumber'] for d in data}
    except Exception:
        return {}


def get_usage_by_disk():
    """{物理盘号: {pct, used, total, volumes:[{letter,pct,used,total}]}}"""
    vols = get_volumes()
    dmap = get_disk_letters()
    result = {}
    for letter, v in vols.items():
        dn = dmap.get(letter)
        if dn is None:
            continue
        if dn not in result:
            result[dn] = {'volumes': [], 'used': 0, 'total': 0}
        result[dn]['volumes'].append({'letter': letter, **v})
        result[dn]['used'] += v['used']
        result[dn]['total'] += v['total']
    for dn, d in result.items():
        d['pct'] = round(d['used'] * 100.0 / d['total'], 1) if d['total'] else 0
    return result
