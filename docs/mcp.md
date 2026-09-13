# MCP Server（AI 客户端接入数字人直播间）

把直播间能力封装为 4 个 MCP 工具，Claude Desktop / Cursor 等 AI 客户端
可直接问答知识库、查看直播状态、指挥口播、打断说话。

| 工具 | 能力 | 后端 |
|---|---|---|
| `ask_kb` | 知识库问答（标准答案/兜底，不编造） | RAG 服务 :8021 |
| `get_live_status` | 阶段/队列/说话中/GPU/降级/会话 | 编排器 :8020 |
| `say_to_live` | 指挥数字人口播一段文本 | 编排器 /say |
| `interrupt_live` | 打断当前口播并清空队列 | 编排器 /interrupt |

服务地址/超时全部在 `configs/mcp.yaml`。工具实现:
`apps/mcp_server/main.py`（MCP 官方 Python SDK 2.x, stdio 传输）。

## 验收（scripts/mcp_selftest.py，2026-09-13 实测）

```
连接成功: dhlive-digital-human
工具列表: ['ask_kb', 'get_live_status', 'interrupt_live', 'say_to_live']
ask_kb → [命中] 本期AI实训课程价格为3980元，包含全部实操课程。
get_live_status → {"healthz": "编排器不可达(直播间未开机?)..."}   # 宕机优雅报错
say_to_live → 编排器不可达(直播间未开机?)...
SELFTEST: PASS
```

## Claude Desktop 接入

`claude_desktop_config.json`（设置 → 开发者 → 编辑配置）：

```json
{
  "mcpServers": {
    "dhlive-digital-human": {
      "command": "D:\\AI\\digital-human-live\\.venv-mcp\\Scripts\\python.exe",
      "args": ["-m", "apps.mcp_server.main"],
      "cwd": "D:\\AI\\digital-human-live",
      "env": { "DHLIVE_MCP_CONFIG": "D:\\AI\\digital-human-live\\configs\\mcp.yaml" }
    }
  }
}
```

Cursor：设置 → MCP → Add Server，同样填 command/args/cwd。

## 运行与自测

```powershell
# AI 客户端会自己拉起 stdio 服务, 无需手动启动; 手动验证:
.venv-mcp\Scripts\python.exe scripts\mcp_selftest.py
```

依赖: `.venv-mcp`（mcp 2.x + numpy/pyyaml/requests）。
服务前置: RAG 服务（rag_start.ps1）在线则 ask_kb 可用；
编排器未开机时工具返回可读错误，不会让客户端模型拿到异常栈。

## 已知限制

- say_to_live 只入队不保证"已播完"（LiveTalking 无完成回调，见编排器 /status 注释）
- stdio 模式每客户端一个服务进程；多客户端并发操纵同一直播间属预期行为
