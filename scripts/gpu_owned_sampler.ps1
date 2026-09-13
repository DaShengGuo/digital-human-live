# =============================================================================
# 【已废弃 2026-09-11】请用 scripts\gpu_owned_query.ps1
#
# 常驻采样器方案（本文件原实现：循环 + Get-Counter + 落 CSV）实测有两个坑：
#   1) Get-Counter 枚举实例偶发卡住 / 与并发查询竞争；
#   2) 本文件当时以 UTF-8 无 BOM 保存, Windows PowerShell 5.1 按 GBK 解析,
#      中文注释被解析坏 → 脚本静默给出 0（真正的坑）。
# 现方案: apps/gpu_sampler.owned_mem_used() 按需调用 gpu_owned_query.ps1
#         （一次性 WMI 查询 + 缓存 30s），无子进程、无 CSV、无过期数据问题。
#
# 注意: 本目录下所有 .ps1 必须保存为 UTF-8 **带 BOM**，否则 PowerShell 5.1
#       会按 ANSI/GBK 读取，中文注释与字符串都会解析异常。
# =============================================================================
Write-Output "本脚本已废弃，请使用 scripts\gpu_owned_query.ps1（见文件头注释）"
exit 0
