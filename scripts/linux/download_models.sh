#!/usr/bin/env bash
# =============================================================================
# download_models.sh — 云端模型下载/检查（对应 scripts/download_models.ps1）
#   stage_a 档必需: CosyVoice-300M-SFT(可下载) + wav2lip.pth(需从本地传)
#                              + my_avatar_live 形象资产(需从本地传)
# 用法: bash scripts/linux/download_models.sh [--profile stage_a] [--check-only]
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

VENV_PY="$REPO_ROOT/.venv-lt/bin/python"
PROFILE="stage_a"
CHECK_ONLY=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --profile) PROFILE="$2"; shift 2 ;;
        --check-only) CHECK_ONLY=1; shift ;;
        *) echo "未知参数: $1" >&2; exit 2 ;;
    esac
done

check_path() {   # check_path <路径> <说明> <体积>
    local p="$1" note="$2" size="$3"
    if [[ -e "$p" ]] && [[ -n "$(ls -A "$p" 2>/dev/null || echo x)" ]]; then
        echo "[OK]   $note  ($size)  → $p"
        return 0
    fi
    echo "[MISS] $note  ($size)  → $p"
    return 1
}

echo "== 模型需求清单 (profile=$PROFILE) =="

MISSING=0

# ── 1. CosyVoice-300M-SFT（ModelScope 可下载）──────────────────
CV_DIR="vendor/CosyVoice/pretrained_models/CosyVoice-300M-SFT"
if ! check_path "$CV_DIR" "CosyVoice-300M-SFT (TTS 主模型)" "~5.4GB"; then
    MISSING=1
    if [[ "$CHECK_ONLY" -eq 0 ]]; then
        if [[ ! -x "$VENV_PY" ]]; then
            echo "  ERROR: venv 不存在，先跑 scripts/linux/bootstrap.sh" >&2
            exit 1
        fi
        echo "  下载中: modelscope iic/CosyVoice-300M-SFT ..."
        "$VENV_PY" - <<PYEOF
from modelscope import snapshot_download
snapshot_download('iic/CosyVoice-300M-SFT', local_dir='$CV_DIR')
print('done')
PYEOF
    fi
fi

# ── 2. wav2lip.pth（官方网盘, 需从本地传输）────────────────────
if ! check_path "vendor/LiveTalking/models/wav2lip.pth" "wav2lip.pth (口型模型)" "~215MB"; then
    MISSING=1
    echo "  获取: 从本地 Windows 传输 — scp <本机>:\"D:/AI/digital-human-live/vendor/LiveTalking/models/wav2lip.pth\" 云端:.../models/"
fi

# ── 3. 形象资产 my_avatar_live（本地生成, 必须传输）────────────
if ! check_path "vendor/LiveTalking/data/avatars/my_avatar_live" "my_avatar_live (你的真人形象)" "~391MB"; then
    MISSING=1
    echo "  获取: 从本地传输 — scp -r <本机>:\"D:/AI/digital-human-live/vendor/LiveTalking/data/avatars/my_avatar_live\" 云端:.../data/avatars/"
fi

# ── 4. 可选: hubert / genavatar 权重（仅 ultralight / 重做形象时需要）──
check_path "vendor/LiveTalking/models/hubert-large-ls960-ft" \
           "hubert-large-ls960-ft (ultralight 音频特征, 本档不需要)" "~2.5GB" || true
check_path "vendor/LiveTalking/models/scrfd_2.5g_kps.onnx" \
           "scrfd_2.5g_kps.onnx (仅重做 avatar 时需要)" "~3MB" || true

echo
if [[ "$MISSING" -eq 1 ]]; then
    echo "有缺失项，见上（[MISS] 行给了获取方式）"
    exit 1
fi
echo "全部就绪。下一步: bash scripts/linux/start.sh --profile stage_a_3090"
