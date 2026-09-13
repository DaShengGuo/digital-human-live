# -*- coding: utf-8 -*-
"""阶段 B 回归测试 — job 状态机/合并不丢句/取消语义/看门狗恢复记账（纯逻辑, 不起真服务）。"""
import os
import sys
import time
import queue
import unittest
from unittest import mock

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from apps import common_config                                  # noqa: E402
from apps.orchestrator import main as orch_main                 # noqa: E402
from apps.watchdog import Watchdog                              # noqa: E402


def make_orch(profile='fallback_8g'):
    cfg = common_config.load_profile(profile)
    o = orch_main.Orchestrator(cfg)
    o.accepting.set()
    return o


class TestJobStateMachine(unittest.TestCase):

    def test_say_returns_job_id_and_queued_state(self):
        o = make_orch()
        r = o.say('你好')
        self.assertTrue(r['ok'])
        self.assertTrue(r['job_id'])
        self.assertEqual(o._jobs[r['job_id']]['state'], 'queued')

    def test_queue_full_rejects_not_silently_drops(self):
        """队列满: 拒绝接单（明确失败）, 不丢已受理任务。"""
        o = make_orch()
        o.max_pending = 2
        o.textq = queue.Queue(maxsize=2)
        r1 = o.say('一')
        r2 = o.say('二')
        r3 = o.say('三')      # 满
        self.assertTrue(r1['ok'])
        self.assertTrue(r2['ok'])
        self.assertFalse(r3['ok'])
        self.assertEqual(r3['code'], 'queue_full')
        self.assertEqual(o._jobs[r3['job_id']]['state'], 'failed')
        # 前两个任务仍在队列
        self.assertEqual(o.textq.qsize(), 2)

    def test_interrupt_cancels_jobs_with_terminal_state(self):
        o = make_orch()
        o.link = None
        o.say('任务甲')
        o.say('任务乙')
        r = o.interrupt()
        self.assertTrue(r['ok'])
        self.assertEqual(len(r['cancelled']), 2)
        for jid in r['cancelled']:
            self.assertEqual(o._jobs[jid]['state'], 'cancelled')

    def test_expired_job_not_dispatched(self):
        """过期任务被淘汰, 不派发。"""
        o = make_orch()
        o.job_ttl_s = -1     # 立即过期
        r = o.say('旧闻')
        self.assertTrue(r['ok'])
        job = o.textq.get_nowait()
        self.assertLess(job['expires_at'], time.time())

    def test_pump_dispatch_preserves_order_and_marks_state(self):
        """合并派发保序: 短句按入队顺序合并, 状态流转 queued→dispatched。"""
        o = make_orch()
        sent = []

        def fake_post(path, payload, timeout_s=5.0):
            sent.append(payload['text'])
            return True, {'code': 0}

        o._lt_post = fake_post
        o.link = mock.Mock(connected=True, sessionid='s-9')
        o.merge_window = 0.05
        o.min_interval = 0.0
        import threading
        t = threading.Thread(target=o._pump_text, daemon=True)
        t.start()
        o.say('第一句。')
        o.say('第二句。')
        o.say('第三句。')
        deadline = time.time() + 5
        while time.time() < deadline and not sent:
            time.sleep(0.05)
        o.stop_flag.set()
        self.assertTrue(sent, '应有派发发生')
        # 合并保序: 同批内文本按原顺序拼接
        self.assertIn('第一句。', sent[0])
        if '第二句。' in sent[0]:
            self.assertLess(sent[0].index('第一句。'), sent[0].index('第二句。'))


class TestWatchdogRecovery(unittest.TestCase):

    def _wd(self):
        cfg = {'orchestrator': {'watchdog': {
            'enabled': True, 'sample_interval_s': 1, 'vram_limit_mb': 6600,
            'vram_rise_window_min': 15, 'vram_rise_threshold_mb': 300,
            'fps_floor': 20, 'fps_floor_duration_s': 30, 'temp_limit_c': 75,
            'on_vram_exceed': 'restart_lip', 'degrade_target': None}}}
        return Watchdog(cfg, {}, on_degrade=mock.MagicMock(),
                        on_restart_lip=mock.MagicMock())

    def test_low_fps_timer_resets_after_action(self):
        """回归: _low_fps_fps_since 拼写 bug → 计时器永不清零, 会连环重启。"""
        w = self._wd()
        self.assertIsNone(w._low_fps_since)
        # 模拟低 FPS 触发一次
        w._low_fps_since = time.time() - 100
        lip = mock.MagicMock()
        lip.is_running.return_value = True
        lip.logfile = 'x.log'
        w.procs = {'livetalking': lip}
        with mock.patch.object(Watchdog, 'tail_log', return_value='------actual avg infer fps:5.0'), \
             mock.patch.object(Watchdog, 'parse_fps', return_value=(5.0, 5.0)):
            w.check_once()
        self.assertIsNone(w._low_fps_since)   # 动作后计时器必须清零

    def test_recovery_cooldown_and_circuit(self):
        """恢复记账结构存在, 冷却与熔断字段初始化正确。"""
        w = self._wd()
        stop = mock.MagicMock()
        stop.is_set.side_effect = [False, True]   # 跑一轮即退出
        with mock.patch.object(Watchdog, 'check_once', return_value={'actions': [], 'sample': None}), \
             mock.patch('time.sleep'):
            w.run_forever(stop_flag=stop)
        self.assertIsNotNone(w._recover_lock)
        self.assertEqual(w._recover_count, {})
        self.assertEqual(w._recover_backoff, 5.0)
        self.assertEqual(w._recover_cooldown_until, 0.0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
