# 数据抢救工作台 v1.2.0 发布说明

一个绿色便携的 Windows 数据恢复工具:从损坏的 U 盘/硬盘/监控盘/虚拟机盘/加密盘里抢回数据。
中文界面,双击即用,全程只读源盘。

## 上手方式

- **新手**:首页"你出了什么事?"按事故选一张卡,跟着步骤链走即可,不需要懂术语
- 看不懂某个功能:面板右上角"? 何时用"三行说清 何时用/怎么准备/怎么看结果
- 每次抢救的产出汇总在"成果总览"页,一键生成单文件 HTML 报告(可发他人)

## 主要能力(20+ 恢复通道)

- **镜像保现场**:校验式镜像(逐块双句柄三点抽检,坏区二分定位到扇区,断点续传);检测到主控"顺序读漂移"自动切换
- **按文件系统恢复**:NTFS(含纯MFT兜底,$Boot损坏也能救)/exFAT(含孤儿目录深挖,NameHash+SetChecksum双校验)/FAT32/FAT16/ext4(日志挖掘,已删文件+掉电现场)/HFS+(原名+路径)
- **签名雕刻**:94 种格式;未分配空间预筛(快+降误报)
- **监控视频**:H.264/H.265 裸流雕刻(SPS解析分辨率fps+MP4重封装)+MP4 moov 重建+海康 .dav/.mpg PS流剥离→裸流/MP4
- **BitLocker**:密码/48位恢复口令/清除密钥解锁,AES-XTS 整卷解密(密钥拉伸×1048576轮经dislocker/libbde双重校准)
- **虚拟机盘**:VMDK/VHD 检测与展开(动态盘/稀疏盘),解开后接全链路
- **RAID**:成员盘镜像自动检测(级别/条带/盘序/四布局按结构打分)+重组,RAID5 缺盘 XOR 降级重建
- **Outlook**:PST/OST 结构体检(孤儿邮件/缺块),头损兜底扫描;含 Outlook 2013+ 默认 4K 页格式
- **勒索现场**:家族识别+免费解密器线索(GandCrab 有免费解密器等)
- **内容取证**:EXIF时间归档/SQLite已删记录/Base64内嵌提取/**事件日志事件ID解析**(登录/USB接入/服务安装等30+常见事件,经真实evtx双库校准)/加密卷检测/CHK识别拆分
- **体检结论融合 SMART**:读取稳定但盘有物理病灶(重映射/待映射)时自动升级为校验式镜像建议

## 获取与安装

1. 从 [Releases](../../releases) 下载 `RescueWorkbench-v1.2.0-win64.zip` 并解压(文件夹整体保留)
2. 双击 `RescueWorkbench.exe`(UAC 提权——读物理盘需要)
3. 或者从源码运行:克隆本仓库,把 `sf-cli.exe` 与 `untrunc/smartctl`(见下)放进 `engine/bin/`,再 `run.bat`

> 源码仓库不含大二进制(engine/bin/ 与测试 VHD 夹具)。运行所需组件:
> - sf-cli.exe — [SectorForge Releases](https://github.com/terraseraph/sectorforge/releases) (MIT)
> - untrunc.exe + DLL — [untrunc Releases](https://github.com/anthwlock/untrunc/releases) (GPL-2.0)
> - smartctl.exe — [smartmontools](https://www.smartmontools.org/wiki/Download) (GPL-2.0+)

## 测试

17 套测试覆盖核心链路(合成夹具+真实 VHD+真实 evtx 校准):`python -I tests/run_all.py`
夹具重建脚本在 `tests/fixtures/README.md`。

## 已知限制(诚实清单)

- 海康监控专用 DHFS 文件系统暂不支持(.dav 剥离与裸流雕刻可用)
- BitLocker/exFAT 深挖/HFS+/PST 经合成样本验证,与真实盘的兼容性以实测为准
- H.264 重封装视频的 B 帧播放顺序可能不完美(裸流原件始终保留)
- APFS 仅识别,文件级恢复未实现
- RAID6/控制器私有偏移未覆盖

## 许可证

主程序源码: **Apache License 2.0**(见 LICENSE/NOTICE)。第三方组件各适用其许可(licenses/)。

## 免责声明

数据恢复存在进一步损坏的理论风险。本工具按只读源盘设计并尽力保护现场,
但对任何数据丢失不承担责任。重要数据请先做镜像备份;商业救援场景请咨询专业数据恢复服务。

## 开源致谢

见 [README](README.md) 与 `licenses/` 目录。GPL 组件以独立子进程方式调用。
