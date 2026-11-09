# -*- coding: utf-8 -*-
"""合并五个恢复目录: 按SHA256去重, BAD文件单独隔离, 按类型归档到 最终文件/"""
import os, sys, csv, hashlib, shutil, json

TYPE_DIR = {
    'jpg': '照片', 'png': '照片', 'tiff': '照片', 'gif': '照片', 'bmp': '照片', 'webp': '照片',
    'mp4': '视频', 'mov': '视频', 'avi': '视频',
    'docx': '文档', 'doc': '文档', 'pdf': '文档', 'pptx': '文档', 'xlsx': '文档', 'txt': '文档', 'csv': '文档',
    'zip': '压缩包', 'rar': '压缩包', '7z': '压缩包',
    'mp3': '音频', 'wav': '音频', 'm4a': '音频',
    'psd': '其他', 'wmf': '其他', 'sqlite': '其他',
}

def sha256(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        while True:
            b = f.read(4*1024*1024)
            if not b: break
            h.update(b)
    return h.hexdigest()

def main(sources, dst):
    seen = {}
    stats = {}
    manifest = []
    bad_files = []
    for src in sources:
        vc = os.path.join(src, 'verified_summary.csv')
        badset = set()
        if os.path.exists(vc):
            for row in csv.reader(open(vc, encoding='utf-8-sig')):
                if len(row) >= 3 and row[2] == 'BAD':
                    badset.add(row[0])
        for fn in os.listdir(src):
            p = os.path.join(src, fn)
            if not os.path.isfile(p) or fn in ('report.csv', 'verified_summary.csv'): continue
            ext = fn.lower().rsplit('.', 1)[-1] if '.' in fn else 'bin'
            h = sha256(p)
            if h in seen: continue
            seen[h] = fn
            sub = TYPE_DIR.get(ext, '其他')
            if fn in badset:
                sub = '待检查-损坏'
            d = os.path.join(dst, sub)
            os.makedirs(d, exist_ok=True)
            target = os.path.join(d, fn)
            if os.path.exists(target):   # 同名不同hash: 加前缀
                target = os.path.join(d, h[:10] + '_' + fn)
            shutil.copyfile(p, target)
            key = sub
            stats[key] = stats.get(key, 0) + 1
            manifest.append([sub, fn, os.path.getsize(p), h])
    with open(os.path.join(dst, '恢复清单.csv'), 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(['分类', '文件', '大小', 'SHA256'])
        w.writerows(manifest)
    print('== 合并统计 ==')
    for k, v in sorted(stats.items(), key=lambda x: -x[1]):
        print(f'{k:10s} {v}')
    print(f'合计 {len(manifest)} 个文件 (已按内容去重)')

if __name__ == '__main__':
    dst = sys.argv[1]
    main(sys.argv[2:], dst)
