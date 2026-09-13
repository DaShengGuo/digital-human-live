# =============================================================================
# bench_vram.ps1 — 显存基准入口（包 python 版）
# 用法:
#   powershell -ExecutionPolicy Bypass -File scripts\bench_vram.ps1 -DurationMin 15
#   powershell -ExecutionPolicy Bypass -File scripts\bench_vram.ps1 -SkipGpu   # mock
# =============================================================================
param(
    [string]$Profile = "stable_8g",
    [double]$DurationMin = 15,
    [int]$Interval = 5,
    [switch]$SkipGpu
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$VenvPy = Join-Path $RepoRoot ".venv-lt\Scripts\python.exe"
if (-not (Test-Path $VenvPy)) { $VenvPy = "python" }

$Args = @("-m", "apps.bench_vram", "--profile", $Profile,
          "--duration-min", $DurationMin, "--interval", $Interval)
if ($SkipGpu) { $Args += "--skip-gpu" }

Write-Host "== bench_vram profile=$Profile duration=${DurationMin}min ==" -ForegroundColor Cyan
& $VenvPy @Args
