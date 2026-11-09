# AGENT.md — 给AI Agent/脚本的接入指南

数据抢救工作台是一个本地HTTP服务(默认 `http://127.0.0.1:8765`, 端口以启动横幅为准), 全部能力通过JSON API暴露, 无需鉴权(仅本机监听)。

## 30秒上手

```
GET  /api/schema                          # ← 先拿这个: 全部任务类型/参数/返回值/场景决策表
POST /api/job  {"kind":"scan","params":{"source":"D:\\disk.img"}}   # 起任务 → {"id":"..."}
GET  /api/job/<id>                        # 轮询: state=done时带result; failed带error
POST /api/job/<id>/cancel                 # 取消
GET  /api/harvest                         # 全部产出盘点(目录/文件数/大小)
```

## 核心约定

- **任务模型**: 一切恢复操作都是异步任务(起→轮询→终态)。镜像类任务可达数小时, 会话断了可用同参数重跑续传。
- **source**: 设备路径(`\\.\PhysicalDrive1`)或镜像/文件路径。**黄金法则: 怀疑盘要坏先 `image`, 之后所有任务对镜像做**。
- **安全边界**: 源盘全程只读; 输出目录与源同盘会被拒绝。
- **场景决策表**: schema里的 `scenarios` 给了8种典型事故(误删/BitLocker/RAID/勒索...)的步骤编排, 照着走即可。

## MCP接入(AI客户端原生调用)

Claude Desktop / Cursor / ZCode 等支持MCP的客户端可直接挂:

```json
{"mcpServers": {"rescueworkbench": {
    "command": "python",
    "args": ["C:/RescueWorkbench/mcp_server.py", "8765"]}}}
```

挂载后每个恢复通道都是一个工具(`rw_probe`/`rw_extract`/`rw_blunlock`...), 另有 `rw_schema`/`rw_devices`/`rw_harvest`/`rw_findpart`/`rw_job_status`/`rw_job_cancel`。工具自带回执: 默认等180秒, 超时返回任务id继续轮询。

## 典型编排示例

误删恢复: `scan` → `files(deleted_only=true)` → `extract(ids=...)` → `harvest`盘点
坏盘抢救: `probe` → `image(mode=verified)` → 之后全部对镜像文件做

---

# English

RescueWorkbench is a local HTTP service (default `http://127.0.0.1:8765`, see the startup banner) exposing a full Windows data-recovery toolkit over a JSON API. No auth (localhost only). The UI is Chinese, but the API is language-neutral — an AI agent can operate everything without reading a word of Chinese.

## Quick start (30 seconds)

```
GET  /api/schema                          # ← start here: all job kinds, params, returns, scenario playbook
POST /api/job  {"kind":"scan","params":{"source":"D:\\disk.img"}}   # start → {"id":"..."}
GET  /api/job/<id>                        # poll; state=done carries result, failed carries error
POST /api/job/<id>/cancel                 # cancel
GET  /api/harvest                         # inventory of all recovered outputs
```

## Key conventions

- **Job model**: every operation is async (start → poll → terminal state). Imaging can take hours; re-running the same params resumes.
- **source**: a device path (`\\.\PhysicalDrive1`) or an image/partition file. **Golden rule: if the disk may be dying, run `image` first and operate on the image afterwards.**
- **Safety**: sources are strictly read-only; output on the same disk as the source is refused.
- **Scenario playbook**: `scenarios` in the schema maps 8 typical disasters (accidental deletion / BitLocker / RAID / ransomware…) to step-by-step pipelines.

## MCP integration (native AI clients)

Works with Claude Desktop, Cursor, Codex, Trae, ZCode and any MCP-capable client:

```json
{"mcpServers": {"rescueworkbench": {
    "command": "python",
    "args": ["C:/RescueWorkbench/mcp_server.py", "8765"]}}}
```

Every recovery channel becomes a tool (`rw_probe`, `rw_extract`, `rw_blunlock`, …) plus helpers (`rw_schema`/`rw_devices`/`rw_harvest`/`rw_findpart`/`rw_job_status`/`rw_job_cancel`). Tools wait up to 180s by default, then return the job id for polling.

## Example pipelines

- Accidental deletion: `scan` → `files(deleted_only=true)` → `extract(ids=...)` → `harvest`
- Dying disk: `probe` → `image(mode=verified)` → work on the image file from then on
