# -*- coding: utf-8 -*-
"""总测试入口: python -I tests/run_all.py
全套分钟级; 改完引擎/适配层后必跑。"""
import os
import sys
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
# LibreOffice python自spawn会WinError5, 统一走py启动器(官方3.11)
PYBASE = ['py', '-3'] if os.system('py -3 -c "1" >nul 2>&1') == 0 else [sys.executable]

SUITES = ['test_units.py', 'test_safeguard.py', 'test_imager.py', 'test_wxdat.py', 'test_allocmap.py', 'test_h264carve.py', 'test_dav.py', 'test_exfatdeep.py', 'test_vmdisk.py', 'test_ext4.py', 'test_bitlocker.py', 'test_content.py', 'test_t4.py', 'test_raid.py', 'test_macfs.py', 'test_pst.py', 'test_e2e.py']

results = {}
for s in SUITES:
    path = os.path.join(HERE, s)
    if not os.path.exists(path):
        print('SKIP %s (不存在)' % s)
        continue
    print('\n########## %s ##########' % s, flush=True)
    r = subprocess.run(PYBASE + ['-I', path], capture_output=True, text=True,
                       encoding='utf-8', errors='replace', cwd=HERE)
    safe = lambda t: (t or '').encode('gbk', errors='replace').decode('gbk')
    print(safe(r.stdout[-3000:]), end='')
    if r.stderr:
        print('[stderr] ' + safe(r.stderr[-500:]), flush=True)
    results[s] = r.returncode

print('\n================ 汇总 ================')
allok = True
for s, rc in results.items():
    print('%-22s %s' % (s, '通过' if rc == 0 else '失败 rc=%s' % rc))
    allok = allok and rc == 0
print('总判定: ' + ('全绿' if allok else '有失败'))
sys.exit(0 if allok else 1)
