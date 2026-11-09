# -*- coding: utf-8 -*-
"""校验式镜像器 v2
map驱动断点(ddrescue兼容) + 二分坏区递归 + 双句柄三点抽检 + 同盘输出拒绝。
设计对标SectorForge imager.rs + aigo实战经验:
- 读取失败或三点抽检不一致都视为"坏", 二分下探到扇区粒度
- 崩溃一致性: 先flush镜像数据再存map
- mapfile周期保存失败吞掉(杀毒锁文件), force才传播
"""
import os
import sys
import time
import random
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from stability import open_source, read_at
from mapfile import (MapFile, MapError, NON_TRIED, NON_TRIMMED,
                     NON_SCRAPED, BAD_SECTOR, FINISHED)
from safeguard import ensure_output_off_source, OutputOnSourceDrive


def sfadapter_devices_size(path):
    """从sf-cli devices查物理盘尺寸(慢路径兜底)"""
    try:
        import sfadapter
        ev, rc = sfadapter.devices()
        for d in ev.get('devices', []):
            if d['path'].lower() == path.lower():
                return d['size'], None
    except Exception:
        pass
    return None, 'not found'

CHUNK = 8 * 1024 * 1024
CHECKPOINT_INTERVAL = 5.0
MAX_RETRIES = 3


class VerifiedImager:
    def __init__(self, source, dest, on_progress=None, identity=None,
                 mapfile=None, chunk=CHUNK, sector=512, total_size=None):
        self.source = source
        self.dest = dest
        self.mapfile = mapfile or (dest + '.map')
        self.on_progress = on_progress or (lambda d: None)
        self.identity = identity
        self.chunk = chunk
        self.sector = sector
        self.total_size = total_size      # 优先外部给定(如sf-cli devices); 物理盘不支持seek(0,2)
        self._cancel = threading.Event()
        # 统计
        self.rereads = 0
        self.bad_regions = []          # [(start,len)] 扇区粒度坏区
        self.io_retries = 0
        self.speed_bins = []           # [(pos, mbps)] 每块完成时的吞吐——热力图数据

    def cancel(self):
        self._cancel.set()

    # ---------- 句柄 ----------
    def _open_src(self):
        f = open_source(self.source)
        if self.identity and not self.identity(f):
            f.close()
            raise IOError('源盘身份校验失败(编号变化或非目标盘)')
        return f

    def _read_full(self, f, off, n):
        parts = []
        got = 0
        while got < n:
            f.seek(off + got)
            b = f.read(n - got)
            if not b:
                raise OSError('源读取提前终止@%d' % (off + got))
            parts.append(b)
            got += len(b)
        return b''.join(parts)

    # ---------- 三点抽检 ----------
    def _spot_ok(self, vrf, buf, pos):
        n = len(buf)
        # 叶子块(512B)只有首点; 一般块取 首/随机中/尾 三点(修复: n<1536时mid会越界)
        pts = [0, n - 512]
        if n >= 1536:
            mid = 512 * random.Random(pos).randrange(1, n // 512 - 1)
            if 0 < mid < n - 512:
                pts.append(mid)
        for rel in sorted(set(pts)):
            a = read_at(vrf, pos + rel, 512)
            if a is None or a != buf[rel:rel + 512]:
                return False
        return True

    # ---------- checkpoint ----------
    def _checkpoint(self, force=False):
        now = time.time()
        if not force and now - self._last_ckpt < CHECKPOINT_INTERVAL:
            return
        self._last_ckpt = now
        try:
            self.dst.flush()
            os.fsync(self.dst.fileno())     # 镜像数据必须先到OS
            self.map.save(self.mapfile)
        except Exception:
            if force:
                raise
            # 周期保存失败吞掉: 杀毒/索引器短暂锁文件, 漏一次只是多覆盖一点

    # ---------- 核心 ----------
    def _split_and_verify(self, src, vrf, pos, ln, attempt=0):
        """二分递归: 读失败或抽检不一致 → 大于扇区就对半下探; 叶子重试后定生死"""
        if self._cancel.is_set():
            return
        try:
            buf = self._read_full(src, pos, ln)
            ok = self._spot_ok(vrf, buf, pos)
        except OSError:
            buf = None
            ok = False
        if ok:
            self.dst.seek(pos)
            self.dst.write(buf)
            self.map.mark(pos, ln, FINISHED)
            self._checkpoint(False)
            return
        if ln > self.sector:
            self.map.mark(pos, ln, NON_SCRAPED)
            self._checkpoint(False)
            half = max(ln // 2, self.sector)
            half -= half % self.sector
            self._split_and_verify(src, vrf, pos, half, 0)
            self._split_and_verify(src, vrf, pos + half, ln - half, 0)
            return
        self._leaf_fail(pos, ln, attempt)

    def _leaf_fail(self, pos, ln, attempt):
        if attempt < MAX_RETRIES:
            self.rereads += 1
            time.sleep(0.05 * (attempt + 1))
            self._split_retry_leaf(pos, ln, attempt + 1)
        else:
            self.bad_regions.append((pos, ln))
            self.map.mark(pos, ln, BAD_SECTOR)
            self.on_progress({'event': 'bad_region', 'offset': pos, 'length': ln})
            self._checkpoint(False)

    def _split_retry_leaf(self, pos, ln, attempt):
        # 叶子重试需要句柄, 在run的上下文里通过闭包传递
        self._retry_leaf_cb(pos, ln, attempt)

    def _source_size(self, f, path):
        """尺寸解析链: 外部给定 → seek(0,2)(镜像文件) → sf-cli devices(物理盘)"""
        if self.total_size:
            return self.total_size
        try:
            f.seek(0, 2)
            n = f.tell()
            f.seek(0)
            return n
        except OSError:
            pass
        if path.startswith('\\\\.\\'):
            ev, rc = sfadapter_devices_size(path)
            if ev:
                return ev
        return os.path.getsize(path)

    def run(self):
        self.on_progress({'event': 'command',
                          'command': '校验式镜像器: %s -> %s (map=%s)' % (self.source, self.dest, self.mapfile)})
        # 0. 同盘输出拒绝(仅物理盘源)
        ensure_output_off_source(self.source, [self.dest, self.mapfile])

        d = os.path.dirname(os.path.abspath(self.dest))
        if d:
            os.makedirs(d, exist_ok=True)
        src = self._open_src()
        total = self._source_size(src, self.source)
        src.seek(0)
        self.total = total

        # map加载/续跑校验(收紧: 有完成区→镜像必须存在且长度==设备尺寸, 对标SectorForge)
        resumed = False
        if os.path.exists(self.mapfile):
            try:
                self.map = MapFile.load(self.mapfile, expect_size=total)
                t = self.map.totals()
                if t[FINISHED] > 0:
                    if not os.path.exists(self.dest):
                        raise MapError('map有完成区但镜像文件不存在——续跑会留下静默空洞'
                                       '；请删除 .map 后重跑(或换正确配对的镜像)')
                    if os.path.getsize(self.dest) != total:
                        raise MapError('镜像文件长度(%d)≠设备尺寸(%d), 拒绝续跑'
                                       % (os.path.getsize(self.dest), total))
                resumed = True
                self.on_progress({'event': 'resume', 'finished_bytes': t[FINISHED],
                                  'bad_bytes': t[BAD_SECTOR]})
            except MapError as e:
                self.on_progress({'event': 'resume_rejected', 'reason': str(e)})
                self.map = MapFile(total)
        else:
            self.map = MapFile(total)

        # 镜像文件: 续跑保留, 新建截断预分配
        if resumed and os.path.exists(self.dest):
            self.dst = open(self.dest, 'r+b')
        else:
            self.dst = open(self.dest, 'wb')
            self.dst.truncate(total)

        vrf = open_source(self.source)
        self._last_ckpt = 0
        self._last_fail_pos = -1
        t0 = time.time()
        last_t, last_fin = t0, self.map.totals()[FINISHED]
        self._retry_leaf_cb = None      # 占位, 下方闭包赋值

        def retry_leaf(pos, ln, attempt):
            try:
                buf = self._read_full(src, pos, ln)
            except OSError:
                self._leaf_fail(pos, ln, attempt)
                return
            if self._spot_ok(vrf, buf, pos):
                self.dst.seek(pos)
                self.dst.write(buf)
                self.map.mark(pos, ln, FINISHED)
                self._checkpoint(False)
            else:
                self._leaf_fail(pos, ln, attempt)
        self._retry_leaf_cb = retry_leaf

        # ---- 主拷贝遍: 顺序处理未试区 ----
        pos = 0
        while not self._cancel.is_set():
            region = self.map.find_next(pos, [NON_TRIED])
            if region is None:
                region0 = self.map.find_next(0, [NON_TRIED])   # 回卷
                if region0 is None:
                    break
                region = region0
            rpos, avail = region
            ln = min(self.chunk, avail)
            ln = max(ln - (ln % self.sector), self.sector)   # 向下对齐到扇区, 最少1扇区
            try:
                _t0 = time.time()
                buf = self._read_full(src, rpos, ln)
                self.speed_bins.append((rpos, ln / max(time.time() - _t0, 1e-6) / 1024 / 1024))
            except OSError as e:
                # 先判整体掉线还是局部坏区: 探LBA0可读=设备在线→坏区; 不可读=掉线等待
                alive = False
                try:
                    probe = read_at(vrf, 0, 512)
                    alive = probe is not None
                except OSError:
                    alive = False
                if not alive:
                    # 掉线暂停语义: 等设备回来(每5s重试, 上限20分钟), 期间不污染map
                    self.on_progress({'event': 'io_retry', 'offset': rpos,
                                      'err': '设备无响应, 等待重新连接: ' + str(e)})
                    gone = 0
                    while gone < 240 and not self._cancel.is_set():
                        time.sleep(5)
                        gone += 1
                        try:
                            src.close()
                        except Exception:
                            pass
                        try:
                            vrf.close()
                        except Exception:
                            pass
                        try:
                            src = self._open_src()
                            vrf = open_source(self.source)
                            if read_at(vrf, 0, 512) is not None:
                                self.on_progress({'event': 'phase', 'message': '设备已重新在线, 继续镜像'})
                                break
                        except Exception:
                            continue
                    if self._cancel.is_set():
                        break
                    if gone >= 240:
                        raise OSError('设备离线超过20分钟: %s' % str(e))
                    continue
                # 设备在线: 先怀疑句柄脏(USB抖动), 重开一次; 同位置再失败=真坏区
                if self._last_fail_pos != rpos:
                    self._last_fail_pos = rpos
                    self.io_retries += 1
                    self.on_progress({'event': 'io_retry', 'offset': rpos, 'err': str(e)})
                    time.sleep(1)
                    try:
                        src.close()
                    except Exception:
                        pass
                    try:
                        vrf.close()
                    except Exception:
                        pass
                    src = self._open_src()
                    vrf = open_source(self.source)
                    continue
                # 真坏区: 标NonTrimmed跳过, 重试遍再二分细抠
                self.map.mark(rpos, ln, NON_TRIMMED)
                self.map.current_pos = rpos + ln
                pos = rpos + ln
                continue
            if self._spot_ok(vrf, buf, rpos):
                self.dst.seek(rpos)
                self.dst.write(buf)
                self.map.mark(rpos, ln, FINISHED)
                self.map.current_pos = rpos + ln
            else:
                self.rereads += 1
                self._split_and_verify(src, vrf, rpos, ln)
                self.map.current_pos = rpos + ln
            pos = rpos + ln
            self._checkpoint(False)

            now = time.time()
            fin = self.map.totals()[FINISHED]
            if now - last_t >= 2:
                spd = (fin - last_fin) / (now - last_t) / 1024 / 1024
                t = self.map.totals()
                self.on_progress({
                    'event': 'progress', 'offset': fin, 'total': total,
                    'pct': round(fin * 100.0 / total, 1),
                    'speed_mbps': round(spd, 1),
                    'eta_min': int((total - fin) / max(spd * 1024 * 1024, 1) / 60),
                    'rereads': self.rereads,
                    'bad_regions': len(self.bad_regions),
                    'bad_bytes': t[BAD_SECTOR],
                    'io_retries': self.io_retries,
                })
                last_t, last_fin = now, fin

        # ---- 重试遍: 处理NonTrimmed/NonScraped ----
        if not self._cancel.is_set():
            while True:
                region = self.map.find_next(0, [NON_TRIMMED, NON_SCRAPED])
                if region is None:
                    break
                rpos, avail = region
                ln = min(self.chunk, avail)
                self._split_and_verify(src, vrf, rpos, ln)

        self._checkpoint(True)
        self.dst.close()
        vrf.close()
        src.close()
        t = self.map.totals()
        ev = {'event': 'done', 'total': total,
              'finished_bytes': t[FINISHED], 'bad_bytes': t[BAD_SECTOR],
              'rereads': self.rereads, 'bad_regions': self.bad_regions,
              'io_retries': self.io_retries,
              'speed_bins': self.speed_bins,
              'cancelled': self._cancel.is_set()}
        self.on_progress(ev)
        return ev


if __name__ == '__main__':
    import sys
    src, dst = sys.argv[1], sys.argv[2]
    img = VerifiedImager(src, dst, on_progress=lambda d: print(d, flush=True) if d['event'] in
                         ('progress', 'done', 'bad_region', 'resume', 'io_retry', 'resume_rejected') else None)
    print(img.run())
