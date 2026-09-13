# 下载 RAG 模型（国内镜像 hf-mirror; 已缓存则秒过）
# HF_HUB_DISABLE_XET: hf-mirror 不代理 Xet 存储后端(401), 强制普通 HTTP 下载
$ErrorActionPreference = 'Stop'
$env:HF_ENDPOINT = 'https://hf-mirror.com'
$env:HF_HUB_DISABLE_XET = '1'
Set-Location (Join-Path $PSScriptRoot '..')
& .\.venv-rag\Scripts\python.exe -m apps.rag_service.download_models --config configs/rag.yaml
