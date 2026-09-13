# 启动 RAG 知识库服务（默认 :8021, 参数见 configs/rag.yaml）
# HF_HUB_OFFLINE: 模型已缓存, 启动不联网(直播机断网也能起)
param([string]$Config = 'configs/rag.yaml')
$ErrorActionPreference = 'Stop'
$env:HF_HUB_OFFLINE = '1'
Set-Location (Join-Path $PSScriptRoot '..')
& .\.venv-rag\Scripts\python.exe -m apps.rag_service.main --config $Config
