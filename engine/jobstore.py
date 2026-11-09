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
    job['updated'] = time.time()
    with open(_path(job['id']), 'w', encoding='utf-8') as f:
        json.dump(job, f, ensure_ascii=False, indent=1, default=str)


def load(job_id):
    try:
        with open(_path(job_id), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def list_jobs():
    if not os.path.isdir(JOBS_DIR):
        return []
    out = []
    for fn in os.listdir(JOBS_DIR):
        if fn.endswith('.json'):
            j = load(fn[:-5])
            if j:
                out.append(j)
    return sorted(out, key=lambda x: -x['created'])
