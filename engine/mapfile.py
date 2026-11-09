# -*- coding: utf-8 -*-
"""ddrescue兼容mapfile: 记录镜像每个区域的抢救状态
格式与GNU ddrescue一致, ddrescueview可直接打开。
不变量: blocks有序、不重叠、连续覆盖[0,size)。
状态: '?'未试 '*'未trim '/'未scrape '-'坏扇区 '+'完成
"""
import os
import time

NON_TRIED = '?'
NON_TRIMMED = '*'
NON_SCRAPED = '/'
BAD_SECTOR = '-'
FINISHED = '+'
_VALID = {NON_TRIED, NON_TRIMMED, NON_SCRAPED, BAD_SECTOR, FINISHED}


class MapError(Exception):
    pass


class MapFile:
    def __init__(self, size, current_pos=0, current_status=NON_TRIED, blocks=None):
        self.size = size
        self.current_pos = current_pos
        self.current_status = current_status
        self.blocks = blocks if blocks is not None else [(0, size, NON_TRIED)]

    # ---------- 查询 ----------
    def totals(self):
        t = {c: 0 for c in _VALID}
        for _, ln, st in self.blocks:
            t[st] += ln
        return t

    def find_next(self, pos, statuses):
        """pos之后第一个处于statuses的区域 → (start, avail); 无则None"""
        for start, ln, st in self.blocks:
            if st in statuses and start + ln > pos:
                return start, start + ln - max(start, pos)
        return None

    def find_prev(self, before, statuses):
        """before之前最后一个处于statuses的区域 → (start, len); 无则None"""
        best = None
        for start, ln, st in self.blocks:
            if st in statuses and start + ln <= before:
                best = (start, ln)
        return best

    # ---------- 修改 ----------
    def mark(self, start, ln, status):
        if status not in _VALID:
            raise MapError('bad status %r' % status)
        end = start + ln
        if start < 0 or end > self.size or ln <= 0:
            raise MapError('mark越界 [%d,%d) size=%d' % (start, end, self.size))
        out = []
        for bstart, blen, bst in self.blocks:
            bend = bstart + blen
            if bend <= start or bstart >= end:          # 无交集, 原样保留
                out.append((bstart, blen, bst))
                continue
            if bstart < start:                          # 前残段
                out.append((bstart, start - bstart, bst))
            out.append((max(bstart, start), min(bend, end) - max(bstart, start), status))
            if bend > end:                              # 后残段
                out.append((end, bend - end, bst))
        out.sort(key=lambda b: b[0])
        self.blocks = self._merge(out)

    @staticmethod
    def _merge(blocks):
        merged = []
        for b in blocks:
            if b[1] == 0:
                continue
            if merged and merged[-1][2] == b[2] and merged[-1][0] + merged[-1][1] == b[0]:
                merged[-1] = (merged[-1][0], merged[-1][1] + b[1], b[2])
            else:
                merged.append(b)
        return merged

    # ---------- 序列化 ----------
    def to_ddrescue_string(self):
        lines = ['# Mapfile. Created by Rescue Workbench (ddrescue compatible).',
                 '# current_pos  current_status']
        lines.append('0x%X     %s' % (self.current_pos, self.current_status))
        for start, ln, st in self.blocks:
            lines.append('0x%08X  0x%08X  %s' % (start, ln, st))
        return '\n'.join(lines) + '\n'

    def save(self, path):
        """原子写: tmp + fsync + rename; rename被锁则重试5次×50ms"""
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='ascii', newline='\n') as f:
            f.write(self.to_ddrescue_string())
            f.flush()
            os.fsync(f.fileno())
        for i in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if i == 4:
                    raise
                time.sleep(0.05)

    @classmethod
    def load(cls, path, expect_size=None):
        """解析ddrescue格式mapfile; expect_size校验总大小(末块end)"""
        blocks = []
        current_pos = 0
        current_status = NON_TRIED
        with open(path, encoding='ascii') as f:
            first = True
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split()
                if first:
                    if len(parts) < 2:
                        raise MapError('line %d: current_pos行缺字段' % lineno)
                    current_pos = _parse_hex(parts[0])
                    current_status = parts[1][0]
                    if current_status not in _VALID:
                        raise MapError('line %d: 非法状态 %r' % (lineno, current_status))
                    first = False
                    continue
                if len(parts) < 3:
                    raise MapError('line %d: 数据行需3字段' % lineno)
                p, ln, st = _parse_hex(parts[0]), _parse_hex(parts[1]), parts[2][0]
                if st not in _VALID or ln <= 0:
                    raise MapError('line %d: 非法块 %r' % (lineno, line))
                blocks.append((p, ln, st))
        if not blocks:
            raise MapError('mapfile无数据块')
        blocks.sort(key=lambda b: b[0])
        # 连续性与覆盖校验
        cursor = 0
        for start, ln, _ in blocks:
            if start != cursor:
                raise MapError('块不连续: 期望起点%d 实际%d' % (cursor, start))
            cursor = start + ln
        size = cursor
        if expect_size is not None and size != expect_size:
            raise MapError('map尺寸%d ≠ 源尺寸%d(错误的map, 拒绝续跑)' % (size, expect_size))
        m = cls(size, current_pos, current_status, cls._merge(blocks))
        return m


def _parse_hex(s):
    """ddrescue风格: 兼容可选0x前缀的十六进制"""
    s = s.strip()
    neg = s.startswith('-')
    if neg:
        s = s[1:]
    if s[:2].lower() == '0x':
        s = s[2:]
    v = int(s, 16)
    return -v if neg else v


if __name__ == '__main__':
    import tempfile
    m = MapFile(10000)
    m.mark(0, 4096, FINISHED)
    m.mark(8192, 512, BAD_SECTOR)
    print(m.to_ddrescue_string())
    with tempfile.NamedTemporaryFile('w', suffix='.map', delete=False) as f:
        p = f.name
    m.save(p)
    m2 = MapFile.load(p, expect_size=10000)
    assert m2.blocks == m.blocks and m2.current_pos == m.current_pos
    print('roundtrip OK:', m2.totals())
    os.unlink(p)
