# -*- coding: utf-8 -*-
"""数据抢救工作台 本地服务: 标准库HTTP + SSE进度推送
用法: python server.py [端口]  (需管理员权限才能读物理盘)
"""
import json
import os
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, 'engine'))
import sfadapter
import pipeline
import jobstore
from mapfile import MapFile

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765

_devices_cache = {'t': 0, 'data': None}


def get_devices(force=False):
    now = time.time()
    if not force and _devices_cache['data'] and now - _devices_cache['t'] < 5:
        return _devices_cache['data']
    ev, rc = sfadapter.devices()
    _devices_cache['t'], _devices_cache['data'] = now, ev
    return ev


def get_devices_enriched():
    """设备列表 + 真实卷用量"""
    ev = get_devices(force=True)
    try:
        from volinfo import get_usage_by_disk
        usage = get_usage_by_disk()
    except Exception:
        usage = {}
    for d in ev.get('devices', []):
        import re
        m = re.search(r'PhysicalDrive(\d+)', d.get('path', ''))
        if m:
            dn = int(m.group(1))
            d['usage'] = usage.get(dn)
    return ev


def get_survey():
    """启动快扫: 对每盘只读分区表+各分区首扇区(sf-cli survey, 多线程)"""
    dev = get_devices()
    paths = [d['path'] for d in dev.get('devices', [])]
    if not paths:
        return {'results': [], 'errors': []}
    args = ['survey']
    for p in paths:
        args += ['--source', p]
    box = {'r': [], 'e': []}

    def on_ev(e):
        ev = e.get('event')
        if ev == 'survey_result':
            box['r'].append(e)
        elif ev == 'survey_error':
            box['e'].append(e)
        elif ev == 'error':
            box['e'].append({'source': '*', 'message': e.get('message', '未知错误')})
    sfadapter.run(args, on_event=on_ev)
    return {'results': box['r'], 'errors': box['e']}


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    # ---------- 基础 ----------
    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(b)

    def _static(self, path, ctype):
        try:
            with open(os.path.join(ROOT, 'web', path), 'rb') as f:
                b = f.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', ctype + ('; charset=utf-8' if ctype.startswith('text') else ''))
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        n = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(n) if n else b'{}'
        try:
            return json.loads(raw or b'{}')
        except ValueError:
            return {}

    # ---------- 路由 ----------
    def do_GET(self):
        p = self.path.split('?')[0]
        if p in ('/', '/index.html'):
            self._static('index.html', 'text/html')
        elif p.startswith('/app.js'):
            self._static('app.js', 'text/javascript')
        elif p.startswith('/virtual.js'):
            self._static('virtual.js', 'text/javascript')
        elif p.startswith('/style.css'):
            self._static('style.css', 'text/css')
        elif p.startswith('/themes.css'):
            self._static('themes.css', 'text/css')
        elif p == '/donate.jpg':
            self._static('donate.jpg', 'image/jpeg')
        elif p == '/api/devices':
            self._json(get_devices_enriched())
        elif p == '/api/survey':
            self._json(get_survey())
        elif p.startswith('/api/smart'):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            dn = int((q.get('disk') or ['0'])[0])
            import smartmon
            self._json(smartmon.query(dn))
        elif p.startswith('/api/preview'):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            if q.get('f'):
                _serve_img(self, q['f'][0])
            elif q.get('src') and q.get('lba') and q.get('id'):
                _serve_from_image(self, q['src'][0], q['lba'][0], q['id'][0])
            else:
                self._json({'error': 'bad args'}, 400)
        elif p.startswith('/api/mapfile'):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            mp = (q.get('path') or [''])[0]
            try:
                m = MapFile.load(mp)
                self._json({'ok': True, 'size': m.size,
                            'blocks': [{'pos': b[0], 'len': b[1], 'status': b[2]} for b in m.blocks],
                            'totals': m.totals()})
            except Exception as e:
                self._json({'ok': False, 'error': str(e)})
        elif p.startswith('/api/allocmap'):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            src = (q.get('src') or [''])[0]
            lba = int((q.get('lba') or ['0'])[0])
            if not src:
                self._json({'ok': False, 'error': 'bad args'}, 400)
            else:
                import allocmap
                self._json(allocmap.analyze(src, lba))
        elif p.startswith('/api/vmdetect'):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            src = (q.get('src') or [''])[0]
            if not src:
                self._json({'ok': False, 'error': 'bad args'}, 400)
            else:
                import vmdisk
                self._json(vmdisk.detect(src))
        elif p.startswith('/api/findpart'):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            src = (q.get('src') or [''])[0]
            want = (q.get('want') or [''])[0]
            if not src:
                self._json({'ok': False, 'error': 'bad args'}, 400)
            else:
                import findpart
                try:
                    if want:
                        hit = findpart.find_fs(src, want)
                        self._json({'ok': bool(hit), 'part': hit})
                    else:
                        self._json({'ok': True,
                                    'parts': findpart.find_partitions(src)})
                except OSError as e:
                    self._json({'ok': False, 'error': str(e)})
        elif p == '/api/jobs':
            self._json({'jobs': [
                {k: j.get(k) for k in ('id', 'kind', 'state', 'created', 'params', 'last_event', 'result', 'error')}
                for j in jobstore.list_jobs(limit=200)]})
        elif p == '/api/harvest':
            import harvest
            self._json(harvest.collect())
        elif p == '/api/version':
            import version
            self._json({'ok': True, 'version': version.VERSION, 'build': version.build_time()})
        elif p == '/api/schema':
            import schema
            sc = schema.get_schema()
            miss = schema.missing_in_schema()
            if miss:
                sc['warning'] = '未登记进schema的任务类型: ' + ','.join(miss)
            self._json(sc)
        elif p.startswith('/api/job/'):
            jid = p.split('/')[-1]
            j = jobstore.load(jid)
            self._json(j if j else {'error': 'not found'}, 200 if j else 404)
        elif p.startswith('/api/events/'):
            self._sse(p.split('/')[-1])
        else:
            self.send_error(404)

    def do_POST(self):
        p = self.path.split('?')[0]
        body = self._body()
        if p == '/api/job':
            kind = body.get('kind')
            params = body.get('params', {})
            if kind not in pipeline.RUNNERS:
                self._json({'error': '未知任务: ' + str(kind)}, 400)
                return
            job = pipeline.start_job(kind, params)
            self._json(job)
        elif p.startswith('/api/job/') and p.endswith('/cancel'):
            jid = p.split('/')[3]
            evt = pipeline.CANCEL.get(jid)
            if evt:
                evt.set()
            self._json({'ok': True})
        elif p == '/api/open':
            target = body.get('path', '')
            if os.path.isfile(target):
                os.startfile(target)          # 打开文件
                self._json({'ok': True})
            elif os.path.isdir(target):
                os.startfile(target)          # 资源管理器打开目录
                self._json({'ok': True})
            else:
                self._json({'ok': False, 'error': '路径不存在'}, 400)
        else:
            self.send_error(404)

    # ---------- SSE ----------
    def _sse(self, jid):
        bus = pipeline.BUSSES.get(jid)
        job = jobstore.load(jid)
        if not bus and not job:
            self._json({'error': 'not found'}, 404)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.end_headers()
        q = queue.Queue(maxsize=256)
        if bus:
            bus.attach(q)
        try:
            # 已结束的任务: 直接补发持久化结果
            if not bus and job:
                ev = {'event': '_replay_state', 'state': job.get('state'),
                      'result': job.get('result'), 'last_event': job.get('last_event')}
                self.wfile.write(('data: ' + json.dumps(ev, ensure_ascii=False) + '\n\n').encode('utf-8'))
                self.wfile.write(b'data: {"event":"_end"}\n\n')
                self.wfile.flush()
                return
            while True:
                try:
                    ev = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b': ping\n\n')
                    self.wfile.flush()
                    continue
                self.wfile.write(('data: ' + json.dumps(ev, ensure_ascii=False) + '\n\n').encode('utf-8'))
                self.wfile.flush()
                if ev.get('event') == '_end':
                    break
        except (ConnectionAbortedError, BrokenPipeError, OSError):
            pass
        finally:
            if bus:
                bus.detach(q)


import hashlib

IMG_TYPES = {
    'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png',
    'gif': 'image/gif', 'webp': 'image/webp', 'bmp': 'image/bmp',
}
THUMB_CACHE = os.path.join(os.environ.get('TEMP', '/tmp'), 'rw_thumb')
IMG_MAX = 50 * 1024 * 1024


def _serve_img(self, path):
    """场景a: 直接serve磁盘上的图片文件(扩展名+大小防护)"""
    ext = path.lower().rsplit('.', 1)[-1] if '.' in path else ''
    if ext not in IMG_TYPES:
        self._json({'error': 'not an image'}, 400)
        return
    try:
        if os.path.getsize(path) > IMG_MAX:
            self._json({'error': 'too large'}, 400)
            return
        with open(path, 'rb') as f:
            b = f.read()
    except OSError as e:
        self._json({'error': str(e)}, 404)
        return
    self.send_response(200)
    self.send_header('Content-Type', IMG_TYPES[ext])
    self.send_header('Content-Length', str(len(b)))
    self.send_header('Cache-Control', 'private, max-age=3600')
    self.end_headers()
    self.wfile.write(b)


def _serve_from_image(self, src, lba, fid):
    """场景b: 从镜像/分区提取单文件到缓存后serve"""
    key = hashlib.md5(('%s|%s|%s' % (src, lba, fid)).encode('utf-8', 'ignore')).hexdigest()[:16]
    cdir = os.path.join(THUMB_CACHE, key)
    if not os.path.isdir(cdir):
        os.makedirs(cdir, exist_ok=True)
        ev, rc = sfadapter.run(['extract', '--source', src, '--partition-lba', lba,
                                '--ids', str(fid), '--out', cdir])
        if rc != 0:
            self._json({'error': 'extract失败'}, 500)
            return
    # 在缓存目录里找唯一文件
    found = None
    for dp, _, names in os.walk(cdir):
        for n in names:
            found = os.path.join(dp, n)
    if not found:
        self._json({'error': 'empty'}, 404)
        return
    _serve_img(self, found)


def is_admin():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


if __name__ == '__main__':
    admin = is_admin()
    import version
    print('*' * 46)
    print('  数据抢救工作台 v%s (构建 %s)' % (version.VERSION, version.build_time()))
    print('  http://127.0.0.1:%d' % PORT)
    print('  权限: %s' % ('管理员(可读物理盘)' if admin else '普通(读物理盘需重启提权)'))
    ev, rc = sfadapter.version()
    if ev and ev.get('contract') is not None:
        if ev['contract'] != 2:
            print('  ⚠️ sf-cli JSON契约版本=%s (适配层按v2编写), 建议更新engine/sf-cli.exe' % ev['contract'])
        else:
            print('  引擎: sf-cli %s (契约v2 OK)' % ev.get('version'))
    print('*' * 46)
    stale = jobstore.mark_stale_jobs()
    if stale:
        print('  已清扫%d个假活任务(上个进程中断遗留)' % stale)
    srv = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
