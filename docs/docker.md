# Docker 化验收记录（2026-09-13）

## 产物

| 文件 | 说明 |
|---|---|
| docker/rag.Dockerfile | RAG 服务镜像: python:3.11-slim, torch 单独走 cpu 源(避免 Linux 默认拉 CUDA 全家桶), 模型 HF_HUB_OFFLINE 离线加载 |
| docker/mcp.Dockerfile | MCP 工具服务镜像(stdio) |
| docker-compose.yml | rag 服务编排: 端口 8021, 挂载 configs(只读)/data/HF 缓存卷, healthcheck |
| .dockerignore | 构建上下文瘦身(vendor/模型/node_modules 不进镜像) |
| requirements-rag.txt | 依赖清单(torch 分离) |

## 实测结果

1. **镜像构建**: `dhlive-rag` 与 `dhlive-mcp` 构建成功（基础镜像经 docker.m.daocloud.io 拉取）
2. **容器内服务功能**（docker exec 实测）:
   - 模型从 HF 缓存卷加载成功（bge-small-zh-v1.5, 71 weights）
   - `已加载落盘索引: 20 条`（data 卷挂载生效）
   - `/healthz` 返回 `{"status":"ok","entries":20,"index_ready":true,...}`
3. **MCP 容器镜像 stdio 握手**（docker run -i 实测）: 发送 initialize → 返回完整
   JSON-RPC 响应（serverInfo/capabilities/tools）——stdio 服务不依赖端口发布
4. **过程中修复的环境问题**（均为真实踩坑）:
   - docker-desktop WSL 虚拟机 /etc/resolv.conf 为空 → 所有 pull 卡死;
     写入 nameserver 223.5.5.5 + 重启 Docker Desktop 后恢复
   - daemon.json 三个 mirror 已失效一个(dockerpull.org), 清理为两个存活源
   - uvicorn 绑定 127.0.0.1 在容器内不可达 → configs/rag.yaml 拆分
     `bind`(0.0.0.0, 容器监听) 与 `host`(127.0.0.1, 客户端访问)
   - Linux 容器 `pip install torch` 默认捆绑 CUDA 全家桶(数GB) →
     Dockerfile 中单独 `--index-url https://download.pytorch.org/whl/cpu`

## 已知问题（环境相关, 非镜像问题）

- 本会话中 Docker Desktop 的 **宿主机端口转发层异常**（Windows→VM TCP 可连但空响应,
  VM 内部 docker-proxy 同样 reset; VM 出网与 pull 正常）。疑似与 Docker Desktop 从
  受限 shell 会话拉起有关。容器内功能已 exec 实测通过; 宿主端口复测命令:
  ```powershell
  docker compose up rag -d
  curl http://127.0.0.1:8021/healthz
  ```
  当前线上部署回退为宿主进程（scripts/rag_start.ps1）, 弹幕问答链路不受影响。
