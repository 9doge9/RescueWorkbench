# -*- coding: utf-8 -*-
"""版本唯一事实源 — banner/关于页/报告脚注共用
BUILD 取本文件的mtime: PyInstaller构建时随拷贝时间落定, 源码运行即代码时间。
"""
import os
import time

VERSION = '1.3.2'


def build_time():
    try:
        t = os.path.getmtime(__file__)
    except OSError:
        t = time.time()
    return time.strftime('%Y-%m-%d %H:%M', time.localtime(t))
