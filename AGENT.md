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
