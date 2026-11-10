# -*- coding: utf-8 -*-
"""稳定性压力审计 — 一次性脚本(不进run_all, 产出审计结论):
S1 并发任务×5  S2 双实例共享jobs/竞态  S3 取消中断一致性
S4 服务中途被杀→重启→任务状态  S5 截断/损坏输入过全部解析器  S6 中文+空格+深路径
"""
import json
import os
import struct
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, 'engine'))

PY = ['py', '-3'] if os.system('py -3 -c "1" >nul 2>&1') == 0 else [sys.executable]


def free_port():
    import socket
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


PORT = free_port()                  # 动态端口: 根治残留进程/TIME_WAIT导致的固定端口冲突
BASE = 'http://127.0.0.1:%d' % PORT
TMP = tempfile.mkdtemp(prefix='rw_stress_')
FAILS = []


def check(name, ok, detail=''):
    print('%s %s %s' % ('PASS' if ok else 'FAIL', name, detail if not ok else ''), flush=True)
    if not ok:
        FAILS.append(name)


def api(path, method='GET', body=None, timeout=60):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode()), r.status
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode()), e.code
        except Exception:
            return {}, e.code


def start_server(port=PORT):
    return subprocess.Popen(PY + ['-I', os.path.join(ROOT, 'server.py'), str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_ready(port=PORT, tries=40):
    for _ in range(tries):
        time.sleep(0.5)
        try:
            with urllib.request.urlopen('http://127.0.0.1:%d/api/version' % port, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
    return False


def wait_terminal(jid, timeout=90):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j, _ = api('/api/job/' + jid)
        if j.get('state') in ('done', 'failed', 'cancelled'):
            return j
        time.sleep(0.5)
    return j


def make_probe_targets(n=5):
    out = []
    for i in range(n):
        p = os.path.join(TMP, 'target%d.bin' % i)
        with open(p, 'wb') as w:
            w.write(bytes((j * 31 + i) % 256 for j in range(1024 * 1024)))
        out.append(p)
    return out


def S1_concurrent():
    """5个probe并发: 全部done, 结果互不串"""
    targets = make_probe_targets(5)
    jids = []
    for p in targets:
        j, c = api('/api/job', 'POST', {'kind': 'probe', 'params': {'source': p}})
        assert c == 200, j
        jids.append(j['id'])
    results = [wait_terminal(j) for j in jids]
    ok = all(r.get('state') == 'done' for r in results)
    # 结果正确性: 每个文件pattern不同→都应stable
    ok = ok and all((r.get('result') or {}).get('stable') is True for r in results)
    det = [(r.get('state'), (r.get('error') or '')[:60]) for r in results if r.get('state') != 'done']
    check('S1 并发5任务不串结果', ok, str(det))


def S2_two_instances():
    """双实例同jobs目录: 两边各起任务并发写 → jobstore无损坏(list_jobs全可解析)"""
    p2 = free_port()
    srv2 = start_server(p2)
    try:
        assert wait_ready(p2), 'second server not ready'
        ja, _ = api('/api/job', 'POST', {'kind': 'probe', 'params': {'source': make_probe_targets(1)[0]}})
        import urllib.request as u2
        req = u2.Request('http://127.0.0.1:%d/api/job' % p2, data=json.dumps(
            {'kind': 'hash', 'params': {'source': os.path.join(ROOT, 'README.md')}}).encode(),
            headers={'Content-Type': 'application/json'}, method='POST')
        jb = json.loads(u2.urlopen(req, timeout=10).read().decode())
        ra = wait_terminal(ja['id'])
        t0 = time.time()
        while time.time() - t0 < 30:
            with u2.urlopen('http://127.0.0.1:%d/api/job/%s' % (p2, jb['id']), timeout=10) as r:
                rb = json.loads(r.read().decode())
            if rb.get('state') in ('done', 'failed'):
                break
            time.sleep(0.5)
        both = ra.get('state') == 'done' and rb.get('state') == 'done'
        # 双方list_jobs都能全量解析(无损坏JSON)
        lst1, c1 = api('/api/jobs', timeout=30)
        import urllib.request as u3
        with u3.urlopen('http://127.0.0.1:%d/api/jobs' % p2, timeout=30) as r:
            lst2 = json.loads(r.read().decode())
        parse_ok = c1 == 200 and lst1.get('jobs') is not None and lst2.get('jobs') is not None
        check('S2 双实例并发写jobstore', both and parse_ok,
              '%s/%s parse=%s' % (ra.get('state'), rb.get('state'), parse_ok))
    finally:
        srv2.terminate()
        try:
            srv2.wait(5)
        except Exception:
            srv2.kill()


def S3_cancel():
    """长任务中途取消: 状态=cancelled, 服务存活, 再起新任务正常"""
    # 用h264carve扫一个大稀疏文件(会跑一阵)
    big = os.path.join(TMP, 'big.img')
    with open(big, 'wb') as w:
        w.truncate(64 * 1024 * 1024)
        w.seek(0)
        w.write(os.urandom(1024))     # 随机头, 扫描要走一会
    j, _ = api('/api/job', 'POST', {'kind': 'h264carve',
                                    'params': {'source': big, 'out': os.path.join(TMP, 'h264_out')}})
    time.sleep(2)
    api('/api/job/%s/cancel' % j['id'], 'POST')
    r = wait_terminal(j['id'], 20)
    alive = api('/api/version')[1] == 200
    j2, c2 = api('/api/job', 'POST', {'kind': 'hash', 'params': {'source': os.path.join(ROOT, 'LICENSE')}})
    r2 = wait_terminal(j2['id'], 30)
    check('S3 取消后服务健康', r.get('state') in ('cancelled', 'done') and alive
          and r2.get('state') == 'done', '%s alive=%s next=%s' % (r.get('state'), alive, r2.get('state')))


def S4_kill_restart():
    """服务中途被杀→重启: job文件可读, 卡running的任务被标记, 不再假装活着"""
    tgt = make_probe_targets(1)[0]
    j, _ = api('/api/job', 'POST', {'kind': 'h264carve',
                                    'params': {'source': os.path.join(TMP, 'big.img'), 'out': os.path.join(TMP, 'h2')}})
    time.sleep(1.5)
    # 杀掉主服务(本函数调用方持有srv引用——这里通过返回提示外层处理)
    return j['id']


def S5_malformed():
    """截断/损坏输入过解析器: 不炸服务, 任务failed或done(空结果)"""
    import test_content as tc
    import test_dav
    import test_h264carve as th
    # 造完好样本再截断
    evtx = os.path.join(TMP, 't.evtx')
    tc.build_evtx(evtx, [(tc.ft_from(__import__('datetime').datetime(2026, 1, 1, tzinfo=__import__('datetime').timezone.utc)), 4624)])
    dav = os.path.join(TMP, 't.dav')
    test_dav.build_dav(dav, th.make_stream(frames=5))
    vhd = os.path.join(ROOT, 'tests', 'fixtures', 'exfat.vhd')
    cases = []
    for name, src in (('evtx', evtx), ('dav', dav), ('vhd', vhd)):
        if not os.path.exists(src):
            continue
        data = open(src, 'rb').read()
        for cut in (0, 64, 1024, len(data) // 2):
            p = os.path.join(TMP, '%s_cut_%d.bin' % (name, cut))
            with open(p, 'wb') as w:
                w.write(data[:cut])
            cases.append(p)
    bad_states = []
    for p in cases:
        for kind, params in (('evlogscan', {'dir': os.path.dirname(p)}),
                             ('davstrip', {'source': p, 'out': os.path.join(TMP, 'o')}),
                             ('findpart', None),
                             ('vmdetect', None)):
            if kind == 'findpart':
                j, c = api('/api/findpart?src=' + urllib.request.quote(p, safe=''), 'GET')
                if c not in (200, 400):
                    bad_states.append((kind, p, c))
                continue
            if kind == 'vmdetect':
                j, c = api('/api/vmdetect?src=' + urllib.request.quote(p, safe=''), 'GET')
                if c not in (200, 400):
                    bad_states.append((kind, p, c))
                continue
            j, c = api('/api/job', 'POST', {'kind': kind, 'params': params})
            if c != 200:
                bad_states.append((kind, p, 'start%d' % c))
                continue
            r = wait_terminal(j['id'], 60)
            if r.get('state') not in ('done', 'failed'):
                bad_states.append((kind, p, r.get('state')))
        # 服务仍活
        if api('/api/version')[1] != 200:
            bad_states.append(('server-alive-after', p, 'dead'))
            break
    check('S5 截断/损坏输入×%d不炸服务' % len(cases), not bad_states, str(bad_states[:3]))


def S6_paths():
    """中文+空格+深层路径: 任务照常"""
    deep = os.path.join(TMP, '项目 文件夹', '深 层', '目录 甲')
    os.makedirs(deep, exist_ok=True)
    src = os.path.join(deep, '样本 文件.bin')
    with open(src, 'wb') as w:
        w.write(bytes((i * 13) % 256 for i in range(1024 * 1024)))
    out = os.path.join(deep, '输出 结果')
    j, c = api('/api/job', 'POST', {'kind': 'probe', 'params': {'source': src}})
    r = wait_terminal(j['id'], 60)
    j2, c2 = api('/api/job', 'POST', {'kind': 'sqlscan', 'params': {'dir': deep}})
    r2 = wait_terminal(j2['id'], 60)
    check('S6 中文/空格/深层路径', r.get('state') == 'done' and r2.get('state') == 'done',
          '%s %s / %s %s' % (r.get('state'), (r.get('error') or '')[:60],
                             r2.get('state'), (r2.get('error') or '')[:60]))


if __name__ == '__main__':
    srv = start_server()
    try:
        assert wait_ready(), 'server not ready'
        S1_concurrent()
        S2_two_instances()
        S3_cancel()
        jid = S4_kill_restart()
        srv.terminate()
        try:
            srv.wait(5)
        except Exception:
            srv.kill()
        time.sleep(1)
        srv = start_server()                 # 重启
        assert wait_ready(), 'server restart failed'
        j, c = api('/api/job/' + jid)
        # 重启后: 死任务应被识别为stale/failed而非永远running
        state = j.get('state') if c == 200 else '404'
        check('S4 被杀任务重启后状态', state != 'running', 'state=%s' % state)
        S5_malformed()
        S6_paths()
    finally:
        srv.terminate()
        try:
            srv.wait(5)
        except Exception:
            srv.kill()
        shutil.rmtree(TMP, ignore_errors=True)
    print('\n====== 压力审计: %s ======' % ('全绿' if not FAILS else '失败: %s' % FAILS))
    sys.exit(1 if FAILS else 0)
