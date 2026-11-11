# -*- coding: utf-8 -*-
"""任务持久化: 每个任务一个JSON文件, 支持断点信息与事件回放"""
import json
import os
import time
import uuid

JOBS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'jobs')


def _path(job_id):
    return os.path.join(JOBS_DIR, job_id + '.json')


def new_job(kind, params):
    os.makedirs(JOBS_DIR, exist_ok=True)
    job = {
        'id': uuid.uuid4().hex[:12],
        'kind': kind,
        'params': params,
        'state': 'pending',   # pending/running/done/failed/cancelled
        'created': time.time(),
        'updated': time.time(),
        'result': None,
        'error': None,
    }
    save(job)
    return job


def save(job):
    """原子写: 临时文件+os.replace; 杀软/索引器瞬时锁文件时重试(压力审计实测WinError 5)"""
    job['updated'] = time.time()
    fp = _path(job['id'])
    tmp = fp + '.tmp'
    last = None
    for attempt in range(3):
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(job, f, ensure_ascii=False, indent=1, default=str)
                f.flush()
                os.fsync(f.fileno())       # NTFS断电安全: 数据先落盘再换名(与mapfile同款)
            os.replace(tmp, fp)
            return
        except PermissionError as e:      # AV/索引器瞬时占用
            last = e
            time.sleep(0.05 * (attempt + 1))
    raise last


def load(job_id):
    try:
        with open(_path(job_id), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def list_jobs(limit=None):
    if not os.path.isdir(JOBS_DIR):
        return []
    out = []
    for fn in os.listdir(JOBS_DIR):
        if fn.endswith('.json'):
            j = load(fn[:-5])
            if j:
                out.append(j)
    out.sort(key=lambda x: -x['created'])
    return out[:limit] if limit else out


def mark_stale_jobs(reason='服务重启中断(可续传任务请重跑同参数)'):
    """服务启动时清扫: 上个进程死掉时卡在running/pending的任务 → failed
    不清扫的话UI/API会永远显示"运行中"(假活)。"""
    n = 0
    for j in list_jobs():
        if j.get('state') in ('running', 'pending'):
            j['state'] = 'failed'
            j['error'] = reason
            save(j)
            n += 1
    return n
