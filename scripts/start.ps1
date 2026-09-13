# =============================================================================
# start.ps1 — 一键启动（配置驱动）
# 用法:
#   powershell -ExecutionPolicy Bypass -File scripts\start.ps1              # 默认档
#   powershell -ExecutionPolicy Bypass -File scripts\start.ps1 -Profile fallback
# 实际拉起由 apps/orchestrator/main.py 完成, 本脚本只负责前台托管它
# =============================================================================
param(
    [string]$Profile = ""
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$VenvPy = Join-Path $RepoRoot ".venv-lt\Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
    Write-Error "venv 不存在, 先运行 scripts\bootstrap.ps1"
    exit 1
}

$OrchArgs = @("-m", "apps.orchestrator.main")
if ($Profile -ne "") { $OrchArgs += @("--profile", $Profile) }

Write-Host "== 启动 digital-human-live (profile=$(if ($Profile) { $Profile } else { 'default' })) ==" -ForegroundColor Cyan
Write-Host "编排器 API: http://127.0.0.1:8020  |  LiveTalking: http://127.0.0.1:8010"
Write-Host "停止: scripts\stop.ps1 或 Ctrl+C`n"

& $VenvPy @OrchArgs
