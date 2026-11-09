# -*- coding: utf-8 -*-
"""safeguard 同盘输出拒绝 单元测试"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'engine'))
import safeguard as sg

SRC = '\\\\.\\PhysicalDrive0'      # C/D所在NVMe

failures = []


def case(name, src, out, expect_refuse):
    try:
        sg.ensure_output_off_source(src, [out])
        refused = False
    except sg.OutputOnSourceDrive:
        refused = True
    ok = refused == expect_refuse
    print(('PASS' if ok else 'FAIL'), name, '->', '拒绝' if refused else '通过')
    if not ok:
        failures.append(name)


case('源0+系统盘输出(同NVMe,应拒)', SRC, os.path.expanduser(r'~\Desktop\t.img'), True)
case('源0+D盘输出(同NVMe,应拒)', SRC, r'D:\任意\out.img', True)
case('镜像文件源(应通过)', r'D:\src.img', r'D:\out.img', False)
case('源0+不存在盘输出(fail-closed应拒)', SRC, r'Z:\no\such\drive\out.img', True)

print('\n结果:', '全部通过' if not failures else '失败: %s' % failures)
sys.exit(1 if failures else 0)
