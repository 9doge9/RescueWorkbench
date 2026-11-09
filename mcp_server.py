# -*- coding: utf-8 -*-
"""RescueWorkbench MCP 服务器 — 把数据抢救工作台暴露成AI Agent可调用的工具
零依赖(纯标准库), stdio传输, JSON-RPC 2.0(行分隔)。

用法(需先启动数据抢救工作台, 记下端口, run.bat默认8765):
    python mcp_server.py            # 连 http://127.0.0.1:8765
    python mcp_server.py 8799       # 指定端口
    set RW_API=http://127.0.0.1:8799 && python mcp_server.py

AI客户端配置(Claude Desktop / Cursor / ZCode等, 示例):
    {"mcpServers": {"rescueworkbench": {
        "command": "python",
        "args": ["C:/RescueWorkbench/mcp_server.py", "8765"]}}}

工具: 每个恢复通道一个工具(自动生成自engine/schema), 另有
    rw_schema / rw_devices / rw_harvest / rw_findpart / rw_job_status / rw_job_cancel
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, 'engine'))
import schema  # noqa: E402
import version  # noqa: E402

DEFAULT_BASE = os.environ.get('RW_API', 'http://127.0.0.1:8765')
RESULT_CAP = 60 * 1024          # 单个工具结果文本上限(字符)
DEFAULT_WAIT = 180              # rw_<kind>默认等待秒数; 超时返回job id供轮询
POLL_INTERVAL = 1.0

# ---------- HTTP ----------


def _req(base, method, path, body=None):
    url = base.rstrip('/') + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode('utf-8')), r.status
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode('utf-8')), e.code
        except Exception:
            return {'error': str(e)}, e.code
    except (urllib.error.URLError, OSError) as e:
        return {'error': '连不上工作台(%s): %s — 先启动RescueWorkbench' % (base, e)}, -1


def start_job(base, kind, params):
    return _req(base, 'POST', '/api/job', {'kind': kind, 'params': params})


def job_status(base, jid):
    return _req(base, 'GET', '/api/job/' + jid)


def wait_job(base, jid, wait):
    """轮询到终态或超时 → job dict"""
    deadline = time.time() + max(0, wait)
    while True:
        j, code = job_status(base, jid)
        if code != 200 or j.get('state') in ('done', 'failed', 'cancelled'):
            return j
        if time.time() >= deadline:
            j['_note'] = '仍在运行(镜像类任务可达数小时): 用 rw_job_status 查询, rw_job_cancel 取消'
            return j
        time.sleep(POLL_INTERVAL)


# ---------- 工具表(自动生成) ----------

def build_tools():
    tools = []
    for kind, v in schema.KINDS.items():
        props = {pk: {'type': pv[0] if pv[0] != 'integer' else 'integer',
                      'description': pv[2]}
                 for pk, pv in v['params'].items()}
        req = [pk for pk, pv in v['params'].items() if pv[1]]
        tools.append({
            'name': 'rw_' + kind,
            'description': '[%s] %s → %s' % (v['name'], v['desc'], v['returns']),
            'inputSchema': {'type': 'object', 'properties': props,
                            'required': req,
                            **({'additionalProperties': False} if req else {})},
        })
    tools += [
        {'name': 'rw_schema', 'description': '获取完整能力自描述(全部任务/端点/场景决策表/安全边界)',
         'inputSchema': {'type': 'object', 'properties': {}}},
        {'name': 'rw_devices', 'description': '列出本机磁盘设备(路径/容量/分区)',
         'inputSchema': {'type': 'object', 'properties': {}}},
        {'name': 'rw_harvest', 'description': '盘点全部任务的输出(目录/文件数/大小)',
         'inputSchema': {'type': 'object', 'properties': {}}},
        {'name': 'rw_findpart', 'description': '轻量分区定位',
         'inputSchema': {'type': 'object', 'properties': {
             'src': {'type': 'string', 'description': '镜像或设备路径'},
             'want': {'type': 'string', 'description': "想要的文件系统: exfat/ext/ntfs/bitlocker... 缺省列出全部"}},
             'required': ['src']}},
        {'name': 'rw_job_status', 'description': '查任务状态(长任务轮询用)',
         'inputSchema': {'type': 'object', 'properties': {
             'id': {'type': 'string', 'description': '任务id'}}, 'required': ['id']}},
        {'name': 'rw_job_cancel', 'description': '取消任务',
         'inputSchema': {'type': 'object', 'properties': {
             'id': {'type': 'string', 'description': '任务id'}}, 'required': ['id']}},
    ]
    return tools


def call_tool(name, args, base):
    args = args or {}
    if name == 'rw_schema':
        d, _ = _req(base, 'GET', '/api/schema')
        return d
    if name == 'rw_devices':
        return _req(base, 'GET', '/api/devices')[0]
    if name == 'rw_harvest':
        return _req(base, 'GET', '/api/harvest')[0]
    if name == 'rw_findpart':
        q = '?src=' + urllib.request.quote(args.get('src', ''), safe='')
        if args.get('want'):
            q += '&want=' + urllib.request.quote(args['want'], safe='')
        return _req(base, 'GET', '/api/findpart' + q)[0]
    if name == 'rw_job_status':
        return job_status(base, args['id'])[0]
    if name == 'rw_job_cancel':
        return _req(base, 'POST', '/api/job/%s/cancel' % args['id'], {})[0]
    if name.startswith('rw_'):
        kind = name[3:]
        if kind not in schema.KINDS:
            return {'error': '未知工具: ' + name}
        wait = args.pop('wait_seconds', DEFAULT_WAIT) or DEFAULT_WAIT
        params = {k: v for k, v in args.items() if v is not None}
        j, code = start_job(base, kind, params)
        if code != 200:
            return j
        return wait_job(base, j['id'], wait)
    return {'error': '未知工具: ' + name}


# ---------- MCP stdio 协议 ----------

def handle(msg, base):
    method = msg.get('method', '')
    mid = msg.get('id')
    if method == 'initialize':
        pv = (msg.get('params') or {}).get('protocolVersion', '2024-11-05')
        return {'jsonrpc': '2.0', 'id': mid, 'result': {
            'protocolVersion': pv,
            'capabilities': {'tools': {}},
            'serverInfo': {'name': 'rescueworkbench', 'version': version.VERSION},
        }}
    if method == 'tools/list':
        return {'jsonrpc': '2.0', 'id': mid, 'result': {'tools': build_tools()}}
    if method == 'tools/call':
        p = msg.get('params') or {}
        try:
            out = call_tool(p.get('name'), p.get('arguments'), base)
            ok = isinstance(out, dict) and out.get('state') == 'failed'
            text = json.dumps(out, ensure_ascii=False)[:RESULT_CAP]
            return {'jsonrpc': '2.0', 'id': mid, 'result': {
                'content': [{'type': 'text', 'text': text}], 'isError': bool(ok)}}
        except Exception as e:
            return {'jsonrpc': '2.0', 'id': mid, 'result': {
                'content': [{'type': 'text', 'text': '工具执行异常: %s' % e}], 'isError': True}}
    if method == 'ping':
        return {'jsonrpc': '2.0', 'id': mid, 'result': {}}
    if mid is not None:
        return {'jsonrpc': '2.0', 'id': mid,
                'error': {'code': -32601, 'message': 'method not found: ' + method}}
    return None                      # 通知类: 不回


def main():
    base = DEFAULT_BASE
    if len(sys.argv) > 1:
        base = 'http://127.0.0.1:' + sys.argv[1]
    try:                                    # Windows管道默认GBK, MCP规定UTF-8
        sys.stdin.reconfigure(encoding='utf-8')
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    sys.stderr.write('[rescueworkbench-mcp] base=%s\n' % base)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        resp = handle(msg, base)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + '\n')
            sys.stdout.flush()


if __name__ == '__main__':
    main()
