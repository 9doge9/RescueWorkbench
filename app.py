# -*- coding: utf-8 -*-
"""数据抢救工作台 独立应用入口
pywebview原生窗口 + 内嵌本地服务。打包后双击即用(需UAC读物理盘)。
"""
import os
import sys
import socket
import threading


def app_root():
    """打包后(frozen)取exe所在目录; 开发时取项目根"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


class JsApi:
    """pywebview JS桥: 原生文件夹/文件选择器"""

    def pick_folder(self):
        try:
            import webview
            win = webview.windows[0]
            r = win.create_file_dialog(webview.FOLDER_DIALOG)
            if r:
                return r if isinstance(r, str) else r[0]
        except Exception:
            pass
        return ''

    def pick_file(self):
        try:
            import webview
            win = webview.windows[0]
            r = win.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=False,
                file_types=('虚拟磁盘 (*.vhd;*.vhdx;*.vmdk;*.raw;*.img)', '所有文件 (*.*)'))
            if r:
                return r if isinstance(r, str) else r[0]
        except Exception:
            pass
        return ''


def main():
    root = app_root()
    # frozen时 web/ 和 engine/ 在 _MEIPASS(_internal)里
    base = getattr(sys, '_MEIPASS', root)
    sys.path.insert(0, root)
    sys.path.insert(0, os.path.join(base, 'engine'))

    port = free_port()

    # 内嵌启动本地服务(复用server.py)
    os.chdir(root)
    import server as srv
    srv.ROOT = base                      # 静态web资源根
    from http.server import ThreadingHTTPServer
    httpd = ThreadingHTTPServer(('127.0.0.1', port), srv.Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()

    import webview
    # 窗口自适应屏幕: 目标1180x800, 小屏按90%工作区收缩(兼容1366x768老本/150%缩放)
    w, h = 1180, 800
    try:
        scr = webview.screens[0]
        w = min(w, int(scr.width * 0.92))
        h = min(h, int(scr.height * 0.88))
    except Exception:
        pass
    webview.create_window(
        '数据抢救工作台',
        'http://127.0.0.1:%d/' % port,
        width=w, height=h, min_size=(760, 520),
        resizable=True,
        text_select=False,
        js_api=JsApi(),
    )
    webview.start()
    httpd.shutdown()


if __name__ == '__main__':
    main()
