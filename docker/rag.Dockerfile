# RAG 知识库服务镜像 (linux/amd64, CPU 档)
# 构建: docker build -f docker/rag.Dockerfile -t dhlive-rag .
# 运行: docker compose up rag   (模型缓存/数据卷见 docker-compose.yml)
FROM python:3.11-slim
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    HF_HUB_OFFLINE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
# torch 必须单独从 cpu 源装: pyPI 的 Linux wheel 捆绑 CUDA 全家桶(数GB)
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu
COPY requirements-rag.txt .
RUN pip install --no-cache-dir -r requirements-rag.txt
COPY apps/ apps/
COPY configs/ configs/
EXPOSE 8021
CMD ["python", "-m", "apps.rag_service.main", "--config", "configs/rag.yaml"]
