# -*- coding: utf-8 -*-
"""看门狗 — 周期采样 GPU，检测 OOM / FPS 崩 / 显存爬升 / 温度超标，触发降级或重启。

判定规则（阈值全部来自 profile 的 orchestrator.watchdog 段）:
- vram_used > vram_limit_mb            → on_vram_exceed 动作（degrade / restart_lip / alert_only）
- 口型进程日志 inferfps/finalfps < fps_floor 持续 fps_floor_duration_s → 重启口型
- vram_rise_window_min 内显存单调上涨 > vram_rise_threshold_mb       → 判泄漏 → 降级
- temp > temp_limit_c                  → 告警日志（笔记本红线 75C）
- 子进程退出（非 stop 期间）            → 自动重启口型进程
"""
import os
import re
import time
import logging
import threading
from collections import deque

from apps import gpu_sampler
from apps import proc

log = logging.getLogger('watchdog')


class Watchdog:
    def __init__(self, cfg: dict, procs: dict, on_degrade=None, on_restart_lip=None,
                 on_restart_tts=None, repo_root=None):
        """cfg: 整份 profile; procs: {name: ManagedProcess}
        on_degrade(target): 降级回调（由 orchestrator 提供实现）
        on_restart_lip():   重启口型进程回调
        on_restart_tts():   重启 TTS 进程回调（阶段B补全）
        repo_root: 仓库根目录（vram_scope=owned 时用来定位进程级显存查询脚本）
        """
        self.cfg = cfg
        self.procs = procs
        self.repo_root = repo_root
        self.on_degrade = on_degrade or (lambda target: None)
        self.on_restart_lip = on_restart_lip or (lambda: None)
        self.on_restart_tts = on_restart_tts
        self._owned_warned = False
        self._apply_cfg(cfg)
        # 显存历史: [(ts, mem_used)]
        self._hist = deque(maxlen=int(self.rise_window_min * 60 / self.interval) + 10)
        # FPS 低速区间
        self._low_fps_since = None
        self.events = []          # 事件记录, healthcheck/验收直接读

    def _apply_cfg(self, cfg: dict):
        wd = (cfg.get('orchestrator', {}).get('watchdog', {}) or {})
        self.enabled = wd.get('enabled', True)
        self.interval = wd.get('sample_interval_s', 5)
        # vram_scope: owned=只统计本项目进程组(默认, 避免桌面程序误伤);
        #             total=整卡口径(上游行为, 8G 笔记本上易误判)
        self.vram_scope = wd.get('vram_scope', 'owned')
        self.vram_limit = wd.get('vram_limit_mb', 4000)
        self.vram_total_cap = wd.get('vram_total_cap_mb', 7300)
        self.owned_cache_s = wd.get('vram_owned_cache_s', 30)
        self.owned_timeout_s = wd.get('vram_owned_timeout_s', 25)
        self.rise_window_min = wd.get('vram_rise_window_min', 15)
        self.rise_threshold = wd.get('vram_rise_threshold_mb', 300)
        self.fps_floor = wd.get('fps_floor', 20)
        self.fps_floor_dur = wd.get('fps_floor_duration_s', 30)
        self.temp_limit = wd.get('temp_limit_c', 75)
        self.on_vram_exceed = wd.get('on_vram_exceed', 'degrade')
        self.degrade_target = wd.get('degrade_target')

    def reload(self, cfg: dict, procs: dict):
        """降级/换档后原位刷新阈值与进程引用（run_forever 线程继续用同一对象）。"""
        self.cfg = cfg
        self.procs = procs
        self._apply_cfg(cfg)
        self._hist.clear()
        self._low_fps_since = None
        self._event('reload', f'watchdog 阈值随档位刷新 (limit={self.vram_limit}MiB)')

    # ── 事件 ────────────────────────────────────────────────
    def _event(self, kind: str, detail: str):
        rec = {'ts': time.strftime('%Y-%m-%d %H:%M:%S'), 'kind': kind, 'detail': detail}
        self.events.append(rec)
        log.warning('[watchdog] %s: %s', kind, detail)

    # ── FPS 解析 ────────────────────────────────────────────
    @staticmethod
    def parse_fps(log_text: str):
        """从 LiveTalking 日志行提取推理 FPS。

        实际格式 (base_avatar.py): '------actual avg infer fps:62.3456'
        兼容 'inferfps:/finalfps:' 旧格式。返回 (infer, final) 或 None。
        """
        m = re.search(r'inferfps[:=]\s*([\d.]+).*?finalfps[:=]\s*([\d.]+)', log_text, re.I)
        if m:
            return float(m.group(1)), float(m.group(2))
        m = re.search(r'avg infer fps[:=]\s*([\d.]+)', log_text, re.I)
        if m:
            v = float(m.group(1))
            return v, v   # 官方只输出一个推理帧率, 两个通道同值
        return None

    def tail_log(self, path: str, nbytes: int = 8192) -> str:
        try:
            with open(path, 'rb') as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - nbytes))
                return f.read().decode('utf-8', errors='ignore')
        except OSError:
            return ''

    # ── 单次检查 ────────────────────────────────────────────
    def check_once(self) -> dict:
        """跑一轮检查, 返回本轮结果 dict（bench 也复用它做单点判定）。"""
        result = {'ts': time.time(), 'sample': None, 'actions': []}
        if not self.enabled:
            return result

        s = gpu_sampler.sample()
        result['sample'] = s
        if s is None:
            # GPU 采样失败也要查进程（不因采样问题漏掉崩溃）
            for name, p in self.procs.items():
                p.poll()
                if p.state == proc.STATE_EXITED:
                    self._event('proc_exit', f'{name} exited rc={p.exit_code} (GPU采样失败时检出)')
                    result['actions'].append(f'restart_{name}')
            return result

        # 显存口径: owned=只看本项目进程组(推荐); total=整卡(上游行为, 桌面程序会算进来)
        if self.vram_scope == 'owned':
            owned = self._owned_vram()
            vram_used = owned[0] if owned else None
            scope = '本链'
        else:
            vram_used, scope = s.mem_used, '整卡'

        if vram_used is None:
            # 进程级采样不可用(采样器没起来/数据过期): 宁可不动手, 也不用整卡口径误杀 ——
            # Chrome/VS Code/直播伴侣 的占用不是本链能修的, 拿它触发重启就是误判。
            if not self._owned_warned:
                self._event('vram_scope_unavailable',
                            '进程级显存采样不可用 → 本轮不做显存判罚(只保留整卡告警); '
                            '查 logs/gpu_owned.csv 与 scripts/gpu_owned_sampler.ps1')
                self._owned_warned = True
        else:
            self._hist.append((s.ts2epoch(), vram_used))
            # 1) 显存超限 → 只记录动作, 不在此同步执行（执行/冷却/熔断统一在 run_forever）
            if vram_used > self.vram_limit:
                self._event('vram_exceed',
                            f'{scope} {vram_used:.0f}MiB > limit {self.vram_limit}MiB'
                            + (f' (整卡 {s.mem_used}MiB)' if scope == '本链' else ''))
                result['actions'].append(self._plan_vram_exceed())

        # 1b) 整卡接近满(含桌面程序) → 只告警, 不动进程: 那不是本链能修的问题
        if self.vram_scope == 'owned' and s.mem_used > self.vram_total_cap:
            self._event('vram_total_high',
                        f'整卡 {s.mem_used}MiB > {self.vram_total_cap}MiB (含桌面程序, 仅告警)')

        # 2) 显存爬升（泄漏判定）→ 只记动作, 执行统一在 run_forever
        leak = self._check_leak()
        if leak:
            self._event('vram_leak', leak)
            result['actions'].append('degrade')

        # 3) 温度
        if s.temp > self.temp_limit:
            self._event('temp_high', f'{s.temp}C > {self.temp_limit}C — 建议降负载(720p/低batch)')

        # 4) FPS 地板（保护线, 与验收 24fps 分离）
        lip = self.procs.get('livetalking')
        if lip and lip.is_running() and lip.logfile:
            tail = self.tail_log(lip.logfile)
            fps = self.parse_fps(tail)
            if fps:
                low = min(fps) < self.fps_floor
                if low and self._low_fps_since is None:
                    self._low_fps_since = time.time()
                elif not low:
                    self._low_fps_since = None
                elif time.time() - self._low_fps_since > self.fps_floor_dur:
                    self._event('fps_floor', f'infer/final < {self.fps_floor} 持续 {self.fps_floor_dur}s')
                    result['actions'].append('restart_lip')
                    self._low_fps_since = None    # 修复: 原拼写 _low_fps_fps_since, 计时器永不清零

        # 5) 进程意外退出（先刷新状态; GPU 采样失败时该检查仍会执行 — 见 run_forever）
        for name, p in self.procs.items():
            p.poll()
            if p.state == proc.STATE_EXITED:
                self._event('proc_exit', f'{name} exited rc={p.exit_code}')
                result['actions'].append(f'restart_{name}')

        return result

    def _owned_vram(self):
        """本项目进程组的显存占用 (MB, 明细); 采样不可用返回 None。"""
        if not self.repo_root:
            return None
        return gpu_sampler.owned_mem_used(
            self.repo_root,
            timeout_s=self.owned_timeout_s,
            cache_s=self.owned_cache_s)

    def _plan_vram_exceed(self) -> str:
        if self.on_vram_exceed == 'degrade' and self.degrade_target:
            return 'degrade'
        if self.on_vram_exceed == 'restart_lip':
            return 'restart_lip'
        return 'alert_only'

    def _check_leak(self):
        """窗口内首尾对比: 上涨超过阈值且整体单调 → 泄漏。"""
        if len(self._hist) < 10:
            return None
        pts = list(self._hist)
        rise = pts[-1][1] - pts[0][1]
        if rise <= self.rise_threshold:
            return None
        # 单调性: 允许 2 次以内回落
        drops = sum(1 for a, b in zip(pts, pts[1:]) if b[1] < a[1])
        if drops <= 2:
            return (f'{pts[0][1]}→{pts[-1][1]}MiB (+{rise}) over {self.rise_window_min}min, drops={drops}')
        return None

    # ── 循环（阶段B: 恢复动作串行化 + 退避/冷却/熔断）─────────
    def run_forever(self, stop_flag=None):
        log.info('watchdog 启动 interval=%ss limit=%sMiB scope=%s',
                 self.interval, self.vram_limit, self.vram_scope)
        self._recover_lock = threading.Lock()   # 恢复动作串行: 同一时刻只执行一个
        self._recover_count = {}                # name → 次数（熔断计数）
        self._recover_cooldown_until = 0.0      # 冷却截止
        self._recover_backoff = 5.0
        MAX_RECOVERS = 10                       # 熔断阈值: 连续恢复超限 → 只告警不再重启
        while not (stop_flag and stop_flag.is_set()):
            try:
                res = self.check_once()
                # 动作执行集中在 run_forever: 串行 + 冷却 + 退避 + 熔断
                for action in res.get('actions', []):
                    if action == 'alert_only':
                        continue
                    now = time.time()
                    if now < self._recover_cooldown_until:
                        log.warning('[watchdog] 冷却中, 跳过动作 %s', action)
                        continue
                    cnt = self._recover_count.get(action, 0) + 1
                    self._recover_count[action] = cnt
                    if cnt > MAX_RECOVERS:
                        self._event('recover_circuit_open',
                                    f'{action} 已连续 {cnt} 次, 熔断: 不再自动恢复, 请人工介入')
                        continue
                    self._execute(action)
                    # 指数退避: 5s → 10s → 20s → 40s（上限 60s）
                    self._recover_cooldown_until = now + min(self._recover_backoff * (2 ** (cnt - 1)), 60)
            except Exception:
                log.exception('watchdog 轮次异常')
            time.sleep(self.interval)

    def _execute(self, action: str):
        """恢复动作分派（串行, 单一入口）。"""
        log.info('[watchdog] 执行恢复动作: %s', action)
        if action == 'degrade':
            self.on_degrade(self.degrade_target)
        elif action in ('restart_lip', 'restart_livetalking'):
            self.on_restart_lip()
        elif action == 'restart_tts':
            if self.on_restart_tts:
                self.on_restart_tts()
            else:
                self._event('no_handler', 'TTS 恢复无处理器（配置未接入）')


def _ts2epoch_patch(self):
    return time.time()
# GpuSample.ts 是文本时间戳, 给它补一个数值化方法
gpu_sampler.GpuSample.ts2epoch = _ts2epoch_patch
