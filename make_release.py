# -*- coding: utf-8 -*-
"""组装公开发布目录 release/ — 干净的 git 仓库形态
用法: python make_release.py
排除: 内部文档/用户数据/构建产物/大二进制/含个人路径脚本
包含: 源码+web+licenses+README+RELEASE_NOTES+测试(源码与manifest)+下载指引
"""
import os
import shutil
import glob

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'release')

INCLUDE_DIRS = ('web', 'engine', 'tests', 'licenses', 'docs')
INCLUDE_FILES = ('README.md', 'RELEASE_NOTES.md', '.gitignore', 'LICENSE', 'NOTICE', 'app.py',
                 'server.py', 'build.bat', 'run.bat', 'RescueWorkbench.spec', 'mcp_server.py', 'AGENT.md')
EXCLUDE_PATTERNS = ('__pycache__', '*.pyc')
# tests里的个人路径脚本按需排除(夹具重建用, 含本机junction设计)
EXCLUDE_FILES = {'start_server.ps1', 'kill_server.ps1', 'verify_img.ps1',
                 'make_test_vhd.ps1', 'worker.ps1', 'manifest_del.ps1'}


def main():
    if os.path.exists(OUT):
        shutil.rmtree(OUT)
    os.makedirs(OUT)
    n_files = n_bytes = 0
    for d in INCLUDE_DIRS:
        for dirpath, dirnames, filenames in os.walk(os.path.join(HERE, d)):
            rel = os.path.relpath(dirpath, HERE)
            dirnames[:] = [x for x in dirnames
                           if x != '__pycache__' and x != 'bin'
                           and x != 'ref_mft']
            if os.sep + 'bin' in dirpath or dirpath.endswith(os.sep + 'bin'):
                continue                      # engine/bin 不入源码发布
            for fn in filenames:
                if fn.endswith('.pyc') or fn in EXCLUDE_FILES:
                    continue
                if d == 'engine' and fn in ('sf-cli.exe',):
                    continue                      # bin/整体不入库
                if fn.endswith(('.vhd', '.vhdx', '.img', '.raw', '.zip')):
                    continue                      # 夹具大二进制不入库
                src = os.path.join(dirpath, fn)
                dst = os.path.join(OUT, rel, fn)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                n_files += 1
                n_bytes += os.path.getsize(src)
    for f in INCLUDE_FILES:
        src = os.path.join(HERE, f)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(OUT, f))
            n_files += 1
            n_bytes += os.path.getsize(src)
    print('release/ 组装完成: %d 个文件, %.1f MB' % (n_files, n_bytes / 1048576))
    # 敏感信息终检
    bad = []
    for dirpath, _, files in os.walk(OUT):
        for fn in files:
            if not fn.endswith(('.py', '.js', '.html', '.bat', '.spec', '.ps1', '.md')):
                continue
            p = os.path.join(dirpath, fn)
            try:
                t = open(p, encoding='utf-8', errors='ignore').read()
            except OSError:
                continue
            if 'C:\\Users\\doge' in t or '\\Users\\doge' in t:
                bad.append(os.path.relpath(p, OUT))
    if bad:
        print('!! 发现残留个人路径:')
        for b in bad:
            print('   ', b)
    else:
        print('敏感信息终检: 通过(无个人路径残留)')


if __name__ == '__main__':
    main()
