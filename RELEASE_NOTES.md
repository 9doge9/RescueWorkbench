# 数据抢救工作台 发布说明

Windows 数据恢复工具，中文界面，解压双击就能用，源盘全程只读。

## 下载

Releases 页下载 zip 解压，双击 RescueWorkbench.exe（UAC 点"是"）。文件夹整体保留，缺一不可。

源码运行需另放 sf-cli.exe / untrunc / smartctl 三个文件到 engine/bin/，下载地址见仓库 README。

## 功能清单

- 校验式镜像：坏区定位到扇区、断点续传、主控"顺序读漂移"检测
- 文件系统恢复：NTFS（含 $Boot 损坏后的 MFT 兜底）、exFAT（已删目录深挖）、FAT32/16、ext4（日志挖已删文件）、HFS+
- 签名雕刻：94 种格式，可只在未分配空间扫
- 监控视频：H.264/H.265 裸流雕刻、断电 MP4 修复、海康 .dav 剥离转 MP4
- BitLocker：密码/恢复口令/清除密钥解锁，AES-XTS 整卷解密
- 虚拟机盘：VMDK/VHD 展开
- RAID：级别/条带/盘序自动检测，RAID5 缺盘降级重建
- Outlook：PST/OST 体检与头损兜底
- 勒索现场：家族识别、免费解密器线索
- 取证：事件日志事件 ID、SQLite 已删记录、EXIF 归档、Base64 内嵌、加密卷检测、CHK 拆分
- AI/脚本：GET /api/schema 自描述，MCP 客户端可直接挂载（见 AGENT.md）

## 已知限制

- 海康 DHFS 不支持；APFS 只识别
- BitLocker、exFAT 深挖、HFS+、PST 经合成样本验证，真实盘以实测为准
- H.264 重封装 B 帧顺序可能不完美，裸流原件保留
- RAID6、控制器私有偏移未覆盖
- 需要 Windows 10+ 64 位

## 测试

18 套测试：`python -I tests/run_all.py`。另有压力审计脚本 `tests/stress_audit.py`（并发、双实例、取消、截断输入、中文路径）。

## 许可证

主程序 Apache License 2.0，随附组件见 licenses/。免责声明见 README。
