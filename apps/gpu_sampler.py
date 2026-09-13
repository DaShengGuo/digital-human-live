# -*- coding: utf-8 -*-
"""GPU 采样工具 — 封装 nvidia-smi，供看门狗与 bench 复用。

只依赖 nvidia-smi（驱动自带），不引入 pynvml，避免多一套依赖。
"""
import subprocess
import re
import logging

log = logging.getLogger('gpu')

# nvidia-smi --query-gpu 输出字段顺序
_QUERY = 'timestamp,memory.total,memory.used,utilization.gpu,temperature.gpu,clocks.sm'
_FMT = 'csv,noheader,nounits'


class GpuSample:
    __slots__ = ('ts', 'mem_total', 'mem_used', 'util', 'temp', 'sm_clock', 'raw')

    def __init__(self, ts, mem_total, mem_used, util, temp, sm_clock, raw):
        self.ts = ts
        self.mem_total = mem_total
        self.mem_used = mem_used
        self.util = util
        self.temp = temp
        self.sm_clock = sm_clock
        self.raw = raw

    def __repr__(self):
        return (f"GpuSample(ts={self.ts} mem={self.mem_used}/{self.mem_total}MiB "
                f"util={self.util}% temp={self.temp}C clk={self.sm_clock}MHz)")


def sample(timeout_s: int = 10):
    """采一次样。nvidia-smi 不存在/失败返回 None（bench 可跳过 GPU）。"""
    try:
        out = subprocess.run(
            ['nvidia-smi', f'--query-gpu={_QUERY}', f'--format={_FMT}'],
            capture_output=True, text=True, timeout=timeout_s
        )
        if out.returncode != 0:
            log.warning('nvidia-smi returncode=%s stderr=%s', out.returncode, out.stderr[:200])
            return None
        return _parse(out.stdout.strip())
    except FileNotFoundError:
        log.warning('nvidia-smi 未找到, 无法采样 GPU')
        return None
    except subprocess.TimeoutExpired:
        log.warning('nvidia-smi 采样超时')
        return None
    except Exception:
        log.exception('GPU 采样异常')
        return None


def _parse(line: str):
    parts = [p.strip() for p in line.split(',')]
    if len(parts) < 6:
        return None
    try:
        # timestamp 可能含逗号前后空格; 时钟可能为 '[N/A]'
        return GpuSample(
            ts=parts[0],
            mem_total=int(parts[1]),
            mem_used=int(parts[2]),
            util=int(parts[3]),
            temp=int(parts[4]),
            sm_clock=int(re.sub(r'\D', '', parts[5]) or 0),
            raw=line,
        )
    except (ValueError, IndexError):
        return None


def has_gpu() -> bool:
    """快速判断本机是否有可用 nvidia-smi。"""
    return sample(timeout_s=5) is not None


# ── 进程级显存（本项目进程组）────────────────────────────────────────────────
# nvidia-smi 在 Windows/WDDM 下 per-process 全是 N/A, 所以走
# scripts/gpu_owned_query.ps1（WMI 性能计数器 + 进程树展开）。
# 单次约 1~6s, 因此带缓存, 不跟着 watchdog 的 5s 采样频率跑。
OWNED_QUERY_REL = ('scripts', 'gpu_owned_query.ps1')

_owned_cache = {'ts': 0.0, 'val': None}


def owned_mem_used(repo_root: str, timeout_s: float = 25.0, cache_s: float = 30.0):
    """本项目进程组的显存占用 → (MB, 明细) 或 None（查不到/超时/不在 Windows）。

    结果缓存 cache_s 秒: watchdog 每 5s 采样, 但 PowerShell 查询较重, 没必要每次跑。
    """
    import os
    import time as _t
    now = _t.time()
    if _owned_cache['val'] is not None or _owned_cache['ts']:
        if now - _owned_cache['ts'] < cache_s:
            return _owned_cache['val']

    _owned_cache['ts'] = now
    _owned_cache['val'] = None
    if os.name != 'nt':
        return None
    script = os.path.join(repo_root, *OWNED_QUERY_REL)
    if not os.path.isfile(script):
        log.warning('未找到 %s, 进程级显存不可用', script)
        return None
    try:
        out = subprocess.run(
            ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', script],
            capture_output=True, text=True, timeout=timeout_s,
            encoding='utf-8', errors='replace')
        line = (out.stdout or '').strip().splitlines()
        line = line[-1].strip() if line else ''
        if not line or line.startswith('ERR'):
            log.warning('进程级显存查询失败: %s', line or (out.stderr or '')[:200])
            return None
        mb_s, _, detail = line.partition('|')
        val = (float(mb_s.replace(',', '')), detail)
        if detail.startswith('no-match'):
            log.warning('进程级显存无匹配实例: %s', detail[:200])
        _owned_cache['val'] = val
        return val
    except subprocess.TimeoutExpired:
        log.warning('进程级显存查询超时(%ss)', timeout_s)
        return None
    except Exception:
        log.exception('进程级显存查询异常')
        return None
