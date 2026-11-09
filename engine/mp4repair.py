# -*- coding: utf-8 -*-
"""MP4视频修复 — 集成untrunc外部工具(GPL-2.0独立分发,子进程调用)
untrunc需要一份同设备/同参数的完好参考视频来学习编码参数
"""
import os
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
UNTRUNC = os.path.join(HERE, 'bin', 'untrunc.exe')


def is_available():
    return os.path.exists(UNTRUNC)


def repair(reference, corrupt, output=None, timeout=300):
    """修复损坏的MP4 → (成功?, 输出路径或错误信息)
    reference: 完好参考视频路径(同设备同参数)
    corrupt: 损坏视频路径
    output: 输出路径(默认corrupt同目录下 _fixed.mp4)
    """
    if not is_available():
        return False, 'untrunc.exe未找到'
    if not os.path.exists(reference):
        return False, '参考视频不存在: %s' % reference
    if not os.path.exists(corrupt):
        return False, '损坏视频不存在: %s' % corrupt
    if output is None:
        base, ext = os.path.splitext(corrupt)
        output = base + '_fixed' + ext
    try:
        r = subprocess.run(
            [UNTRUNC, '-n', reference, corrupt],
            capture_output=True, text=True,
            timeout=timeout,
            encoding='utf-8', errors='replace',
            cwd=os.path.dirname(UNTRUNC),
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if os.path.exists(output) and os.path.getsize(output) > 0:
            return True, output
        # untrunc可能输出到其他路径——检查其stdout找输出文件
        for line in (r.stdout or '').split('\n'):
            if 'writing' in line.lower() or 'output' in line.lower():
                for part in line.split():
                    if part.endswith('.mp4') and os.path.exists(part):
                        return True, part
        return False, (r.stderr or r.stdout or 'unknown error').strip()[:200]
    except subprocess.TimeoutExpired:
        return False, '修复超时(%ds)' % timeout
    except OSError as e:
        return False, str(e)


def find_corrupt_mp4s(directory, min_size=1024):
    """在目录中找可能损坏的MP4(存在但缺少moov atom)"""
    import struct
    candidates = []
    for fn in os.listdir(directory):
        if not fn.lower().endswith(('.mp4', '.mov', '.3gp')):
            continue
        p = os.path.join(directory, fn)
        if not os.path.isfile(p) or os.path.getsize(p) < min_size:
            continue
        # 读头部检查ftyp和moov
        with open(p, 'rb') as f:
            head = f.read(8 * 1024 * 1024)  # 前8MB通常包含moov(前置)或不含(后置)
        has_ftyp = b'ftyp' in head[:64]
        has_moov = b'moov' in head
        if has_ftyp and not has_moov:
            # moov可能在文件尾部——读尾部
            sz = os.path.getsize(p)
            with open(p, 'rb') as f:
                f.seek(max(0, sz - 1024 * 1024))
                tail = f.read()
            if b'moov' not in tail:
                candidates.append(p)
    return candidates
