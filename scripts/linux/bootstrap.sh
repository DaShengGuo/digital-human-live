#!/usr/bin/env bash
# =============================================================================
# bootstrap.sh — 云端 Linux 一次性环境准备（对应 scripts/bootstrap.ps1）
#   1. 检查 python3.10+/GPU/磁盘/ffmpeg
#   2. 建 .venv-lt（LiveTalking 与 tts_gateway 共用）
#   3. 安装 torch(cu124) + LiveTalking requirements + CosyVoice 最小依赖
# 用法: bash scripts/linux/bootstrap.sh [--skip-deps]
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

PIP_INDEX="https://mirrors.aliyun.com/pypi/simple/"
PIP_HOST="mirrors.aliyun.com"
TORCH_INDEX="https://mirrors.aliyun.com/pytorch-wheels/cu124/"
SKIP_DEPS=0
[[ "${1:-}" == "--skip-deps" ]] && SKIP_DEPS=1

echo "== digital-human-live bootstrap (Linux) =="
echo "repo: $REPO_ROOT"

# ── 1. Python 检查 ─────────────────────────────────────────────
PY="${PY:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
    echo "ERROR: 找不到 $PY，请先安装 Python 3.10+" >&2
    exit 1
fi
PY_VER="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
PY_MAJOR="${PY_VER%%.*}"; PY_MINOR="${PY_VER##*.}"
if [[ "$PY_MAJOR" -lt 3 || ( "$PY_MAJOR" -eq 3 && "$PY_MINOR" -lt 10 ) ]]; then
    echo "ERROR: 需要 Python >= 3.10，当前 $PY_VER" >&2
    exit 1
fi
echo "Python: $("$PY" --version) ($(command -v "$PY"))"

# ── 2. GPU 检查（允许缺失，bench 可 mock）─────────────────────
if command -v nvidia-smi >/dev/null 2>&1; then
    nvidia-smi --query-gpu=name,memory.total,driver_version \
               --format=csv,noheader | sed 's/^/GPU: /'
else
    echo "WARN: nvidia-smi 未找到 — 脚本/编排器可照常开发，bench 将输出 mock 报告" >&2
fi

# ── 3. ffmpeg 检查（验收脚本用 ffprobe 分析录制）──────────────
for bin in ffmpeg ffprobe; do
    if ! command -v "$bin" >/dev/null 2>&1; then
        echo "WARN: 缺 $bin — 验收分析会失败。安装: sudo apt-get update && sudo apt-get install -y ffmpeg" >&2
    fi
done

# ── 4. 磁盘检查 ───────────────────────────────────────────────
FREE_GB=$(df -BG --output=avail "$REPO_ROOT" | tail -1 | tr -dc '0-9')
echo "磁盘剩余: ${FREE_GB}GB"
if [[ "${FREE_GB:-0}" -lt 30 ]]; then
    echo "WARN: 剩余空间不足 30GB — 模型下载可能失败 (CosyVoice-300M≈5.4GB, avatar≈0.4GB)" >&2
fi

# ── 5. venv ───────────────────────────────────────────────────
# GPU 镜像常预装 torch（如算家云 base 环境 2.5.1+cu124）→ venv 继承，省 2.5GB 下载
HAS_SYS_TORCH=0
if "$PY" -c "import torch,sys; sys.exit(0 if torch.cuda.is_available() else 1)" >/dev/null 2>&1; then
    HAS_SYS_TORCH=1
    echo "检测到系统 torch: $("$PY" -c 'import torch; print(torch.__version__, "cuda", torch.version.cuda)') → venv 继承"
fi

VENV_DIR="$REPO_ROOT/.venv-lt"
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    echo "创建 venv: $VENV_DIR"
    if [[ "$HAS_SYS_TORCH" -eq 1 ]]; then
        "$PY" -m venv --system-site-packages "$VENV_DIR"
    else
        "$PY" -m venv "$VENV_DIR"
    fi
fi
VENV_PY="$VENV_DIR/bin/python"
"$VENV_PY" -m pip install --upgrade pip -i "$PIP_INDEX" --trusted-host "$PIP_HOST" -q

if [[ "$SKIP_DEPS" -eq 1 ]]; then
    echo "跳过依赖安装。venv python: $VENV_PY"
    exit 0
fi

# ── 6. LiveTalking 依赖 ───────────────────────────────────────
if [[ "$HAS_SYS_TORCH" -eq 1 ]]; then
    echo
    echo "== 跳过 torch 安装（继承系统 torch）=="
else
    echo
    echo "== 安装 torch (cu124) =="
    # 阿里云 pytorch wheel 镜像（官方 download.pytorch.org 在境内实测仅 ~300KB/s）
    "$VENV_PY" -m pip install -i "$PIP_INDEX" --trusted-host "$PIP_HOST" \
        torch==2.6.0+cu124 torchvision==0.21.0+cu124 torchaudio==2.6.0+cu124 \
        -f "$TORCH_INDEX" --no-cache-dir
fi

echo
echo "== 安装 LiveTalking requirements =="
"$VENV_PY" -m pip install -i "$PIP_INDEX" --trusted-host "$PIP_HOST" \
    -r "$REPO_ROOT/vendor/LiveTalking/requirements.txt"

# 虚拟摄像头/音频输出（可失败，webrtc 模式不需要）
"$VENV_PY" -m pip install -i "$PIP_INDEX" --trusted-host "$PIP_HOST" \
    pyvirtualcam pyaudio || echo "WARN: pyvirtualcam/pyaudio 安装失败（webrtc 模式可忽略）"

# ── 7. CosyVoice 最小依赖（tts_gateway 与 LiveTalking 共用 venv）──
echo
echo "== 安装 CosyVoice 最小依赖 =="
"$VENV_PY" -m pip install -i "$PIP_INDEX" --trusted-host "$PIP_HOST" \
    HyperPyYAML conformer lightning gradio loguru srt wget wetext inflect modelscope
# 冒烟实测缺的传递依赖 (2026-09-07)
"$VENV_PY" -m pip install -i "$PIP_INDEX" --trusted-host "$PIP_HOST" \
    openai-whisper omegaconf hydra-core gdown matplotlib rich x-transformers \
    pyworld pyarrow networkx onnx onnxruntime

echo
echo "bootstrap 完成。下一步: bash scripts/linux/download_models.sh"
echo "venv python: $VENV_PY"
