# -*- coding: utf-8 -*-
"""director — 直播话术导演（阶段E；本轮按用户要求重写循环逻辑）。

新循环规则（用户 2026-09-11 指定）:
- 把启用药方完整播一遍 = 一个 cycle
- cycle 结束后进入 idle 等待 idle_gap_s（默认 300s = 5 分钟）
- idle 期间只要检测到"忙"（队列有待播 / 有已派发未播完 / 会话正在说话），
  就不开始新 cycle，也不重置计时（即: 新文本没读完前绝不循环）
- 只有连续空闲满 idle_gap_s 后，才再播一个 cycle
- "忙"的判定来自编排器 /status（pending/active/speaking）

底层播报仍由 orchestrator 队列承担; 导演只决定何时发起 cycle。
"""
import re
import time
import threading
import logging

from apps import storage

log = logging.getLogger('director')

_SENT_SPLIT = re.compile(r'(?<=[。！？!?；;])')


def split_lines(text: str) -> list:
    """自然分句: 按句号/问号/分号切, 去空行。"""
    return [s.strip() for s in _SENT_SPLIT.split(text) if s.strip()]


class Director:
    def __init__(self, orch, say_fn, interrupt_fn, status_fn=None,
                 poll_s: float = 2.0, line_pause_s: float = 1.0,
                 idle_gap_s: float = 300.0):
        """orch: 取 run_id; say_fn/interrupt_fn: 派发接口;
        status_fn: 返回编排器 /status dict（判定忙闲）;
        poll_s: 状态轮询间隔; line_pause_s: 句间停顿;
        idle_gap_s: 一个 cycle 结束后, 需连续空闲多久才开始下一个 cycle（默认 300s）。"""
        self.orch = orch
        self.say = say_fn
        self.interrupt = interrupt_fn
        self.status_fn = status_fn or (lambda: {})
        self.poll_s = poll_s
        self.line_pause_s = line_pause_s
        self.idle_gap_s = idle_gap_s
        self.stop_flag = threading.Event()
        self.paused = threading.Event()      # 暂停讲解（人工控制）
        self.manual = threading.Event()      # 人工接管: 停自动派发并锁定
        self.current = {'script_id': None, 'line_index': 0, 'line': ''}
        self.last_busy = time.time()         # 最近一次"忙"的时刻
        self.state_name = 'idle-wait'

    def _busy(self) -> bool:
        try:
            s = self.status_fn() or {}
        except Exception:
            return False
        if s.get('pending', 0) or s.get('active', 0):
            return True
        if s.get('speaking'):
            return True
        return False

    def run_forever(self):
        while not self.stop_flag.is_set():
            if self.manual.is_set():
                self.state_name = 'takeover'
                time.sleep(1)
                continue
            if self.paused.is_set():
                self.state_name = 'paused'
                time.sleep(0.5)
                continue
            try:
                self._supervise()
            except Exception:
                log.exception('导演轮次异常')
                time.sleep(5)

    def _supervise(self):
        """单步决策: 忙→更新 last_busy 并等待; 空闲不足 gap→等待; 空闲够→播一个 cycle。"""
        if self._busy():
            self.last_busy = time.time()
            self.state_name = 'busy'
            time.sleep(self.poll_s)
            return
        if time.time() - self.last_busy < self.idle_gap_s:
            self.state_name = 'idle-wait'
            time.sleep(self.poll_s)
            return
        # 连续空闲达标 → 播放一个完整 cycle（期间若变忙, cycle 内不强制, 但会自然排队）
        self.state_name = 'cycle'
        self._play_enabled_scripts_once()
        # cycle 结束后把基准重置到现在, 开始下一段 5 分钟空闲计时
        self.last_busy = time.time()

    def _wait_speaking_started(self, max_wait_s: float = 30.0) -> bool:
        """等这一句**开口**（speaking=True），最多 max_wait_s 秒。

        直播要的是"上一句还在说，下一句已经排上"，中间不留空窗，所以等"开口"
        而不是等"播完"。合成慢（缓存未命中，CPU RTF≈10）时最多等 30s 就继续派，
        句子会在 LiveTalking 里排队顺序播 —— 听感依旧连续，且不会丢句
        （max_pending 由编排器把守，不是靠这里硬等）。
        """
        t0 = time.time()
        while time.time() - t0 < max_wait_s:
            if self.stop_flag.is_set() or self.manual.is_set() or self.paused.is_set():
                return False
            try:
                s = self.status_fn() or {}
            except Exception:
                s = {}
            if s.get('speaking'):
                return True
            time.sleep(0.3)
        return False

    def _play_enabled_scripts_once(self):
        scripts = storage.list_scripts(enabled_only=True)
        if not scripts:
            return
        for sc in scripts:
            if self.stop_flag.is_set() or self.manual.is_set() or self.paused.is_set():
                return
            lines = split_lines(sc['content'])
            start = storage.get_progress(self.orch_run_id(), sc['id'])
            if start >= len(lines):
                start = 0    # 一轮结束, 下一轮从头（保留进度语义: 整场循环）
            for i in range(start, len(lines)):
                if self.stop_flag.is_set() or self.manual.is_set() or self.paused.is_set():
                    return
                self.current = {'script_id': sc['id'], 'line_index': i, 'line': lines[i]}
                r = self.say(lines[i])
                if not r.get('ok'):
                    time.sleep(2)
                    continue
                # 等这句开口再派下一句（流水线）: 直播间不留空窗
                if not self._wait_speaking_started():
                    return
                time.sleep(self.line_pause_s)
                storage.save_progress(self.orch_run_id(), sc['id'], i + 1)
        storage.save_progress(self.orch_run_id(), sc['id'], 0)   # 一轮完成, 进度归零

    def orch_run_id(self):
        return getattr(self.orch, 'RUN_ID', 'adhoc')

    def status(self) -> dict:
        s = {'state': self.state_name,
             'idle_left': max(0.0, self.idle_gap_s - (time.time() - self.last_busy)),
             'current': self.current['line'][:30]}
        return s

    def pause(self):
        self.paused.set()
        storage.log_event('manual', '暂停讲解')

    def resume(self):
        self.paused.clear()
        storage.log_event('manual', '恢复讲解')

    def takeover(self):
        self.manual.set()
        self.interrupt()
        storage.log_event('manual', '人工接管（自动派发已锁定）')

    def release(self):
        self.manual.clear()
        self.last_busy = time.time()    # 解除后重新计时, 避免立刻抢播
        storage.log_event('manual', '解除人工接管, 恢复自动讲解')

    def stop(self):
        self.stop_flag.set()
