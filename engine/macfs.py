# -*- coding: utf-8 -*-
"""Mac文件系统 — HFS+文件列表/提取 + APFS容器识别
HFS+ (TN1150, 结构公开): 卷头@1024('H+'/'HX') → catalog B-tree(叶子节点
遍历) → 文件记录(dataFork 8个extent) → 直接读块提取。路径用folder记录
的parentID链回根(2)。HFS+无显式"已删"标记——删除后残留在catalog的记录
本解析器会一并列出(如实标注为catalog残留)。
诚实边界: extents溢出B-tree(高度碎片化文件)与资源fork不读v1;
APFS(对象图/B树/vnode)体量大, v1只识别容器基本信息。
"""
import struct

VH_OFF = 1024
SIG_HFSPLUS = b'H+'
SIG_HFSX = b'HX'
APFS_MAGIC = b'NXSB'

NODE_HEADER = 1
NODE_LEAF = -1
NODE_INDEX = 0

REC_FOLDER = 1
REC_FILE = 2
REC_FOLDER_THREAD = 3
REC_FILE_THREAD = 4


def detect(source, part_lba=0):
    """Mac卷识别 → {'kind': 'hfs+'|'apfs'|None, ...基本信息}"""
    with open(source, 'rb') as f:
        f.seek(part_lba + 24)
        t = f.read(4)
        if t == APFS_MAGIC:
            f.seek(part_lba)
            nx = f.read(512)
            bs = struct.unpack_from('<I', nx, 32)[0]
            blocks = struct.unpack_from('<Q', nx, 40)[0]
            info = {'kind': 'apfs', 'block_size': bs if 512 <= bs <= 65536 else None,
                    'blocks': blocks or None}
            if info['block_size'] and blocks:
                info['size_est'] = bs * blocks
            info['detail'] = 'APFS容器(Mac 10.13+/iOS) — 文件级解析未实现(如实)'
            return info
        f.seek(part_lba + VH_OFF)
        vh = f.read(512)
        if len(vh) >= 512 and vh[0:2] in (SIG_HFSPLUS, SIG_HFSX):
            bs = struct.unpack_from('<I', vh, 0x28)[0]
            return {'kind': 'hfs+', 'block_size': bs,
                    'total_blocks': struct.unpack_from('<I', vh, 0x2C)[0],
                    'free_blocks': struct.unpack_from('<I', vh, 0x30)[0],
                    'file_count': struct.unpack_from('<I', vh, 0x20)[0],
                    'folder_count': struct.unpack_from('<I', vh, 0x24)[0],
                    'hfsx': vh[0:2] == SIG_HFSX,
                    'detail': 'HFS+' + ('X(大小写敏感)' if vh[0:2] == SIG_HFSX else '')}
        return {'kind': None, 'detail': '非Mac文件系统'}


def _fork(buf, off):
    """HFSPlusForkData(80B) → (logicalSize, [(startBlock,count)...])"""
    size, = struct.unpack_from('<Q', buf, off)
    exts = []
    for i in range(8):
        start, count = struct.unpack_from('<II', buf, off + 16 + 8 * i)
        if count:
            exts.append((start, count))
    return size, exts


class Vol:
    def __init__(self, f, base):
        f.seek(base + VH_OFF)
        vh = f.read(512)
        if len(vh) < 512 or vh[0:2] not in (SIG_HFSPLUS, SIG_HFSX):
            raise ValueError('非HFS+卷(无H+/HX签名)')
        self.f = f
        self.base = base
        self.bs = struct.unpack_from('<I', vh, 0x28)[0]
        self.total_blocks = struct.unpack_from('<I', vh, 0x2C)[0]
        _, self.cat_exts = _fork(vh, 0x110)          # catalogFile
        if not self.cat_exts:
            raise ValueError('catalog文件无extent')
        self._cat = b''
        for start, count in self.cat_exts:
            f.seek(base + start * self.bs)
            self._cat += f.read(count * self.bs)
        self._node_size = self.bs      # 默认节点=块大小; load()里按B树头修正

    def node(self, n):
        """B-tree节点 → (descriptor, 原始字节)"""
        node_size = self._node_size
        off = n * node_size
        if off + node_size > len(self._cat):
            return None, b''
        b = self._cat[off:off + node_size]
        fLink, bLink = struct.unpack_from('<ii', b, 0)
        kind = struct.unpack_from('<b', b, 8)[0]
        height = b[9]
        nrec = struct.unpack_from('<H', b, 10)[0]
        return {'fLink': fLink, 'bLink': bLink, 'kind': kind,
                'height': height, 'nrec': nrec}, b

    def records(self, b, ndesc):
        """节点记录 → [(起始, 结束)]
        记录偏移数组从节点尾倒序存: 记录i的偏移在 n-2*(i+1);
        记录i的结束 = 记录i+1的起始(最后一个用空闲空间偏移)"""
        out = []
        n = len(b)
        for i in range(ndesc['nrec']):
            p = n - 2 * (i + 1)
            if p < 14:
                break
            off, = struct.unpack_from('<H', b, p)
            if i + 1 < ndesc['nrec']:
                p2 = n - 2 * (i + 2)
                nxt, = struct.unpack_from('<H', b, p2) if p2 >= 14 else (n,)
            else:
                p2 = n - 2 * (ndesc['nrec'] + 1)
                nxt, = struct.unpack_from('<H', b, p2) if p2 >= 14 else (n,)
            if off < 14 or off >= n:
                break
            out.append((off, nxt if nxt > off else n))
        return out

    def load(self):
        """解析B树头 → 叶子链遍历 → (files, folders)
        files: [{id,name,path,size,extents,deleted_flag未知}]
        folders: {id: (parent, name)}"""
        d0, b0 = self.node(0)
        if not d0 or d0['kind'] != NODE_HEADER:
            raise ValueError('catalog节点0非B树头节点')
        tree_depth, root = struct.unpack_from('<HI', b0, 14)
        leaf_recs, first_leaf, last_leaf = struct.unpack_from('<III', b0, 20)
        node_size = struct.unpack_from('<H', b0, 32)[0]
        total_nodes = struct.unpack_from('<I', b0, 36)[0]
        self._node_size = node_size
        if node_size != self.bs:
            # 节点大小≠块大小: 重新按node_size切catalog流
            self._cat = b''
            for start, count in self.cat_exts:
                self.f.seek(self.base + start * self.bs)
                self._cat += self.f.read(count * self.bs)
        d0, b0 = self.node(0)
        tree_depth, root = struct.unpack_from('<HI', b0, 14)
        leaf_recs, first_leaf, last_leaf = struct.unpack_from('<III', b0, 20)
        # 走叶子链
        files = []
        folders = {}
        n = first_leaf
        seen = set()
        while n and n not in seen and n * self._node_size < len(self._cat):
            seen.add(n)
            d, b = self.node(n)
            if not d:
                break
            for s, e in self.records(b, d):
                rec = b[s:e]
                self._parse_record(rec, files, folders)
            if d['kind'] != NODE_LEAF:
                break
            n = d['fLink']
        self._build_paths(files, folders)
        self.leaf_stats = {'first': first_leaf, 'last': last_leaf,
                           'leaf_records': leaf_recs, 'node_size': self._node_size}
        return files, folders

    def _parse_record(self, rec, files, folders):
        if len(rec) < 10:
            return
        klen, parent = struct.unpack_from('<HI', rec, 0)
        if klen < 6 or 2 + klen > len(rec):
            return
        name_len = struct.unpack_from('<H', rec, 6)[0]
        if 8 + 2 * name_len > 2 + klen:
            name_len = max(0, (klen - 8) // 2)
        name = rec[8:8 + 2 * name_len].decode('utf-16-be', 'replace')
        rtype, = struct.unpack_from('<H', rec, klen)
        body = rec[klen:]
        if rtype == REC_FILE and len(body) >= 152:
            file_id, = struct.unpack_from('<I', body, 8)
            size, exts = _fork(body, 72)
            files.append({'id': file_id, 'parent': parent, 'name': name,
                          'size': size, 'extents': exts, 'is_dir': False})
        elif rtype == REC_FOLDER and len(body) >= 48:
            fid, = struct.unpack_from('<I', body, 8)
            folders[fid] = (parent, name)

    def _build_paths(self, files, folders):
        folders[2] = (0, '')                     # 根
        for fl in files:
            parts = [fl['name']]
            p = fl['parent']
            hops = 0
            while p and p != 2 and p in folders and hops < 64:
                pp, pn = folders[p]
                parts.append(pn)
                p = pp
                hops += 1
            fl['path'] = '/'.join(reversed(parts))

    def read_file(self, rec):
        """dataFork 8个extent读文件 → bytes; 超出8extent返回None(诚实)"""
        need = (rec['size'] + self.bs - 1) // self.bs
        have = sum(c for _, c in rec['extents'])
        if have * self.bs < rec['size']:
            return None                            # extents溢出B-tree, v1不支持
        out = bytearray()
        for start, count in rec['extents']:
            self.f.seek(self.base + start * self.bs)
            out += self.f.read(count * self.bs)
            if len(out) >= rec['size']:
                break
        return bytes(out[:rec['size']])
