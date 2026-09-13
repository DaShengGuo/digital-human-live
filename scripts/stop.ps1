# =============================================================================
# stop.ps1 — 停止所有 digital-human-live 进程
# 优先读 logs\processes.pid（编排器写入），按 PID 连子树一起结束
# 用法: powershell -ExecutionPolicy Bypass -File scripts\stop.ps1
# =============================================================================
$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$RepoRoot = Split-Path -Parent $PSScriptRoot
$PidFile = Join-Path $RepoRoot "logs\processes.pid"
# 归属证明 = 命令行包含本仓库路径 且 命中本项目入口模块。
# 不再按任意 app.py 关键字清扫（防误杀无关 Python, 防 PID 复用误杀）。
# 2026-09-11 修正: 原正则漏了 apps.console（控制台进程一直逃过清理, 残留占用 8030）
$OwnedPattern = [regex]("$([regex]::Escape($RepoRoot)).*(apps[./\\](orchestrator|tts_gateway|console)|app\.py)")

function Stop-Owned($procId, $why) {
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction SilentlyContinue
    if (-not $p) { Write-Host "跳过 pid=$procId ($why): 进程已不存在"; return }
    if (-not $p.CommandLine -or -not $OwnedPattern.IsMatch($p.CommandLine)) {
        Write-Warning "拒绝终止 pid=$procId ($why): 命令行与项目归属不符（可能 PID 复用）"
        return
    }
    Write-Host "停止 pid=$procId ($why)"
    & taskkill /PID $procId /T /F 2>$null | Out-Null   # /T 连子树
}

if (Test-Path $PidFile) {
    try { $procs = Get-Content $PidFile -Raw | ConvertFrom-Json } catch { $procs = $null }
    if ($procs) {
        # 先停编排器主进程树（含其派生的 tts/livetalking 子进程）
        if ($procs._meta -and $procs._meta.orch_pid) {
            Stop-Owned $procs._meta.orch_pid "编排器 run=$($procs._meta.run_id)"
        }
        foreach ($name in $procs.PSObject.Properties.Name) {
            if ($name -eq '_meta') { continue }
            $info = $procs.$name
            if ($info.pid) { Stop-Owned $info.pid $name }
        }
    }
    Remove-Item $PidFile -ErrorAction SilentlyContinue
} else {
    Write-Warning "未找到 $PidFile — 只按归属特征清理残留"
}

# 兜底: 命令行含本仓库路径且属本项目模块的残留（只清理自己, 不碰其他 python）
$strays = Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
    Where-Object { $_.CommandLine -and $OwnedPattern.IsMatch($_.CommandLine) -and
                   $_.ProcessId -ne $PID }
foreach ($p in $strays) {
    Stop-Owned $p.ProcessId "残留清理"
}

Write-Host "stop 完成" -ForegroundColor Green
exit 0
