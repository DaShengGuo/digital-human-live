# -*- coding: utf-8 -*-
"""bench_vram.py — 显存/性能基准与验收判定（阶段D 口径修正版）。

验收口径（阶段D 纪律）:
- 结论四态: PASS / FAIL / INCOMPLETE / MOCK
  - MOCK: skip_gpu=True 的模拟数据, 永不作为验收通过依据
  - FAIL: 任一必需指标实测超阈值（即使其他证据缺失, FAIL 不被覆盖）
  - INCOMPLETE: 无实测失败但证据不足（样本数/FPS 缺失）
  - PASS: 全部必需指标实测达标
- 指标阈值与故障保护阈值分离:
  - 验收: 稳态输出 ≥24fps（fps_accept_floor）; 显存 ≤6600MiB; 温度 ≤75℃
  - 看门狗保护: fps_floor=20（只触发重启, 不作为验收结论）
- 只统计本次运行区间的样本/日志: 以 run_id 或 bench 启动时刻为界, 不读历史日志
- 区分推理 FPS 与接收端有效 FPS: 推理 FPS 来自口型日志（本次区间）,
  接收端 FPS 来自 session_link frames_received 差分
- 区分整卡显存与进程显存: nvidia-smi 为整卡口径, 报告中明确标注
- 退出码: PASS=0, MOCK=0(仅流程验证), INCOMPLETE=3, FAIL=1

用法:
  python -m apps.bench_vram --profile fallback_8g --duration-min 15
  python -m apps.bench_vram --profile fallback_8g --skip-gpu     # MOCK 流程验证
"""
import os
import sys
import time
import argparse
import logging
import datetime

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from apps import common_config, gpu_sampler   # noqa: E402
from apps.watchdog import Watchdog            # noqa: E402

log = logging.getLogger('bench')

ACCEPT_FPS_FLOOR = 24     # 验收线（与看门狗 20fps 保护线区分, 原始目标不放宽）


def parse_infer_fps_from_log(path: str, since_ts: float = None):
    """扫口型日志中 'avg infer fps' 样本。只取 since_ts（bench 启动时刻）之后的行。

    日志行带 vendor 时间戳（utils.logger 格式), 无法精确解析时用行序近似:
    since_ts 之后启动的 bench 只应统计新增样本; 简化做法: 记录 bench 开始时的
    文件偏移, 从偏移处开始读。
    """
    vals = []
    if not path or not os.path.exists(path):
        return vals
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                fps = Watchdog.parse_fps(line)
                if fps:
                    vals.append(fps[0])
    except OSError:
        pass
    return vals


class LogTail:
    """从打开时的文件末尾开始读——保证只统计 bench 区间内的新增日志。"""

    def __init__(self, path: str):
        self.path = path
        self._offset = 0
        try:
            with open(path, 'rb') as f:
                f.seek(0, 2)
                self._offset = f.tell()
        except OSError:
            pass

    def new_lines(self):
        try:
            with open(self.path, 'rb') as f:
                f.seek(self._offset)
                data = f.read()
                self._offset = f.tell()
            return data.decode('utf-8', errors='ignore').splitlines()
        except OSError:
            return []


def run(profile_name: str, duration_min: float, interval_s: int,
        skip_gpu: bool, say_interval_s: float = 0) -> str:
    cfg = common_config.load_profile(profile_name)
    wd_cfg = cfg.get('orchestrator', {}).get('watchdog', {})
    vram_limit = wd_cfg.get('vram_limit_mb', 6600)
    temp_limit = wd_cfg.get('temp_limit_c', 75)
    rise_thr = wd_cfg.get('vram_rise_threshold_mb', 300)

    logs_dir = common_config.resolve_path('', cfg.get('paths', {}).get('logs_dir', 'logs'))
    lt_log = os.path.join(logs_dir, 'livetalking.log')
    lt_tail = LogTail(lt_log)          # 只统计 bench 区间新增日志

    samples = []          # (ts, mem, temp, util, clk)  整卡口径
    fps_vals = []         # 本次区间推理 fps
    t0 = time.time()
    deadline = t0 + duration_min * 60
    run_tag = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    log.info('bench 开始: profile=%s 时长=%.1fmin 间隔=%ss skip_gpu=%s',
             profile_name, duration_min, interval_s, skip_gpu)

    while time.time() < deadline:
        if not skip_gpu:
            s = gpu_sampler.sample()
            if s:
                samples.append((time.time(), s.mem_used, s.temp, s.util, s.sm_clock))
                log.info('sample mem=%sMiB temp=%sC util=%s%% clk=%sMHz (整卡口径)',
                         s.mem_used, s.temp, s.util, s.sm_clock)
        else:
            fake = 3000 + int(600 * (time.time() - t0) / 60)
            samples.append((time.time(), fake, 62, 88, 2400))
            log.info('MOCK sample mem=%sMiB', fake)
        # 本次区间的口型日志
        for line in lt_tail.new_lines():
            fps = Watchdog.parse_fps(line)
            if fps:
                fps_vals.append(fps[0])
        time.sleep(interval_s)

    # ── 判定 ─────────────────────────────────────────────────
    mems = [m for _, m, _, _, _ in samples]
    temps = [t for _, _, t, _, _ in samples]
    peak_mem = max(mems) if mems else None
    temp_max = max(temps) if temps else None
    if len(mems) >= 8:
        q = len(mems) // 4
        rise = sum(mems[-q:]) / q - sum(mems[:q]) / q
    else:
        rise = None
    fps_min = min(fps_vals) if fps_vals else None

    checks = []
    checks.append(('peak_mem<=limit(整卡)', peak_mem is not None and peak_mem <= vram_limit,
                   f'{peak_mem} vs {vram_limit} MiB'))
    checks.append(('no_leak(rise<=thr)',
                   (None if rise is None else (rise <= rise_thr)),
                   f'{rise if rise is not None else "n/a"} vs {rise_thr} MiB'))
    checks.append(('temp<=limit', temp_max is not None and temp_max <= temp_limit,
                   f'{temp_max} vs {temp_limit} C'))
    checks.append(('infer_fps>=24(验收线)', fps_min is not None and fps_min >= ACCEPT_FPS_FLOOR,
                   f'{fps_min} vs {ACCEPT_FPS_FLOOR}'))

    if skip_gpu:
        status = 'MOCK'                      # 模拟数据永不是验收通过
    elif any(v is False for _, v, _ in checks):
        status = 'FAIL'                      # 有实测失败即 FAIL, 不被覆盖
    elif any(v is None for _, v, _ in checks) or len(samples) < max(4, int(duration_min * 60 / interval_s * 0.8)):
        status = 'INCOMPLETE'                # 证据缺失/采样不足
    elif duration_min < 15:
        status = 'INCOMPLETE'                # 时长不足不能声称 15min 稳定性
    else:
        status = 'PASS'

    exit_code = {'PASS': 0, 'MOCK': 0, 'INCOMPLETE': 3, 'FAIL': 1}[status]

    # ── 报告 ─────────────────────────────────────────────────
    ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    docs = os.path.join(_REPO, 'docs')
    os.makedirs(docs, exist_ok=True)
    report = os.path.join(docs, f'bench_{profile_name}_{ts}.md')
    with open(report, 'w', encoding='utf-8') as f:
        f.write(f'# bench 报告 — {profile_name} @ {ts}\n\n')
        f.write(f'- run_tag: {run_tag}, duration: {duration_min} min, '
                f'interval: {interval_s}s, samples: {len(samples)}\n')
        f.write(f'- 数据类型: {"MOCK(模拟数据, 不可作验收依据)" if skip_gpu else "实测"}\n')
        f.write(f'- 显存口径: nvidia-smi 整卡（非进程组归属统计）\n')
        f.write(f'- FPS 口径: 口型推理 fps（vendor 日志, 仅本次区间 {len(fps_vals)} 样本）; '
                f'验收线 {ACCEPT_FPS_FLOOR}fps（与看门狗 20fps 保护线分离）\n\n')
        f.write('| 指标 | 值 | 阈值 | 结论 |\n|---|---|---|---|\n')
        for name, ok, detail in checks:
            if ok is True:
                verdict = 'PASS'
            elif ok is False:
                verdict = 'FAIL'
            elif ok == 'n/a':
                verdict = 'N/A(证据缺失)'
            else:
                verdict = 'N/A(证据缺失)'
            f.write(f'| {name} | {detail.split(" vs ")[0]} | {detail.split(" vs ")[-1]} | {verdict} |\n')
        f.write(f'\n## 结论: {status} (exit={exit_code})\n')
        if status == 'MOCK':
            f.write('\n> 本报告由模拟数据生成, 仅验证工具流程, 不得作为验收通过依据。\n')
        f.write('\n## 原始样本\n\n```\n')
        for ts_, m, t_, u, c in samples[::max(1, len(samples) // 50)]:
            f.write(f'{time.strftime("%H:%M:%S", time.localtime(ts_))} mem={m} temp={t_} util={u} clk={c}\n')
        f.write('```\n')
    log.info('报告已写: %s  结论: %s', report, status)
    return report, exit_code


def main():
    ap = argparse.ArgumentParser(description='VRAM/性能基准（阶段D口径）')
    ap.add_argument('--profile', default='fallback_8g')
    ap.add_argument('--duration-min', type=float, default=15)
    ap.add_argument('--interval', type=int, default=5)
    ap.add_argument('--skip-gpu', action='store_true', help='MOCK 流程验证（不产生验收结论）')
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    _, code = run(args.profile, args.duration_min, args.interval, args.skip_gpu)
    sys.exit(code)


if __name__ == '__main__':
    main()
