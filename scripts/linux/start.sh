#!/usr/bin/env bash
# =============================================================================
# start.sh — 一键启动（配置驱动，对应 scripts/start.ps1）
# 用法:
#   bash scripts/linux/start.sh                          # 默认档
#   bash scripts/linux/start.sh --profile stage_a_3090   # 云端档
#   bash scripts/linux/start.sh --profile stage_a_3090 --daemon   # 后台 + 日志
# 实际拉起由 apps/orchestrator/main.py 完成，本脚本只负责托管它
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

PROFILE=""
DAEMON=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --profile) PROFILE="$2"; shift 2 ;;
        --daemon) DAEMON=1; shift ;;
        *) echo "未知参数: $1" >&2; exit 2 ;;
    esac
done

VENV_PY="$REPO_ROOT/.venv-lt/bin/python"
if [[ ! -x "$VENV_PY" ]]; then
    echo "ERROR: venv 不存在（$VENV_PY），先跑 scripts/linux/bootstrap.sh" >&2
    exit 1
fi

ARGS=(-m apps.orchestrator.main)
[[ -n "$PROFILE" ]] && ARGS+=(--profile "$PROFILE")

echo "== 启动 digital-human-live (profile=${PROFILE:-default}) =="
echo "编排器 API: http://127.0.0.1:8020  |  LiveTalking: http://127.0.0.1:8010"
echo "停止: bash scripts/linux/stop.sh 或 Ctrl+C"
echo

if [[ "$DAEMON" -eq 1 ]]; then
    mkdir -p "$REPO_ROOT/logs"
    LOG="$REPO_ROOT/logs/orchestrator.out.log"
    # setsid: 脱离当前终端，SSH 断开不杀进程
    setsid "$VENV_PY" "${ARGS[@]}" >>"$LOG" 2>&1 &
    echo "已后台启动 pid=$!，日志: $LOG"
    echo "查看: tail -f $LOG"
else
    exec "$VENV_PY" "${ARGS[@]}"
fi
