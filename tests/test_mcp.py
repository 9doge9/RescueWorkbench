# -*- coding: utf-8 -*-
"""MCP服务器测试: 工具表完整性 + 对真实工作台API的端到端调用
先起临时server.py实例, 再走 mcp_server 的工具函数(不起stdio进程, 直接调逻辑)。
"""
import os
import sys
import json
import shutil
import struct
import tempfile
import subprocess
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'engine'))

# LibreOffice python自spawn会WinError5 → 子进程统一走py启动器
PY = ['py', '-3'] if os.system('py -3 -c "1" >nul 2>&1') == 0 else [sys.executable]

import mcp_server
import schema

FAILS = []


def check(name, ok, detail=''):
    print('%s %s %s' % ('PASS' if ok else 'FAIL', name, detail if not ok else ''), flush=True)
    if not ok:
        FAILS.append(name)


def test_tools():
    tools = mcp_server.build_tools()
    names = {t['name'] for t in tools}
    for kind in schema.KINDS:
        assert 'rw_' + kind in names, kind
    for helper in ('rw_schema', 'rw_devices', 'rw_harvest', 'rw_findpart', 'rw_job_status', 'rw_job_cancel'):
        assert helper in names, helper
    check('工具表覆盖全部任务类型+辅助工具', len(tools) == len(schema.KINDS) + 6, str(len(tools)))
    # inputSchema 合法性: 有required的必须有属性表
    for t in tools:
        sc = t['inputSchema']
        assert sc['type'] == 'object' and isinstance(sc.get('properties'), dict), t['name']
        for r in sc.get('required', []):
            assert r in sc['properties'], (t['name'], r)
    check('inputSchema结构合法', True)


def test_e2e(tmp):
    port = 8977
    srv = subprocess.Popen(PY + ['-I', os.path.join(ROOT, 'server.py'), str(port)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = 'http://127.0.0.1:%d' % port
    try:
        for _ in range(40):
            time.sleep(0.5)
            try:
                d, code = mcp_server._req(base, 'GET', '/api/version')
                if code == 200:
                    break
            except Exception:
                pass
        else:
            check('服务起来', False)
            return
        d, code = mcp_server._req(base, 'GET', '/api/version')
        check('API连通', code == 200 and d.get('version'), str(d)[:80])

        d = mcp_server.call_tool('rw_schema', {}, base)
        check('rw_schema', d.get('kinds') and d.get('workflow'), str(d)[:80])

        # 真实任务: probe 对小文件
        f = os.path.join(tmp, 'probe_target.bin')
        with open(f, 'wb') as w:
            w.write(bytes((i * 7 + 3) % 256 for i in range(512 * 1024)))
        j = mcp_server.call_tool('rw_probe', {'source': f, 'wait_seconds': 60}, base)
        check('rw_probe端到端', j.get('state') == 'done' and 'stable' in (j.get('result') or {}),
              str(j)[:120])
        # 参数错误路径
        bad = mcp_server.call_tool('rw_probe', {'source': f + '.nope'}, base)
        check('失败任务isError语义', bad.get('state') == 'failed', str(bad)[:100])
        # job_status
        sid = mcp_server.call_tool('rw_job_status', {'id': j['id']}, base)
        check('rw_job_status', sid.get('id') == j['id'])
        # 未知工具
        check('未知工具拒绝', 'error' in mcp_server.call_tool('rw_nope', {}, base))
    finally:
        srv.terminate()
        try:
            srv.wait(5)
        except Exception:
            srv.kill()


def test_stdio(tmp):
    """stdio协议烟雾: 子进程跑initialize+tools/list"""
    import threading
    p = subprocess.Popen(PY + ['-I', os.path.join(ROOT, 'mcp_server.py'), '8978'],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, encoding='utf-8')
    try:
        p.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                                  'params': {'protocolVersion': '2024-11-05', 'capabilities': {},
                                             'clientInfo': {'name': 'test'}}}) + '\n')
        p.stdin.write(json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}) + '\n')
        p.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}) + '\n')
        p.stdin.flush()
        r1 = json.loads(p.stdout.readline())
        r2 = json.loads(p.stdout.readline())
        check('MCP initialize', r1['result']['serverInfo']['name'] == 'rescueworkbench', str(r1)[:100])
        tools = r2['result']['tools']
        check('MCP tools/list', len(tools) >= 35, str(len(tools)))
    finally:
        p.terminate()
        try:
            p.wait(3)
        except Exception:
            p.kill()


if __name__ == '__main__':
    tmp = tempfile.mkdtemp()
    try:
        test_tools()
        test_e2e(tmp)
        test_stdio(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print('\n====== MCP结果: %s ======' % ('全绿' if not FAILS else '失败: %s' % FAILS))
    sys.exit(1 if FAILS else 0)
