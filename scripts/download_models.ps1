# =============================================================================
# download_models.ps1 — 模型下载/检查（只文档化命令, 不自动下载网盘资源）
# 用法: powershell -ExecutionPolicy Bypass -File scripts\download_models.ps1 [-Profile stable] [-Only check]
# 纪律: 不下载与当前 profile 无关的大模型
# =============================================================================
param(
    [string]$Profile = "stable",
    [ValidateSet("check", "list")]
    [string]$Only = "check"
)
$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

function Test-Dir([string]$p) { Test-Path (Join-Path $RepoRoot $p) }

Write-Host "== 模型需求清单 (profile=$Profile) ==" -ForegroundColor Cyan

$Needs = [ordered]@{}

if ($Profile -in @("stable", "all")) {
    $Needs["CosyVoice-300M-SFT"] = @{
        Path    = "vendor\CosyVoice\pretrained_models\CosyVoice-300M-SFT"
        Size    = "~5.4GB"
        Get     = "modelscope: iic/CosyVoice-300M-SFT (含内置音色 spk2info.pt)"
        Command = "python -c `"from modelscope import snapshot_download; snapshot_download('iic/CosyVoice-300M-SFT', local_dir='vendor/CosyVoice/pretrained_models/CosyVoice-300M-SFT')`""
        Note    = "TTS 主模型 (SFT 内置音色: 中文女/男等); 必需"
    }
    $Needs["ultralight avatar 资产"] = @{
        Path    = "vendor\LiveTalking\data\avatars\ultralight_avatar1"
        Size    = "~1-2GB"
        Get     = "LiveTalking 官方网盘 (夸克/GDrive, 见 vendor README 2.1)"
        Command = "# 手动下载 ultralight_avatar1 包解压到该目录 (含 full_imgs/ face_imgs/ coords.pkl ultralight.pth)"
        Note    = "数字人形象; 必需"
    }
    $Needs["hubert-large-ls960-ft"] = @{
        Path    = "vendor\LiveTalking\models\hubert-large-ls960-ft"
        Size    = "~2.5GB"
        Get     = "ModelScope: facebook/hubert-large-ls960-ft (HF 直连限流严重, 实测用 modelscope)"
        Command = "python -c `"from modelscope import snapshot_download; snapshot_download('facebook/hubert-large-ls960-ft', local_dir='vendor/LiveTalking/models/hubert-large-ls960-ft')`""
        Note    = "ultralight 音频特征; 必需 (已验证 modelscope 源 27MB/s, 约 1 分钟)"
    }
    $Needs["人脸检测权重 (仅重做 avatar 时需要)"] = @{
        Path    = "vendor\LiveTalking\models\scrfd_2.5g_kps.onnx"
        Size    = "~2.5MB"
        Get     = "Ultralight-Digital-Human 官方仓库 releases"
        Command = "# 手动下载, 连同 checkpoint_epoch_335.pth.tar 放到 vendor/LiveTalking/models/"
        Note    = "仅 genavatar 重新生成形象时需要; 用现成 avatar 可跳过"
    }
}

if ($Profile -in @("fallback", "all")) {
    $Needs["wav2lip.pth (wav2lip256)"] = @{
        Path    = "vendor\LiveTalking\models\wav2lip.pth"
        Size    = "~400MB"
        Get     = "LiveTalking 官方网盘 (README 2.1: wav2lip256.pth 重命名)"
        Command = "# 手动下载"
        Note    = "兼容档口型; 按需"
    }
    $Needs["wav2lip256_avatar1"] = @{
        Path    = "vendor\LiveTalking\data\avatars\wav2lip256_avatar1"
        Size    = "~1-2GB"
        Get     = "LiveTalking 官方网盘 (README 2.1)"
        Command = "# 手动下载解压"
        Note    = "兼容档形象; 按需"
    }
}

if ($Profile -in @("quality", "all")) {
    $Needs["MuseTalk 模型组"] = @{
        Path    = "vendor\LiveTalking\models\musetalk*"
        Size    = "~4GB"
        Get     = "LiveTalking 官方文档 (musetalk 部分)"
        Command = "# 手动下载; 未 bench 通过前不下载"
        Note    = "画质档专用; bench 前禁止拉取"
    }
}

# ── 检查 / 列出 ───────────────────────────────────────────────
foreach ($name in $Needs.Keys) {
    $n = $Needs[$name]
    $exists = $false
    if ((Test-Path (Join-Path $RepoRoot $n.Path)) -and ((Get-ChildItem (Join-Path $RepoRoot $n.Path) -ErrorAction SilentlyContinue | Measure-Object).Count -gt 0)) { $exists = $true }
    $mark = if ($exists) { "[OK]  " } else { "[MISS]" }
    $color = if ($exists) { "Green" } else { "Yellow" }
    Write-Host "$mark $name  ($($n.Size))  → $($n.Path)" -ForegroundColor $color
    Write-Host "       来源: $($n.Get)"
    Write-Host "       获取: $($n.Command)"
    Write-Host "       说明: $($n.Note)"
}

if ($Only -eq "check") {
    Write-Host "`n提示: [MISS] 项按上面命令获取; modelscope 下载建议先`$env:HF_ENDPOINT='https://hf-mirror.com'"
}
