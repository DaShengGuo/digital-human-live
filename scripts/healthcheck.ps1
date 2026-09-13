# =============================================================================
# healthcheck.ps1 — 健康检查：进程/端口/显存/TTFF 日志
# 用法: powershell -ExecutionPolicy Bypass -File scripts\healthcheck.ps1
# 退出码: 0=健康  1=异常
# =============================================================================
$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Fail = $false

# ── 1. 编排器健康 API ─────────────────────────────────────────
$OrchPort = 8020
try {
    $h = Invoke-RestMethod -Uri "http://127.0.0.1:${OrchPort}/healthz" -TimeoutSec 5
    Write-Host "[编排器] profile=$($h.profile) uptime=$($h.uptime_s)s queue=$($h.queue)"
    foreach ($name in $h.procs.PSObject.Properties.Name) {
        $p = $h.procs.$name
        $state = if ($p.state -eq 'running') { "OK" } else { "BAD" }
        $color = if ($p.state -eq 'running') { "Green" } else { "Red" }
        Write-Host "  [$state] $name pid=$($p.pid) state=$($p.state)" -ForegroundColor $color
        if ($p.state -ne 'running') { $Fail = $true }
    }
    if ($h.gpu) {
        Write-Host ("  GPU: {0}/{1}MiB  util={2}%  temp={3}C" -f $h.gpu.mem_used, $h.gpu.mem_total, $h.gpu.util, $h.gpu.temp)
        if ($h.gpu.mem_used -gt 6600) {
            Write-Host "  [BAD] 显存超过 6.6G 上限" -ForegroundColor Red; $Fail = $true
        }
        if ($h.gpu.temp -gt 75) {
            Write-Host "  [BAD] 温度超过 75C（笔记本红线）" -ForegroundColor Red; $Fail = $true
        }
    } else {
        Write-Host "  GPU: nvidia-smi 不可用（mock 环境）" -ForegroundColor Yellow
    }
    if ($h.events.Count -gt 0) {
        Write-Host "  最近看门狗事件:"
        $h.events | Select-Object -Last 5 | ForEach-Object {
            Write-Host ("    {0} [{1}] {2}" -f $_.ts, $_.kind, $_.detail) -ForegroundColor Yellow
        }
    }
} catch {
    Write-Host "[BAD] 编排器 8020 端口不可达: $($_.Exception.Message)" -ForegroundColor Red
    $Fail = $true
}

# ── 2. LiveTalking 端口 ───────────────────────────────────────
try {
    $null = Invoke-WebRequest -Uri "http://127.0.0.1:8010/" -TimeoutSec 5 -UseBasicParsing
    Write-Host "[OK] LiveTalking 8010 可达" -ForegroundColor Green
} catch {
    Write-Host "[BAD] LiveTalking 8010 不可达" -ForegroundColor Red
    $Fail = $true
}

# ── 3. TTS 网关端口 ───────────────────────────────────────────
try {
    $t = Invoke-RestMethod -Uri "http://127.0.0.1:8011/healthz" -TimeoutSec 5
    Write-Host "[OK] TTS 网关 8011 可达 (model_loaded=$($t.model_loaded))" -ForegroundColor Green
} catch {
    Write-Host "[BAD] TTS 网关 8011 不可达" -ForegroundColor Red
    $Fail = $true
}

# ── 4. TTFF 证据（从日志粗读最近一次 TTS TTFB）────────────────
$TtsLog = Join-Path $RepoRoot "logs\tts.log"
if (Test-Path $TtsLog) {
    $ttffLine = Select-String -Path $TtsLog -Pattern "TTS TTFB ([\d.]+)s" | Select-Object -Last 1
    if ($ttffLine) {
        $ttff = [double]$ttffLine.Matches[0].Groups[1].Value
        $ok = $ttff -le 1.5
        $color = if ($ok) { "Green" } else { "Red" }
        Write-Host ("  TTS TTFB(≈TTFF): {0:N3}s (阈值 1.5s) [{1}]" -f $ttff, $(if ($ok) { "OK" } else { "BAD" })) -ForegroundColor $color
        if (-not $ok) { $Fail = $true }
    } else {
        Write-Host "  TTS 尚无合成记录（先 POST /say 一次）" -ForegroundColor Yellow
    }
}

if ($Fail) { Write-Host "`n结果: 异常" -ForegroundColor Red; exit 1 }
Write-Host "`n结果: 健康" -ForegroundColor Green
