# -*- coding: utf-8 -*-
"""ext4 journal挖掘测试(合成夹具tests/ext4img.py):
A. 卷解析: 超级块/组描述符/inode读取
B. 活文件数据直读一致(live.txt/inner.bin)
C. journal走查: 1笔完整事务 + 1笔崩溃尾巴(无commit)
D. 挖掘: victim.txt(已删)有名+inode副本+extent; live.txt存活
E. 恢复: 主表路径(inode11)与journal副本路径(inode14)内容逐字节一致
F. sf-cli独立裁判: scan在合成盘上识别出ext分区(结构合法性交叉验证)
G. 健壮性: 垃圾目录块拒绝/非ext4拒绝
"""
import os
import sys
import tempfile
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'engine'))
sys.path.insert(0, HERE)

import ext4
import ext4img
import sfadapter

FAILS = []
LIVE = b'LIVEFILE' * 512
VICTIM = b'VICTIMDATA' * 934


def check(name, ok, detail=''):
    print(('PASS ' if ok else 'FAIL ') + name + (('  ' + detail) if detail else ''), flush=True)
    if not ok:
        FAILS.append(name)


def main():
    tmp = tempfile.mkdtemp(prefix='ext4_')
    try:
        img = os.path.join(tmp, 'ext4.img')
        facts = ext4img.build(img, LIVE, VICTIM)
        part = facts['part_lba']

        with open(img, 'rb') as f:
            vol = ext4.Vol(f, part)
            vol.gdt_load()
            check('卷解析', vol.bs == 4096 and vol.volume_name == 'testvol'
                  and vol.n_groups == 1 and vol.inode_size == 256)
            n11 = vol.read_inode(11)
            check('inode11 extent', n11['mode'] == 0x81A4 and n11['size'] == len(LIVE)
                  and n11['extents'] == [(0, 1159, 1)],
                  str((hex(n11['mode']), n11['size'], n11['extents'])))
            check('live数据一致', vol.read_file_bytes(n11) == LIVE)
            n13 = vol.read_inode(13)
            check('inner数据一致', vol.read_file_bytes(n13) ==
                  (b'INNERBLOB' * 910).ljust(8192, b'\x00'))
            check('victim主表已删', vol.read_inode(14)['mode'] == 0)

        # journal走查
        with open(img, 'rb') as f:
            vol = ext4.Vol(f, part)
            vol.gdt_load()
            jb = ext4.journal_blocks_of(vol)
            check('journal块序列', jb and len(jb) == 1024)
            rep = ext4.parse_journal(jb)
            check('1笔完整事务+1笔崩溃尾巴',
                  len(rep['transactions']) == 1 and len(rep['uncommitted']) == 1,
                  'tx=%d unc=%d' % (len(rep['transactions']), len(rep['uncommitted'])))
            tx = rep['transactions'][0]
            check('事务映射(fs块号)', [fs for fs, _ in tx['mapping']] ==
                  [facts['root_dir_block'], facts['inode_table_block']])

        res = ext4.mine_journal(img, part)
        check('挖掘出名字', {n['name'] for n in res['names']} >=
              {'live.txt', 'sub', 'victim.txt'}, str(res.get('names'))[:120])
        vic = next(n for n in res['names'] if n['name'] == 'victim.txt')
        check('victim已删+日志副本',
              vic['main_inode'] == '已删' and vic.get('journal_copy') and
              vic['journal_copy']['size'] == len(VICTIM),
              str(vic)[:140])
        lv = next(n for n in res['names'] if n['name'] == 'live.txt')
        check('live存活+日志副本', lv['main_inode'] == '存活' and lv.get('journal_copy'))

        out1 = os.path.join(tmp, 'victim.bin')
        got, want = ext4.recover_by_ino(img, part, 14, out1)
        check('victim经journal副本恢复(逐字节)',
              got == want == len(VICTIM) and open(out1, 'rb').read() == VICTIM,
              '%s/%s' % (got, want))
        out2 = os.path.join(tmp, 'live.bin')
        got2, want2 = ext4.recover_by_ino(img, part, 11, out2)
        check('live经主表恢复', open(out2, 'rb').read() == LIVE)

        # sf-cli独立裁判
        ev, rc = sfadapter.run(['scan', '--source', img])
        parts = (ev or {}).get('partitions') or []
        check('sf-cli识别ext分区(交叉验证)',
              any((p.get('filesystem') or '').startswith('ext') for p in parts),
              str([(p.get('filesystem'), p.get('start_lba')) for p in parts]))

        # 健壮性
        check('垃圾目录块拒绝', ext4.parse_dir_block(os.urandom(4096)) == []
              or len(ext4.parse_dir_block(os.urandom(4096))) <= 3)
        junk = os.path.join(tmp, 'junk.img')
        open(junk, 'wb').write(os.urandom(8192))
        try:
            ext4.Vol(open(junk, 'rb'), 0)
            check('非ext4拒绝', False)
        except ValueError:
            check('非ext4拒绝', True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print('\n%s: %d项失败' % ('总判定' if FAILS else '全绿', len(FAILS)), flush=True)
    sys.exit(1 if FAILS else 0)


if __name__ == '__main__':
    main()
