# MCP 工具服务镜像 (stdio 传输, 由 AI 客户端拉起; 镜像用于 Linux 侧分发)
# 构建: docker build -f docker/mcp.Dockerfile -t dhlive-mcp .
# 验证: echo initialize请求 | docker run -i --rm dhlive-mcp
FROM python:3.11-slim
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PYTHONUNBUFFERED=1
WORKDIR /app
RUN pip install --no-cache-dir "mcp>=2" numpy pyyaml requests
COPY apps/ apps/
COPY configs/ configs/
CMD ["python", "-m", "apps.mcp_server.main"]
