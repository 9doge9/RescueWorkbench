# -*- coding: utf-8 -*-
"""SMART健康属性: 调smartctl -A -H解析关键指标
关注字段: 重映射扇区(5)/通电时间(9)/通电次数(12)/温度(194或190)/待定扇区(197)/UNC纠正(198)/SSD磨损(231/233)
"""
import os
import re
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
SMARTCTL = os.path.join(HERE, 'bin', 'smartctl.exe')

ATTR_NAMES = {
    5: '重映射扇区数', 9: '通电时间', 12: '通电次数',
    170: '可用预留空间', 171: '编程失败次数', 172: '擦除失败次数',
    173: '磨损均衡次数', 174: '意外掉电次数', 175: '掉电损耗',
    176: '擦除失败计数', 177: '磨损范围差值',
    187: 'UNC错误总数', 188: '命令超时',
    190: '温度(气流)', 194: '温度', 196: '重新分配事件',
    197: '待定扇区数', 198: 'UNC纠正扇区', 199: 'UDMA CRC错误',
    231: 'SSD剩余寿命', 232: ' endurance剩余', 233: '介质磨损指标',
    241: '总写入量(LBA)', 242: '总读取量(LBA)',
}
# 判死的阈值属性: 值非零=有物理问题
CRITICAL = {5: '重映射扇区', 187: 'UNC错误', 196: '重新分配事件', 197: '待定扇区', 198: 'UNC纠正'}


def query(disk_number):
    """disk_number: 物理盘号 → {ok, health, temp, attrs:[], warnings:[]}"""
    dev = '/dev/pd%d' % disk_number
    # 先自动探测类型, 不行再试nvme/ata
    out = ''
    for dtype in (None, 'nvme', 'ata', 'sat'):
        args = [SMARTCTL, '-A', '-H']
        if dtype:
            args += ['-d', dtype]
        args.append(dev)
        try:
            r = subprocess.run(args, capture_output=True, text=True, encoding='utf-8',
                               errors='replace',
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                               timeout=30)
        except (subprocess.TimeoutExpired, OSError):
            continue
        out = r.stdout or ''
        if 'does not detect' not in out and 'Unable to detect' not in out and (out.count('0x') > 0 or 'overall' in out.lower() or 'health' in out.lower()):
            break
    if not out or 'Unable to detect' in out:
        return {'ok': False, 'reason': '设备或桥接芯片不支持SMART'}
    health = '未知'
    m = re.search(r'overall-health self-assessment test result:\s*(\S+)', out, re.I)
    if not m:
        m = re.search(r'Smart overall-health self-assessment:\s*(\S+)', out, re.I)
    if m:
        health = m.group(1)

    attrs = []
    warnings = []
    temp = None

    # NVMe格式: 键值对(SMART/Health Information)
    if 'NVMe' in out and 'Critical Warning' in out:
        kv = dict(re.findall(r'^([A-Za-z0-9 %()\[\]/.-]+?):\s+(.+)$', out, re.M))
        NVME_MAP = {
            'Temperature': '温度',
            'Available Spare': '可用预留空间',
            'Percentage Used': '寿命已用',
            'Data Units Written': '总写入量',
            'Power On Hours': '通电时间',
            'Power Cycles': '通电次数',
            'Unsafe Shutdowns': '意外掉电次数',
            'Media and Data Integrity Errors': '介质错误',
            'Critical Warning': '临界警告',
        }
        for raw_key, cn in NVME_MAP.items():
            if raw_key in kv:
                val = kv[raw_key].strip()
                attrs.append({'id': 0, 'name': cn, 'value': val.split()[0], 'worst': '-', 'thresh': '-', 'raw': val[:40]})
                if raw_key == 'Temperature':
                    try:
                        temp = int(val.split()[0])
                    except ValueError:
                        pass
                if raw_key == 'Critical Warning' and val.strip() not in ('0x00', '0'):
                    warnings.append('临界警告: %s' % val)
                if raw_key == 'Available Spare':
                    pct = val.strip().rstrip('%')
                    if pct.isdigit() and int(pct) < 20:
                        warnings.append('可用预留空间仅%s%%' % pct)
                if raw_key == 'Percentage Used':
                    pct = val.strip().rstrip('%')
                    if pct.isdigit() and int(pct) > 90:
                        warnings.append('寿命已用%s%%' % pct)
                if raw_key == 'Media and Data Integrity Errors':
                    n = val.split()[0].replace(',', '')
                    if n.isdigit() and int(n) > 0:
                        warnings.append('介质错误 %s' % val.split()[0])
        return {'ok': True, 'health': health, 'temp': temp,
                'attrs': attrs, 'warnings': warnings, 'raw_available': True}
    # ID# attribute_name flag value worst thresh type updated when_raw
    for line in out.split('\n'):
        mm = re.match(r'^\s*(\d+)\s+(\S+)\s+0x[0-9a-fA-F]+\s+(\d+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(\S+)(.*)$', line)
        if not mm:
            continue
        aid = int(mm.group(1))
        if aid not in ATTR_NAMES:
            continue
        raw_field = mm.group(8).strip()
        raw_val = raw_field.split()[0] if raw_field else '0'
        try:
            num = int(raw_val)
        except ValueError:
            num = 0
        # 温度字段取原始值
        if aid in (190, 194) and temp is None:
            try:
                temp = int(raw_val.split()[0])
            except ValueError:
                pass
        attrs.append({'id': aid, 'name': ATTR_NAMES[aid], 'value': mm.group(3),
                      'worst': mm.group(4), 'thresh': mm.group(5), 'raw': raw_field[:40]})
        if aid in CRITICAL and num > 0:
            warnings.append('%s=%s（%s）' % (CRITICAL[aid], num, '立即备份'))
        if aid == 231 and num < 10 and num > 0:
            warnings.append('SSD剩余寿命仅%s%%' % num)

    # nvme 格式(smartctl对nvme输出不同): smart health info
    if not attrs and 'nvme' in out.lower():
        for k, v in _parse_nvme(out).items():
            if k == '温度':
                temp = v
            if k in ('临界警告', '可用备用空间不足', '介质错误', '百分比剩余') and v not in ('0', '正常', '100'):
                warnings.append('%s: %s' % (k, v))

    return {'ok': True, 'health': health, 'temp': temp,
            'attrs': attrs, 'warnings': warnings,
            'raw_available': len(attrs) > 0}


def _parse_nvme(out):
    d = {}
    for line in out.split('\n'):
        mm = re.match(r'^\s*([^:]+):\s+(.+)$', line)
        if mm and any(k in mm.group(1).lower() for k in ('temperature', 'critical', 'available', 'percentage', 'media errors')):
            d[mm.group(1).strip()] = mm.group(2).strip()
    return d


def summary(verdict):
    """体检式一句总结"""
    if not verdict.get('ok'):
        return verdict.get('reason', '不可读')
    if verdict['warnings']:
        return '⚠️ ' + '；'.join(verdict['warnings'][:3])
    h = verdict.get('health', '')
    if 'PASSED' in h.upper():
        t = ('，%d°C' % verdict['temp']) if verdict.get('temp') else ''
        return 'SMART整体通过' + t
    return 'SMART状态: ' + h
