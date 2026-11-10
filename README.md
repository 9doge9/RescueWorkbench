# 数据抢救工作台

Windows 数据恢复工具。U盘打不开、文件误删、监控录像看不了、盘被 BitLocker 锁住、NAS 阵列盘单块读不出，这些情况都能试着救。中文界面，解压双击就能用。

源盘全程只读，恢复结果写到别的盘。

> **English speakers**: a Chinese-first portable Windows data-recovery toolkit (verified imaging, NTFS/exFAT/ext4/HFS+ recovery, video repair, BitLocker, RAID). Chinese is not required: any MCP-capable agent (Claude, Codex, Cursor, Trae, ZCode) can drive it — see [AGENT.md](AGENT.md). Portable build in [Releases](../../releases).

## 怎么用

双击打开，首页选你遇到的情况，比如"U盘提示要格式化"，跟着步骤走。哪一步看不明白，面板右上角有个"何时用"，点开是两三行说明。

不想自己动手就交给 AI。Claude、Codex、Cursor、Trae、ZCode 这类工具挂上 `mcp_server.py`（配置见 [AGENT.md](AGENT.md)），直接说"我的U盘打不开，帮我救回照片"就行。

两种方式安全机制一样：源盘只读，输出不落源盘。

## 能做什么

| 遇到的情况 | 对应能力 |
|---|---|
| 盘快坏了 | 校验式镜像，坏区定位到扇区、断点续传，之后都在镜像上做 |
| 误删文件 | NTFS/exFAT/FAT32 按原名恢复，exFAT 支持已删目录深挖 |
| 文件系统损坏 | 94 种格式签名雕刻；NTFS 的 $Boot 坏了用 MFT 兜底 |
| 监控录像 | 裸流雕刻、断电 MP4 修复、海康 .dav 剥离转 MP4 |
| BitLocker | 密码或 48 位恢复口令解锁，解密成可挂载镜像 |
| 虚拟机盘 | VMDK/VHD 展开，之后当普通盘处理 |
| RAID 阵列 | 自动检测级别/条带/盘序并重组，RAID5 缺一块盘也能降级重建 |
| Mac/Linux 盘 | HFS+ 按原名恢复；ext4 日志挖已删文件和掉电现场；APFS 暂只识别 |
| Outlook | PST/OST 体检，文件头损坏也能兜底扫出邮件清单 |
| 勒索加密 | 识别家族，查有没有免费解密器 |
| 取证 | 事件日志（登录/USB/服务安装）、SQLite 已删记录、EXIF 归档 |

## 快速开始

1. 下载解压，双击 `RescueWorkbench.exe`，UAC 点"是"（读物理盘需要管理员）
2. 按首页指引走
3. 一条铁律：怀疑盘要坏，先做镜像，后面全对着镜像做

## 使用原则

- 输出不落在源盘（同盘直接拒绝）
- 恢复结果按完整/部分/无数据如实标注
- 检测到主控"顺序读漂移"自动改用校验式镜像
- 中断后重跑同一任务可续传

## 已知限制

- 海康 DHFS 不支持（.dav 剥离和裸流雕刻可用）
- BitLocker、exFAT 深挖、HFS+、PST 目前经合成样本验证，真实盘兼容性以实测为准
- H.264 重封装的 B 帧顺序可能不完美，裸流原件保留
- APFS 只识别，不能恢复
- 需要 Windows 10 及以上，64 位

## 免责声明

数据恢复是在坏介质上抢读数据，存在进一步损坏的理论风险。本工具按只读源盘设计，但对任何数据丢失不承担责任。重要数据请先做镜像备份；商业救援请咨询专业数据恢复服务。

## 许可证

主程序源码 Apache License 2.0（见 [LICENSE](LICENSE) 与 [NOTICE](NOTICE)），随附组件各适用其许可（`licenses/` 目录）。

## 开源致谢

- [SectorForge](https://github.com/terraseraph/sectorforge)（MIT）— 恢复引擎
- [untrunc](https://github.com/anthwlock/untrunc)（GPL-2.0）— MP4 修复
- [smartmontools](https://www.smartmontools.org/)（GPL-2.0+）— SMART 查询
- [pywebview](https://github.com/r0x0r/pywebview)（BSD-3）— 窗口框架
- Python（PSF）、PyInstaller（GPL + bootloader 例外）

GPL 组件以独立子进程方式调用，源码可从各自官方仓库获取。

## 开发

```bash
python server.py 8799          # 开发模式起服务
python -I tests/run_all.py     # 18 套测试
```

运行所需三个二进制（sf-cli / untrunc / smartctl）的下载指引见 [RELEASE_NOTES.md](RELEASE_NOTES.md)。

## AI 和脚本接入

`GET /api/schema` 返回全部任务类型和参数。支持 MCP 的客户端挂 `mcp_server.py`，每个恢复通道是一个工具。见 [AGENT.md](AGENT.md)。

## 支持作者

工具免费开源。如果它帮您救回过重要数据，可以扫码请作者喝杯咖啡：

<div align="center">
  <img src="docs/donate.jpg" alt="支持作者" width="220">
</div>
