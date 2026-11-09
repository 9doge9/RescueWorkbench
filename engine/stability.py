# -*- coding: utf-8 -*-
"""读稳定性探测: 判断源盘是否"说谎"(读取成功但数据漂移)
两类探测:
1. 定点重复读: 同一位置读N次比对哈希 → 弱单元检测
2. 流式vs定点: 连续流读取与seek分块读取比对 → 主控漂移检测(aigo案例)
"""
import hashlib
import os

SAMPLE_POINTS = [0.05, 0.3, 0.55, 0.8, 0.95]


def open_source(path):
    """打开源(物理盘或镜像)。物理盘需扇区对齐读。"""
    return open(path, 'rb', buffering=0)


def read_at(f, off, n):
    f.seek(off)
    b = b''
    while len(b) < n:
        c = f.read(n - len(b))
        if not c:
            break
        b += c
    return b if len(b) == n else None


def sha(b):
    return hashlib.sha256(b).hexdigest()[:16]


def probe_repeat(f, size, rounds=2, sample_mb=1):
    """定点重复读探测。返回 (不稳定点数, 总点数, 详情)"""
    n = min(sample_mb * 1024 * 1024, size)          # 小文件/小分区: 采样块收缩, 防负偏移seek
    n -= n % 512
    if n <= 0:
        return 0, 0, []
    bad = 0
    details = []
    for pct in SAMPLE_POINTS:
        off = int(size * pct)
        off -= off % 512
        if off + n > size:
            off = size - n
        hashes = set()
        for _ in range(rounds):
            b = read_at(f, off, n)
            if b is None:
                hashes.add('READ_FAIL')
                break
            hashes.add(sha(b))
        unstable = len(hashes) > 1
        if unstable:
            bad += 1
        details.append({'offset': off, 'unstable': unstable})
    return bad, len(SAMPLE_POINTS), details


def probe_stream_vs_seek(f, size, start_frac=0.1, span_mb=256, chunk_mb=8):
    """流式连续读 vs 逐块seek读 比对。
    返回 (漂移检测, 详情)。漂移=True 表示此盘顺序流不可信, 必须用校验式镜像。"""
    span = min(span_mb * 1024 * 1024, size // 2)
    start = int(size * start_frac)
    start -= start % 512

    # 1) 单句柄流式读
    f.seek(start)
    stream = b''
    while len(stream) < span:
        c = f.read(span - len(stream))
        if not c:
            break
        stream += c
    if len(stream) < span:
        return None, {'error': 'stream_read_short'}

    # 2) 逐块seek读(新句柄)
    f2 = open_source(f.name)
    chunk = chunk_mb * 1024 * 1024
    seek_parts = []
    off = start
    while off < start + span:
        b = read_at(f2, off, min(chunk, start + span - off))
        if b is None:
            return None, {'error': 'seek_read_fail', 'offset': off}
        seek_parts.append(b)
        off += len(b)
    f2.close()

    drift = hashlib.sha256(stream).hexdigest() != hashlib.sha256(b''.join(seek_parts)).hexdigest()
    return drift, {'span': span, 'start': start, 'drift': drift}


def probe(source, size):
    """综合探测入口。返回裁决dict"""
    f = open_source(source)
    try:
        bad, total, details = probe_repeat(f, size)
        drift, drift_detail = probe_stream_vs_seek(f, size)
        verdict = {
            'repeat_unstable': bad,
            'repeat_points': total,
            'stream_drift': drift,
            'stable': (bad == 0 and drift is False),
            'recommend': 'verified_imager' if (drift or bad > 0) else 'fast_imager',
            'details': {'repeat': details, 'stream': drift_detail},
        }
        return verdict
    finally:
        f.close()


if __name__ == '__main__':
    import sys
    src = sys.argv[1]
    v = probe(src, os.path.getsize(src))
    import json
    print(json.dumps(v, ensure_ascii=False, indent=2))
