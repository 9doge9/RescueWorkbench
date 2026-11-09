# -*- coding: utf-8 -*-
"""能力自描述 — /api/schema 的数据源, 也是 MCP 工具表的数据源
给AI Agent/脚本用: 有哪些任务类型、参数怎么填、结果长什么样、场景该怎么编排。
新增runner时同步在这里登记(sanity: server启动时对照pipeline.RUNNERS告缺)。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import version

# 通用参数说明(避免重复)
_P = {
    'source': ('string', True, '数据源: 设备路径(\\\\.\\PhysicalDrive1)或镜像/分区文件路径'),
    'out': ('string', False, '输出目录(不存在会创建)'),
    'partition_lba': ('integer', False, '分区起始LBA(扇区); 缺省0; 深挖类任务缺省时会自动按签名定位'),
    'mapfile': ('string', False, 'ddrescue兼容map文件路径(有则只处理非坏区)'),
}

KINDS = {
    'probe': {'name': '读稳定性体检', 'params': {
        'source': _P['source']},
        'returns': 'stable/stream_drift/repeat_unstable/repeat_points/recommend',
        'desc': '判断盘能不能安全读: 定点重复读+流式vs定点比对, 检测"主控说谎"(顺序读漂移)'},
    'image': {'name': '整盘镜像', 'params': {
        'source': _P['source'], 'dest': ('string', True, '镜像输出文件路径'),
        'mode': ('string', False, "fast(两遍ddrescue式)|verified(校验式,坏盘必选), 默认fast"),
        'mapfile': _P['mapfile'], 'access': ('string', False, 'IO访问方式, 漂移盘可设scsi'),
        'reverse': ('boolean', False, '反向镜像(躲开头部坏区)'), 'retry_only': ('boolean', False, '只重试map里的坏区')},
        'returns': 'finished_bytes/bad_bytes/rereads/sha256',
        'desc': '把源盘复制成镜像文件, 之后一切恢复操作都在镜像上做(保护源盘)'},
    'scan': {'name': '分区扫描', 'params': {
        'source': _P['source']},
        'returns': 'partitions[{filesystem,start_lba,size_sectors,confidence}]',
        'desc': '识别源上的分区与文件系统(NTFS/exFAT/FAT/ext4/HFS+/BitLocker...)'},
    'files': {'name': '列出文件', 'params': {
        'source': _P['source'], 'partition_lba': _P['partition_lba'],
        'deleted_only': ('boolean', False, '只列已删(大卷更快)')},
        'returns': 'files[{id,name,path,size,deleted,recoverable}]/files_total/deleted',
        'desc': '按文件系统列文件(含已删), 供extract按id提取'},
    'extract': {'name': '提取文件', 'params': {
        'source': _P['source'], 'partition_lba': _P['partition_lba'],
        'ids': ('string', True, '要提取的文件id, 逗号分隔(来自files结果)'),
        'out': _P['out']},
        'returns': 'extracted/failed/out',
        'desc': '把files列出的文件按id恢复到输出目录(自动剥离NTFS孤儿后缀)'},
    'carve': {'name': '签名雕刻', 'params': {
        'source': _P['source'], 'out': _P['out'],
        'types': ('string', False, '格式类型过滤, 逗号分隔'),
        'start': ('integer', False, '扫描起始字节'),
        'end': ('integer', False, '扫描结束字节'), 'mapfile': _P['mapfile']},
        'returns': 'files_found',
        'desc': '不依赖文件系统, 按94种格式签名直接捞文件(无原名, 按类型落盘)'},
    'carve_free': {'name': '未分配空间雕刻', 'params': {
        'source': _P['source'], 'partition_lba': _P['partition_lba'],
        'out': _P['out'], 'types': ('string', False, '格式类型过滤'), 'mapfile': _P['mapfile']},
        'returns': 'files_found/ranges/bytes_scanned/fs',
        'desc': '先解析分配图(NTFS $Bitmap/FAT表)只在空闲区间雕刻——更快且降误报'},
    'mftscan': {'name': '纯MFT兜底扫描', 'params': {
        'source': _P['source']},
        'returns': 'mode/records/files[{name,path,size,deleted}]',
        'desc': 'NTFS的$Boot损坏时的兜底: 全盘搜FILE签名重建文件树'},
    'h264carve': {'name': '监控裸流雕刻', 'params': {
        'source': _P['source'], 'out': _P['out'],
        'start': ('integer', False, '起始字节'), 'end': ('integer', False, '结束字节'),
        'min_kb': ('integer', False, '最小段大小KB, 默认64')},
        'returns': 'found/bytes/out',
        'desc': '扫H.264/H.265裸码流(无容器的监控数据), 出裸流原件+重封装MP4'},
    'davstrip': {'name': '海康.dav剥离', 'params': {
        'source': _P['source'], 'out': _P['out']},
        'returns': 'raw/mp4/codec/width/height/fps/video_pes',
        'desc': '海康.dav/.mpg是MPEG-PS包装: 剥掉pack/PES壳得到标准裸流并转MP4'},
    'mp4repair': {'name': 'MP4修复', 'params': {
        'reference': ('string', True, '同设备完好视频路径(参数参考)'),
        'corrupt': ('string', False, '损坏MP4路径(单文件模式)'),
        'directory': ('string', False, '目录(批量模式, 自动找损坏MP4)'),
        'output': ('string', False, '修复输出路径')},
        'returns': 'ok/total/results[{file,ok,output}]',
        'desc': '断电MP4缺moov索引: 用同设备完好视频作参考重建索引(untrunc引擎)'},
    'exfatdeep': {'name': 'exFAT已删深挖', 'params': {
        'source': _P['source'], 'partition_lba': _P['partition_lba'],
        'extract': ('boolean', False, 'true=全部落盘'), 'out': _P['out']},
        'returns': 'deleted/orphans/records/saved[{file,size,mode}]',
        'desc': '全簇扫描exFAT目录项: 挖已删条目集与孤儿目录(NameHash+SetChecksum双校验)'},
    'ext4journal': {'name': 'ext4日志挖掘', 'params': {
        'source': _P['source'], 'partition_lba': _P['partition_lba'],
        'recover': ('array', False, "要恢复的inode号列表, 或'*'(全部有日志副本的已删文件)"),
        'out': _P['out']},
        'returns': 'volume_name/transactions/uncommitted/names[{journal_ino,name,size}]/recovered',
        'desc': '走JBD2日志找删除前的名字与数据(inode副本直读), 崩溃尾巴=掉电现场证据'},
    'hfslist': {'name': 'Mac HFS+恢复', 'params': {
        'source': _P['source'], 'partition_lba': _P['partition_lba'],
        'extract': ('boolean', False, 'true=全部提取'), 'out': _P['out']},
        'returns': 'count/folders/files[{path,size}]/saved',
        'desc': 'HFS+ catalog B树遍历(原名+路径提取); APFS仅识别不支持'},
    'blunlock': {'name': 'BitLocker解锁', 'params': {
        'source': _P['source'], 'partition_lba': _P['partition_lba'],
        'password': ('string', False, '解锁密码(与recovery二选一)'),
        'recovery': ('string', False, '48位恢复口令(横杠可有可无)'),
        'out': _P['out']},
        'returns': 'raw/via/algo/volume_size',
        'desc': '解锁并AES-XTS整卷解密成raw镜像, 自动设为当前数据源接全链路'},
    'vmexpand': {'name': '虚拟磁盘展开', 'params': {
        'source': _P['source'], 'out': _P['out']},
        'returns': 'kind/raw/virtual_size',
        'desc': 'VMDK/VHD检测与展开(fixed/flat免展开直接用), 展开后当普通盘恢复'},
    'raidrebuild': {'name': 'RAID重组', 'params': {
        'members': ('array', True, '成员盘镜像路径列表(按盘序; 缺失盘用空字符串占位)'),
        'out': _P['out'],
        'cfg': ('object', False, '手动指定{level,stripe_kb,order,rotation}; 缺省自动检测')},
        'returns': 'level/stripe_kb/raw/virtual_size/candidates',
        'desc': '自动检测RAID0/1/5参数并重组; RAID5缺盘可XOR降级重建'},
    'pstcheck': {'name': 'PST/OST体检', 'params': {
        'source': _P['source']},
        'returns': 'index_type/healthy/recovered_messages',
        'desc': 'Outlook邮件库结构体检; 头损自动兜底扫描直接捞邮件(含4K页格式)'},
    'wxdat': {'name': '微信dat解密', 'params': {
        'dir': ('string', True, 'dat文件目录'), 'out': _P['out']},
        'returns': 'ok/skip/fail/key/exts',
        'desc': '批量解密微信图片dat(自动探测密钥)'},
    'exifscan': {'name': 'EXIF整理', 'params': {
        'dir': ('string', True, '已恢复文件目录'), 'out': _P['out'],
        'mode': ('string', False, "organize=按拍摄时间归档, 默认只扫描")},
        'returns': 'photos/with_exif/organized/results[{file,exif}]',
        'desc': '解析照片EXIF(拍摄时间/相机/GPS), 可按年月归档重命名'},
    'b64scan': {'name': 'Base64内嵌提取', 'params': {
        'dir': ('string', True, '已恢复文件目录'), 'out': _P['out']},
        'returns': 'files[{file,hits[{ext,size}]}]/found',
        'desc': '从文本/日志里提取Base64编码的内嵌文件(图片/压缩包), 多层嵌套也能剥'},
    'sqlscan': {'name': 'SQLite残留', 'params': {
        'dir': ('string', False, '已恢复文件目录(找.db文件)'),
        'source': _P['source'], 'start': ('integer', False, '镜像扫描起始字节'),
        'end': ('integer', False, '镜像扫描结束字节')},
        'returns': 'scanned/dbs[{path,tables,freelist_pages}]/free_fragments',
        'desc': 'SQLite库体检+自由页已删数据挖掘; 或对镜像整段雕刻库'},
    'evlogscan': {'name': '事件日志解析', 'params': {
        'dir': ('string', True, '含.evtx文件的目录(从C:\\Windows\\System32\\winevt\\Logs拷出)')},
        'returns': 'logs[{file,records,events[{id,name,count}],notable[(time,rid,eid,name)]}]',
        'desc': 'evtx取证时间线+常见事件ID对照(登录/USB/服务安装等, 勒索用7045)'},
    'aesdetect': {'name': '加密检测', 'params': {
        'source': _P['source'], 'offset': ('integer', False, '起始字节'), 'end': ('integer', False, '结束字节')},
        'returns': 'volume/high_pct/high_regions/max_entropy',
        'desc': '检测BitLocker/LUKS卷头与高熵加密区(判断数据是否被加密锁死)'},
    'ransomscan': {'name': '勒索现场检测', 'params': {
        'dir': ('string', True, '受害文件所在目录')},
        'returns': 'suspicion/family/family_decryptor/evidence/advice',
        'desc': '三路证据投票(勒索信/异常扩展名/高熵)识别家族, 给免费解密器线索'},
    'chkscan': {'name': 'CHK识别', 'params': {
        'dir': ('string', True, 'FOUND.00X目录'),
        'extract': ('boolean', False, 'true=拆分提取'), 'out': _P['out']},
        'returns': 'total/identified/results[{file,parts[{type,size}]}]',
        'desc': 'CHKDSK产物FILE0000.CHK按魔数识别真身并拆分(JPG/ZIP/PDF/MP4...)'},
    'report': {'name': 'HTML报告', 'params': {
        'out': ('string', True, '报告输出.html路径'),
        'dev_model': ('string', False, '设备型号(展示用)'),
        'probe': ('object', False, 'probe任务的done结果'),
        'image': ('object', False, 'image任务done结果'),
        'scan': ('array', False, 'scan任务的partitions'),
        'files': ('object', False, 'files任务done结果摘要{files_total,deleted}'),
        'extract': ('object', False, 'extract任务done结果{extracted,failed,out}'),
        'exfatdeep': ('object', False, 'exfatdeep任务done结果摘要'),
        'ext4journal': ('object', False, 'ext4journal任务done结果摘要'),
        'blunlock': ('object', False, 'blunlock任务done结果'),
        'raidrebuild': ('object', False, 'raidrebuild任务done结果'),
        'hfslist': ('object', False, 'hfslist任务done结果摘要'),
        'pstcheck': ('object', False, 'pstcheck任务done结果'),
        'ransomscan': ('object', False, 'ransomscan任务done结果'),
        'evlogscan': ('object', False, 'evlogscan任务done结果(可只传logs数组)')},
        'returns': 'out',
        'desc': '汇总各通道结果生成单文件HTML报告(缺省的通道不渲染)'},
    'hash': {'name': 'SHA-256校验', 'params': {
        'source': _P['source']},
        'returns': 'sha256',
        'desc': '对镜像/文件算SHA-256'},
    'verify': {'name': '验证分类', 'params': {
        'dir': ('string', True, '已恢复文件目录')},
        'returns': 'summary',
        'desc': '对恢复结果做完整性验证分类(zip/office/图片...)'},
}

# 场景决策表(与首页场景卡同源, 面向AI的任务编排)
SCENARIOS = [
    {'situation': 'U盘/移动硬盘打不开·提示格式化',
     'steps': ['probe', 'image(读不稳用verified)', 'scan', 'files+extract']},
    {'situation': '文件被误删/清空',
     'steps': ['scan', 'files(deleted_only=true)+extract', 'carve_free兜底', 'exfatdeep(若exFAT)']},
    {'situation': '监控硬盘/录像看不了',
     'steps': ['davstrip(.dav文件)', 'mp4repair(断电MP4)', 'h264carve(无容器裸流)']},
    {'situation': '盘被BitLocker锁住',
     'steps': ['blunlock(password或recovery)', 'scan+files+extract(对解密出的raw)']},
    {'situation': 'Mac电脑的盘',
     'steps': ['hfslist', 'carve兜底']},
    {'situation': '文件全变乱码·疑似勒索',
     'steps': ['ransomscan(先!别急着重装)', 'aesdetect', '有免费解密器先试官方']},
    {'situation': '虚拟机盘',
     'steps': ['vmexpand', 'scan+files+extract(对raw)']},
    {'situation': 'NAS/服务器阵列',
     'steps': ['raidrebuild', 'ext4journal(若ext4)', 'scan+files+extract(对重组raw)']},
]


def get_schema():
    return {
        'tool': '数据抢救工作台 RescueWorkbench',
        'version': version.VERSION,
        'safety': [
            '源盘全程只读(设计保证)', '输出目录与源同盘会被拒绝(同盘写保护)',
            '黄金法则: 怀疑盘要坏→先image, 之后所有任务对镜像做',
        ],
        'workflow': {
            'start': 'POST /api/job {"kind": "...", "params": {...}} → 返回{id}',
            'poll': 'GET /api/job/<id> → state: pending/running/done/failed/cancelled, done时带result',
            'stream': 'GET /api/events/<id> (SSE, 可选; progress/phase事件)',
            'cancel': 'POST /api/job/<id>/cancel',
            'finish': '长任务(镜像)断了可重跑同参数续传',
        },
        'endpoints': {
            'GET /api/devices': '设备列表',
            'GET /api/survey': '全盘快扫',
            'GET /api/smart?disk=N': 'SMART',
            'GET /api/findpart?src=&want=': '轻量分区定位(want=exfat/ext/bitlocker...)',
            'GET /api/allocmap?src=&lba=': '未分配空间图',
            'GET /api/vmdetect?src=': '虚拟磁盘检测',
            'GET /api/mapfile?path=': '镜像进度map',
            'GET /api/harvest': '全部产出盘点',
            'GET /api/jobs': '任务历史',
            'GET /api/version': '版本',
            'POST /api/open {path}': '资源管理器打开',
        },
        'kinds': {k: {
            'name': v['name'], 'desc': v['desc'], 'returns': v['returns'],
            'params': {pk: {'type': pv[0], 'required': pv[1], 'desc': pv[2]}
                       for pk, pv in v['params'].items()},
        } for k, v in KINDS.items()},
        'scenarios': SCENARIOS,
    }


def missing_in_schema():
    """server启动时对照pipeline.RUNNERS, 报告没登记进schema的任务类型"""
    try:
        import pipeline
        return sorted(set(pipeline.RUNNERS) - set(KINDS))
    except Exception:
        return []
