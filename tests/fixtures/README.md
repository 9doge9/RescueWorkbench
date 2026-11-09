# 测试夹具说明

## 现有夹具
- `ntfs.vhd` / `ntfs.manifest.json` — 真实Windows NTFS格式化的200MB VHD，含9个已知文件（中文/空格/深层路径），4个删除标记（del_root.txt、文档/del_in_sub.txt、暂存/整夹）
- `exfat.vhd` / `exfat.manifest.json` — 同结构 exFAT 版
- `src/` — 源文件（确定性内容，random.seed=20261105）

## 何时重造夹具
VHD损坏/manifest与内容不匹配/需要新场景时。

## 重造流程（血泪教训版，照抄别发挥）

**核心三律**：
1. PowerShell脚本必须**单个UTF-8 BOM**（重复添加=双BOM=静默不执行；无BOM+中文=GBK乱码解析错）
2. `diskpart`脚本里**不要写 `online disk`**——本机已联机会报错，diskpart遇错即中止后续行
3. cmd.bat / 传给diskpart的路径**必须纯ASCII**（中文路径会被GBK吞字符截断）；用junction桥接：`New-Item -ItemType Junction -Path C:\work\rfx -Target <fixtures目录>`（junction不需要提权）

**步骤**（需一次提权，用提权worker方式最稳）：
1. 启动 `worker.ps1`（提权，15分钟窗口，监听 `C:\rescue_ctl\cmd.bat`，输出到 `out.txt`）
2. 预先用bash写好diskpart脚本到 `C:\rescue_fix_tmp\`（`detach_*.txt`/`at*.txt`/`dp*.txt`）
3. 投放纯ASCII的 `cmd.bat`：attach→`xcopy C:\work\rfx\src\* R:\ /E /I /Y /Q`→`powershell -File C:\work\rfx\manifest_del.ps1 -Letter R -Tag ntfs -OutDir C:\work\rfx`→detach→`move` VHD到 `C:\work\rfx\`
4. exFAT同流程（`fs=exfat`、盘符S）
5. `python -I tests/test_e2e.py` 验证

**Get-DiskImage看不见diskpart方式挂的VHD**——残留清理必须用diskpart按文件detach，别用Dismount-DiskImage瞎清。

## 已知边界（观测值，不判FAIL）
- sf-cli对NTFS已删：整夹删除+小resident文件可能不列出（本夹具4个删除标记中0-1个可见）；exFAT已删4/4全恢复
