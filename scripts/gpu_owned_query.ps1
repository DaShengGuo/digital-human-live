# =============================================================================
# gpu_owned_query.ps1 — 一次性查询「本项目进程组」的显存占用, 打印一行结果
#
# 背景: nvidia-smi 在 Windows/WDDM 下拿不到 per-process 显存（全是 N/A），
#       上游看门狗只能用整卡口径 → Chrome/VS Code/直播伴侣/dwm 的占用被算进本链,
#       桌面一忙就误判 vram_exceed 并重启口型进程（2026-09-11 实测复现多次）。
#
# 关键事实: 本仓库 .venv-lt\Scripts\python.exe 是启动器, 真正占显存的是它派生的
#           worker 子进程 → 必须做进程树展开。
#
# 输出: "合计MB|pid=MB pid=MB ..."   取不到数据时输出 "ERR|原因"
# 用法: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\gpu_owned_query.ps1
#       [-PidFile logs\owned_pids.txt]
# =============================================================================
param([string]$PidFile = "")

$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$RepoRoot = Split-Path -Parent $PSScriptRoot
if ($PidFile -eq "") { $PidFile = Join-Path $RepoRoot "logs\owned_pids.txt" }

$pids = @()
if (Test-Path $PidFile) {
    $pids = @(Get-Content $PidFile |
              Where-Object { $_ -match '^\s*\d+\s*$' } |
              ForEach-Object { [int]$_.Trim() } |
              Select-Object -Unique)
}
if ($pids.Count -eq 0) { Write-Output "ERR|no-pids($PidFile)"; exit 0 }

# 进程树展开(启动器 → worker → 孙), 纯数组实现避免泛型集合在 PS5.1 上偶发失败
$expanded = @($pids)
$allproc = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
             Select-Object ProcessId, ParentProcessId)
$children = @{}
foreach ($p in $allproc) {
    $ppid = [int]$p.ParentProcessId
    if (-not $children.ContainsKey($ppid)) { $children[$ppid] = @() }
    $children[$ppid] += [int]$p.ProcessId
}
for ($depth = 0; $depth -lt 3; $depth++) {
    $cand = @()
    foreach ($e in $expanded) { if ($children.ContainsKey($e)) { $cand += $children[$e] } }
    $add = @($cand | Where-Object { $expanded -notcontains $_ })
    if ($add.Count -eq 0) { break }
    $expanded += $add
}

$inst = @(Get-CimInstance -ClassName Win32_PerfFormattedData_GPUPerformanceCounters_GPUProcessMemory -ErrorAction SilentlyContinue)
if ($inst.Count -eq 0) { Write-Output "ERR|no-counter-instances"; exit 0 }

$sum = 0.0
$parts = @()
foreach ($procId in $expanded) {
    $hit = @($inst | Where-Object { $_.Name -like "pid_${procId}_*" })
    if ($hit.Count -gt 0) {
        $mb = 0.0
        foreach ($h in $hit) { $mb += [double]$h.DedicatedUsage }
        $sum += $mb
        $parts += ("{0}={1}MB" -f $procId, [math]::Round($mb / 1MB, 0))
    }
}
if ($parts.Count -eq 0) {
    # 一个都没匹配上: 把诊断信息带上, 便于区分"本链真没占显存"和"查询/匹配坏了"
    $sample = (@($inst | Select-Object -First 3 | ForEach-Object { $_.Name }) -join ';')
    Write-Output ("0.0|no-match(expanded={0},instances={1},sample=[{2}])" -f `
                  $expanded.Count, $inst.Count, $sample)
    exit 0
}
Write-Output ("{0:N1}|{1}" -f ($sum / 1MB), ($parts -join ' '))
