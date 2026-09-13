# =============================================================================
# bootstrap.ps1 — 一次性环境准备
#   1. 检查 python 3.10+/GPU/nvidia-smi/磁盘
#   2. 建 D 盘 venv（.venv-lt 共用于 LiveTalking 与 tts_gateway）
#   3. 安装 LiveTalking requirements + tts_gateway 依赖
#   4. 安装 CosyVoice 最小依赖（不解 deepspeed/tensorrt 等 linux-only 项）
# 用法: powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1 [-SkipDeps]
# =============================================================================
param(
    [switch]$SkipDeps
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot
Write-Host "== digital-human-live bootstrap ==" -ForegroundColor Cyan

# ── 1. Python 检查 ─────────────────────────────────────────────
$PyExe = $null
foreach ($cand in @("D:\AI\digital-human-live\.venv-lt\Scripts\python.exe",
                    "C:\tools\python311\python.exe")) {
    if (Test-Path $cand) { $PyExe = $cand; break }
}
if (-not $PyExe) {
    foreach ($cand in @("python", "py -3.11", "py -3.10")) {
        try {
            $v = & $cand.Split(' ')[0] --version 2>$null
            if ($LASTEXITCODE -eq 0) { $PyExe = $cand.Split(' ')[0]; break }
        } catch {}
    }
}
if (-not $PyExe) { Write-Error "找不到 Python 3.10+，请先安装"; exit 1 }
Write-Host "Python: $PyExe ($(& $PyExe --version))"

# ── 2. GPU 检查（允许缺失, bench 可 mock）─────────────────────
$smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($smi) {
    nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | ForEach-Object {
        Write-Host "GPU: $_" -ForegroundColor Green
    }
} else {
    Write-Warning "nvidia-smi 未找到 — 脚本/编排器可照常开发, bench 将输出 mock 报告"
}

# ── 3. 磁盘检查 ───────────────────────────────────────────────
$drive = (Get-Item $RepoRoot).PSDrive
$freeGB = [math]::Round($drive.Free / 1GB, 1)
Write-Host "磁盘 $($drive.Name): 剩余 ${freeGB}GB"
if ($freeGB -lt 30) {
    Write-Warning "剩余空间不足 30GB — 模型下载可能失败 (CosyVoice-300M ≈3GB, avatar 资产 ≈1-2GB)"
}

# ── 4. venv ───────────────────────────────────────────────────
$VenvDir = Join-Path $RepoRoot ".venv-lt"
if (-not (Test-Path (Join-Path $VenvDir "Scripts\python.exe"))) {
    Write-Host "创建 venv: $VenvDir"
    & $PyExe -m venv $VenvDir
}
$VenvPy = Join-Path $VenvDir "Scripts\python.exe"

# pip 走阿里云镜像（本机 7897 代理已坏, 见 memory: prod-machine-env）
$PipArgs = @("-m", "pip", "install", "-i", "https://mirrors.aliyun.com/pypi/simple/",
             "--trusted-host", "mirrors.aliyun.com")
& $VenvPy -m pip install --upgrade pip -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com -q

if ($SkipDeps) { Write-Host "跳过依赖安装"; exit 0 }

# ── 5. LiveTalking 依赖 ───────────────────────────────────────
Write-Host "`n== 安装 LiveTalking requirements ==" -ForegroundColor Cyan
# torch 单独装: 阿里云 pytorch wheel 镜像 (官方 download.pytorch.org 实测仅 ~300KB/s)
& $VenvPy @PipArgs "torch==2.6.0+cu124" "torchvision==0.21.0+cu124" "torchaudio==2.6.0+cu124" -f https://mirrors.aliyun.com/pytorch-wheels/cu124/ --no-cache-dir
if ($LASTEXITCODE -ne 0) { Write-Error "torch 安装失败"; exit 1 }
& $VenvPy @PipArgs -r (Join-Path $RepoRoot "vendor\LiveTalking\requirements.txt")
if ($LASTEXITCODE -ne 0) { Write-Error "LiveTalking requirements 安装失败"; exit 1 }

# 虚拟摄像头/音频输出（可失败, webrtc 模式不需要）
& $VenvPy @PipArgs pyvirtualcam pyaudio 2>$null

# ── 6. CosyVoice 最小依赖（tts_gateway 与 LiveTalking 共用 venv）────
Write-Host "`n== 安装 CosyVoice 最小依赖 ==" -ForegroundColor Cyan
& $VenvPy @PipArgs HyperPyYAML conformer lightning gradio loguru srt wget wetext inflect modelscope
# 冒烟实测缺的传递依赖 (2026-09-07): whisper/omegaconf/hydra-core/gdown/matplotlib/rich/x-transformers/pyworld/pyarrow/networkx/onnx/onnxruntime
& $VenvPy @PipArgs openai-whisper omegaconf hydra-core gdown matplotlib rich x-transformers pyworld pyarrow networkx onnx onnxruntime

Write-Host "`nbootstrap 完成。下一步: scripts\download_models.ps1" -ForegroundColor Green
Write-Host "venv python: $VenvPy"
