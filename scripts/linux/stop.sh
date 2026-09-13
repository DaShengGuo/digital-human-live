#!/usr/bin/env bash
# =============================================================================
# stop.sh — 停止所有 digital-human-live 进程（对应 scripts/stop.ps1）
#   优先读 logs/processes.pid（编排器写入），按进程组连孙进程一起结束
#   归属校验: 命令行必须含本仓库路径 且 命中项目入口模块（防 PID 复用误杀）
# 用法: bash scripts/linux/stop.sh
# =============================================================================
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PID_FILE="$REPO_ROOT/logs/processes.pid"

owned() {   # owned <pid> — 命令行含本仓库路径且命中项目入口
    local pid="$1" cmd
    cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)" || return 1
    [[ -n "$cmd" ]] || return 1
    [[ "$cmd" == *"$REPO_ROOT"* ]] || return 1
    [[ "$cmd" =~ (apps/(orchestrator|tts_gateway)|app\.py) ]] || return 1
    return 0
}

pgid_of() { ps -o pgid= -p "$1" 2>/dev/null | tr -d ' '; }

stop_owned() {   # stop_owned <pid> <why>
    local pid="$1" why="$2" pg
    if ! kill -0 "$pid" 2>/dev/null; then
        echo "跳过 pid=$pid ($why): 进程已不存在"
        return
    fi
    if ! owned "$pid"; then
        echo "拒绝终止 pid=$pid ($why): 命令行与项目归属不符（可能 PID 复用）" >&2
        return
    fi
    echo "停止 pid=$pid ($why)"
    pg="$(pgid_of "$pid")"
    if [[ -n "$pg" ]]; then kill -TERM -- "-$pg" 2>/dev/null; else kill -TERM "$pid" 2>/dev/null; fi
    for _ in $(seq 1 20); do
        kill -0 "$pid" 2>/dev/null || return
        sleep 0.5
    done
    echo "  pid=$pid 未在 10s 内退出，强杀"
    if [[ -n "$pg" ]]; then kill -KILL -- "-$pg" 2>/dev/null; else kill -KILL "$pid" 2>/dev/null; fi
}

if [[ -f "$PID_FILE" ]]; then
    # 先停编排器主进程树（含其派生的 tts/livetalking 子进程）
    mapfile -t PIDS < <(python3 - "$PID_FILE" <<'PYEOF'
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding='utf-8'))
except Exception:
    sys.exit(0)
meta = d.get('_meta') or {}
if meta.get('orch_pid'):
    print(f"{meta['orch_pid']}\t编排器 run={meta.get('run_id','')}")
for name, info in d.items():
    if name == '_meta' or not isinstance(info, dict):
        continue
    if info.get('pid'):
        print(f"{info['pid']}\t{name}")
PYEOF
)
    for line in "${PIDS[@]:-}"; do
        [[ -n "$line" ]] || continue
        stop_owned "${line%%$'\t'*}" "${line#*$'\t'}"
    done
    rm -f "$PID_FILE"
else
    echo "WARN: 未找到 $PID_FILE — 只按归属特征清理残留" >&2
fi

# 兜底: 命令行含本仓库路径且属本项目模块的残留（只清理自己，不碰其他 python）
for cmdline in /proc/[0-9]*/cmdline; do
    pid="${cmdline#/proc/}"; pid="${pid%%/*}"
    [[ "$pid" == "$$" ]] && continue
    if owned "$pid"; then
        stop_owned "$pid" "残留清理"
    fi
done

echo "stop 完成"
exit 0
