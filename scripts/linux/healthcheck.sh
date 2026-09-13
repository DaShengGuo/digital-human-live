#!/usr/bin/env bash
# =============================================================================
# healthcheck.sh — 健康检查：进程/端口/显存/TTFF 日志（对应 scripts/healthcheck.ps1）
# 用法: bash scripts/linux/healthcheck.sh [--profile stage_a_3090]
# 退出码: 0=健康  1=异常
# =============================================================================
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

# 阈值随档位走（3090 与 8G 笔记本不同）
VRAM_LIMIT=6600
TEMP_LIMIT=75
PROFILE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --profile) PROFILE="$2"; shift 2 ;;
        *) echo "未知参数: $1" >&2; exit 2 ;;
    esac
done
if [[ -z "$PROFILE" ]]; then
    PROFILE="$(python3 -c "import yaml,sys;print(yaml.safe_load(open('configs/default.yaml'))['default_profile'])" 2>/dev/null || echo stable_8g)"
fi
PROF_FILE="configs/profile_${PROFILE}.yaml"
if [[ -f "$PROF_FILE" ]]; then
    read -r VRAM_LIMIT TEMP_LIMIT < <(python3 - "$PROF_FILE" <<'PYEOF'
import sys, yaml
w = (yaml.safe_load(open(sys.argv[1], encoding='utf-8')) or {}).get('orchestrator', {}).get('watchdog', {})
print(w.get('vram_limit_mb', 6600), w.get('temp_limit_c', 75))
PYEOF
)
fi
echo "profile=$PROFILE  阈值: 显存<${VRAM_LIMIT}MiB  温度<${TEMP_LIMIT}C"

FAIL=0

# ── 1. 编排器健康 API ─────────────────────────────────────────
HZ="$(curl -s --max-time 5 http://127.0.0.1:8020/healthz || true)"
if [[ -z "$HZ" ]]; then
    echo "[BAD] 编排器 8020 端口不可达"
    FAIL=1
else
    python3 - "$HZ" "$VRAM_LIMIT" "$TEMP_LIMIT" <<'PYEOF'
import json, sys
h = json.loads(sys.argv[1]); vram_lim = int(sys.argv[2]); temp_lim = int(sys.argv[3])
fail = False
print(f"[编排器] profile={h.get('profile')} phase={h.get('phase')} uptime={h.get('uptime_s')}s queue={h.get('queue')}")
for name, p in (h.get('procs') or {}).items():
    ok = p.get('state') == 'running'
    print(f"  [{'OK' if ok else 'BAD'}] {name} pid={p.get('pid')} state={p.get('state')}")
    if not ok:
        fail = True
g = h.get('gpu')
if g:
    print(f"  GPU: {g.get('mem_used')}/{g.get('mem_total')}MiB  util={g.get('util')}%  temp={g.get('temp')}C")
    if (g.get('mem_used') or 0) > vram_lim:
        print(f"  [BAD] 显存超过 {vram_lim}MiB 上限"); fail = True
    if (g.get('temp') or 0) > temp_lim:
        print(f"  [BAD] 温度超过 {temp_lim}C"); fail = True
else:
    print("  GPU: nvidia-smi 不可用（mock 环境）")
for e in (h.get('events') or [])[-5:]:
    print(f"    看门狗 {e.get('ts')} [{e.get('kind')}] {e.get('detail')}")
sys.exit(1 if fail else 0)
PYEOF
    [[ $? -ne 0 ]] && FAIL=1
fi

# ── 2. LiveTalking 端口 ───────────────────────────────────────
if curl -s --max-time 5 -o /dev/null http://127.0.0.1:8010/; then
    echo "[OK] LiveTalking 8010 可达"
else
    echo "[BAD] LiveTalking 8010 不可达"; FAIL=1
fi

# ── 3. TTS 网关端口 ───────────────────────────────────────────
TTS="$(curl -s --max-time 5 http://127.0.0.1:8011/healthz || true)"
if [[ -n "$TTS" ]]; then
    echo "[OK] TTS 网关 8011 可达 ($(python3 -c "import json,sys;d=json.loads(sys.argv[1]);print('model_loaded=%s warmup=%s' % (d.get('model_loaded'), d.get('warmup_done')))" "$TTS" 2>/dev/null))"
else
    echo "[BAD] TTS 网关 8011 不可达"; FAIL=1
fi

# ── 4. TTFF 证据（从日志粗读最近一次 TTS TTFB）────────────────
if [[ -f logs/tts.log ]]; then
    TTFF="$(grep -oP 'TTS TTFB ([\d.]+)s' logs/tts.log 2>/dev/null | tail -1 | grep -oP '[\d.]+')"
    if [[ -n "$TTFF" ]]; then
        OK="$(python3 -c "print('OK' if float('$TTFF') <= 1.5 else 'BAD')")"
        echo "  TTS TTFB(≈TTFF): ${TTFF}s (阈值 1.5s) [$OK]"
        [[ "$OK" == "BAD" ]] && FAIL=1
    else
        echo "  TTS 尚无合成记录（先 POST /say 一次）"
    fi
fi

if [[ "$FAIL" -eq 1 ]]; then echo; echo "结果: 异常"; exit 1; fi
echo; echo "结果: 健康"
