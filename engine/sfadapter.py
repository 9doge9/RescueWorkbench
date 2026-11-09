# -*- coding: utf-8 -*-
"""sf-cli适配层: 封装SectorForge命令行引擎, 输出统一为Python字典事件流
纯标准库。sf-cli.exe随项目携带(MIT协议)。
"""
import subprocess
import threading
import queue
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
SF_CLI = os.path.join(_HERE, 'sf-cli.exe')


def _spawn(args, on_event=None, event_q=None):
    """运行sf-cli子进程, 解析NDJSON事件行。返回 (proc, 完成回调结果dict)。
    on_event: 每个事件的回调 fn(dict) -> None
    事件同时放入 event_q (若给定) 供SSE转发。"""
    cmd = [SF_CLI, '--json'] + [str(a) for a in args]
    # 命令回放: 控制台透明页/抽屉用
    if on_event:
        try:
            on_event({'event': 'command', 'command': ' '.join(cmd)})
        except Exception:
            pass
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding='utf-8', errors='replace',
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    def reader():
        last = None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            last = ev
            if on_event:
                try:
                    on_event(ev)
                except Exception:
                    pass
            if event_q is not None:
                event_q.put(ev)
        proc.wait()
        if event_q is not None:
            event_q.put(None)  # 结束标记

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    return proc, t


def run(args, on_event=None):
    """同步运行, 返回最后一个事件(通常是result); on_event收到每个事件"""
    box = {'last': None}

    def wrap(e):
        box['last'] = e
        if on_event:
            try:
                on_event(e)
            except Exception:
                pass

    proc, t = _spawn(args, on_event=wrap)
    t.join()
    return box['last'], proc.returncode


def run_async(args, on_event=None):
    """异步运行, 返回 (proc, thread)"""
    return _spawn(args, on_event=on_event)


def run_killable(args, cancel_evt, on_event=None, poll=0.5):
    """可取消运行: cancel_evt置位时杀进程(sf-cli靠被杀实现暂停,断点在mapfile)。
    返回 (最后事件, 退出码, 是否被取消)"""
    import time
    box = {'last': None}

    def wrap(ev):
        box['last'] = ev
        if on_event:
            try:
                on_event(ev)
            except Exception:
                pass

    proc, t = _spawn(args, on_event=wrap)
    cancelled = False
    while t.is_alive():
        if cancel_evt.is_set():
            cancelled = True
            proc.kill()
            break
        t.join(poll)
    t.join()
    return box['last'], proc.returncode, cancelled


# ---------- 高层封装 ----------

def devices(on_event=None):
    return run(['devices'], on_event)


def scan(source, deep=False, on_event=None):
    args = ['scan', '--source', source]
    if deep:
        args.append('--deep')
    return run(args, on_event)


def files_list(source, partition_lba, mapfile=None, on_event=None):
    args = ['files', '--source', source, '--partition-lba', partition_lba]
    if mapfile:
        args += ['--map', mapfile]
    return run(args, on_event)


def extract(source, partition_lba, ids, out, mapfile=None, on_event=None):
    args = ['extract', '--source', source, '--partition-lba', partition_lba,
            '--ids', ids, '--out', out]
    if mapfile:
        args += ['--map', mapfile]
    return run(args, on_event)


def carve(source, out, types=None, mapfile=None, on_event=None):
    args = ['carve', '--source', source, '--out', out]
    if types:
        args += ['--types', types]
    if mapfile:
        args += ['--map', mapfile]
    return run(args, on_event)


def image(source, dest, mapfile, access=None, on_event=None):
    args = ['image', '--source', source, '--dest', dest, '--map', mapfile]
    if access:
        args += ['--access', access]
    return run(args, on_event)


def hash(source, on_event=None):
    return run(['hash', '--source', source], on_event)


def version():
    return run(['version'])


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == 'test':
        ev, rc = devices()
        print('devices rc=', rc)
        print(json.dumps(ev, ensure_ascii=False, indent=2)[:500])
        ev, rc = scan(os.path.join(_HERE, '..', '..', '工具评估', 'SectorForge', 'cartest.img'))
        print('scan rc=', rc, 'last event =', ev)


def strip_orphan_suffix(directory):
    """sf-cli提取的NTFS已删文件名带" (id N)"后缀 → 批量剥离
    冲突时保留原名(不覆盖任何东西)。→ 改名数"""
    import os
    import re
    pat = re.compile(r' \(id \d+\)(?=\.[^.]+$|$)')
    renamed = 0
    for fn in os.listdir(directory):
        m = pat.search(fn)
        if not m:
            continue
        new = fn[:m.start()] + fn[m.end():]
        src = os.path.join(directory, fn)
        dst = os.path.join(directory, new)
        if not new or os.path.exists(dst):
            continue
        try:
            os.rename(src, dst)
            renamed += 1
        except OSError:
            pass
    return renamed
