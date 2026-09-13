# -*- coding: utf-8 -*-
"""test_orchestrator.py — 编排器纯逻辑单测（不起真进程、不依赖 GPU）。"""
import os
import sys
import time
import queue
import unittest
from unittest import mock

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from apps.watchdog import Watchdog        # noqa: E402
from apps import gpu_sampler              # noqa: E402
from apps import proc                     # noqa: E402


class FakeSample:
    def __init__(self, mem_used, temp=60):
        self.ts = '00:00:00'
        self.mem_total = 8188
        self.mem_used = mem_used
        self.util = 50
        self.temp = temp
        self.sm_clock = 2400
        self.raw = ''

    def ts2epoch(self):
        return time.time()


def make_watchdog(**over):
    cfg = {'orchestrator': {'watchdog': {
        'enabled': True, 'sample_interval_s': 1, 'vram_limit_mb': 6600,
        'vram_scope': 'total',        # 老用例按整卡口径写; owned 口径见 TestWatchdogVramScope
        'vram_rise_window_min': 15, 'vram_rise_threshold_mb': 300,
        'fps_floor': 20, 'fps_floor_duration_s': 30, 'temp_limit_c': 75,
        'on_vram_exceed': 'degrade', 'degrade_target': 'fallback_8g'}}}
    cfg['orchestrator']['watchdog'].update(over.get('wd', {}))
    w = Watchdog(cfg, {}, on_degrade=mock.MagicMock(), on_restart_lip=mock.MagicMock())
    return w


class TestWatchdog(unittest.TestCase):

    def test_vram_exceed_plans_degrade_deferred_execution(self):
        """阶段E修正: check_once 只产出动作, 执行统一由 run_forever/_execute 串行
        （原实现在 check_once 内同步调用 on_restart_lip → 与 restart_proc 递归栈）。"""
        w = make_watchdog()
        w._hist.clear()
        with mock.patch.object(gpu_sampler, 'sample', return_value=FakeSample(7000)):
            res = w.check_once()
        self.assertIn('degrade', res['actions'])
        w.on_degrade.assert_not_called()     # 检测阶段不执行
        w._execute('degrade')                # run_forever 串行执行
        w.on_degrade.assert_called_with('fallback_8g')
        self.assertTrue(any(e['kind'] == 'vram_exceed' for e in w.events))

    def test_vram_under_limit_no_action(self):
        w = make_watchdog()
        with mock.patch.object(gpu_sampler, 'sample', return_value=FakeSample(4000)):
            res = w.check_once()
        self.assertEqual(res['actions'], [])

    def test_leak_detection(self):
        """单调上涨超过阈值 → 判泄漏 → 降级。"""
        w = make_watchdog()
        w._hist.clear()
        mems = [3000 + i * 40 for i in range(20)]   # +760MB 单调上涨
        for m in mems:
            w._hist.append((time.time(), m))
        leak = w._check_leak()
        self.assertIsNotNone(leak)

    def test_no_leak_with_oscillation(self):
        """正常波动不判泄漏。"""
        w = make_watchdog()
        w._hist.clear()
        mems = [4000, 4100, 3950, 4050, 3980, 4090, 3960, 4070, 3990, 4080]
        for m in mems:
            w._hist.append((time.time(), m))
        self.assertIsNone(w._check_leak())

    def test_temp_alarm_only(self):
        w = make_watchdog()
        with mock.patch.object(gpu_sampler, 'sample', return_value=FakeSample(4000, temp=79)):
            res = w.check_once()
        self.assertEqual(res['actions'], [])       # 只告警不动作
        self.assertTrue(any(e['kind'] == 'temp_high' for e in w.events))

    def test_parse_fps_actual_format(self):
        """LiveTalking 实际日志格式: '------actual avg infer fps:62.3456'"""
        fps = Watchdog.parse_fps('2025-01-01 - x - INFO - ------actual avg infer fps:62.3456')
        self.assertIsNotNone(fps)
        self.assertAlmostEqual(fps[0], 62.3456, places=3)

    def test_parse_fps_legacy_format(self):
        fps = Watchdog.parse_fps('inferfps: 30.2 finalfps: 25.0')
        self.assertEqual(fps, (30.2, 25.0))

    def test_parse_fps_none(self):
        self.assertIsNone(Watchdog.parse_fps('nothing here'))


class TestGpuSampler(unittest.TestCase):

    def test_parse_line(self):
        line = '2025/09/06 12:00:00.000, 8188, 1765, 12, 52, 2400'
        s = gpu_sampler._parse(line)
        self.assertEqual(s.mem_total, 8188)
        self.assertEqual(s.mem_used, 1765)
        self.assertEqual(s.temp, 52)

    def test_parse_na_clock(self):
        line = '2025/09/06 12:00:00.000, 8188, 1765, 12, 52, [N/A]'
        s = gpu_sampler._parse(line)
        self.assertIsNotNone(s)
        self.assertEqual(s.sm_clock, 0)


class TestProcPid(unittest.TestCase):

    def test_pid_file_roundtrip(self):
        import tempfile, json
        d = tempfile.mkdtemp()
        fake = mock.MagicMock()
        fake.popen.pid = 1234
        fake.state = proc.STATE_RUNNING
        fake.cmd = ['python', '-x']
        proc.write_pid_file(d, {'tts': fake})
        data = json.load(open(proc.pid_file(d), encoding='utf-8'))
        self.assertEqual(data['tts']['pid'], 1234)
        proc.clear_pid_file(d)
        self.assertFalse(os.path.exists(proc.pid_file(d)))

    def test_owned_pids_written_for_sampler(self):
        """显存采样器要读 logs/owned_pids.txt（编排器 + 存活子进程）。"""
        import tempfile
        d = tempfile.mkdtemp()
        fake = mock.MagicMock()
        fake.popen.pid = 4321
        fake.state = proc.STATE_RUNNING
        fake.cmd = ['python', '-x']
        proc.write_owned_pids(d, {'tts': fake}, 999)
        got = open(os.path.join(d, 'owned_pids.txt'), encoding='utf-8').read().split()
        self.assertEqual(got, ['999', '4321'])


class TestWatchdogVramScope(unittest.TestCase):
    """显存口径（2026-09-11）: owned=只看本项目进程组, 避免桌面程序把整卡口径顶爆后误杀口型。"""

    def _wd(self, **over):
        cfg = {'orchestrator': {'watchdog': {
            'enabled': True, 'sample_interval_s': 1,
            'vram_scope': 'owned', 'vram_limit_mb': 4000, 'vram_total_cap_mb': 7300,
            'vram_rise_window_min': 15, 'vram_rise_threshold_mb': 300,
            'fps_floor': 20, 'fps_floor_duration_s': 30, 'temp_limit_c': 75,
            'on_vram_exceed': 'restart_lip'}}}
        cfg['orchestrator']['watchdog'].update(over)
        return Watchdog(cfg, {}, on_degrade=mock.MagicMock(), on_restart_lip=mock.MagicMock(),
                        repo_root=None)

    def test_owned_over_limit_restarts(self):
        w = self._wd()
        with mock.patch.object(gpu_sampler, 'sample', return_value=FakeSample(5000)), \
             mock.patch.object(Watchdog, '_owned_vram', return_value=(4500.0, 'p=4500MB')):
            res = w.check_once()
        self.assertIn('restart_lip', res['actions'])
        self.assertTrue(any(e['kind'] == 'vram_exceed' for e in w.events))

    def test_owned_under_limit_while_total_high_only_alerts(self):
        """整卡被 Chrome/VS Code 顶到 7600, 但本链只有 1200 → 只告警, 不重启。"""
        w = self._wd()
        with mock.patch.object(gpu_sampler, 'sample', return_value=FakeSample(7600)), \
             mock.patch.object(Watchdog, '_owned_vram', return_value=(1200.0, 'lip=1200MB')):
            res = w.check_once()
        self.assertEqual(res['actions'], [])
        self.assertTrue(any(e['kind'] == 'vram_total_high' for e in w.events))
        self.assertFalse(any(e['kind'] == 'vram_exceed' for e in w.events))

    def test_owned_unavailable_no_action(self):
        """采样器没起来时宁可不判罚, 也不能退回整卡口径误杀。"""
        w = self._wd()
        with mock.patch.object(gpu_sampler, 'sample', return_value=FakeSample(7600)), \
             mock.patch.object(Watchdog, '_owned_vram', return_value=None):
            res = w.check_once()
        self.assertEqual(res['actions'], [])
        self.assertTrue(any(e['kind'] == 'vram_scope_unavailable' for e in w.events))

    def test_owned_mem_used_parses_query_output(self):
        """gpu_sampler.owned_mem_used 解析 gpu_owned_query.ps1 的一行输出, 且带缓存。"""
        import tempfile
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, 'scripts'), exist_ok=True)
        open(os.path.join(d, 'scripts', 'gpu_owned_query.ps1'), 'w').close()
        fake = mock.MagicMock(returncode=0, stdout='1,234.5|111=1000MB 222=234.5MB\n', stderr='')
        gpu_sampler._owned_cache.update({'ts': 0.0, 'val': None})
        with mock.patch.object(gpu_sampler.subprocess, 'run', return_value=fake) as m:
            got = gpu_sampler.owned_mem_used(d, cache_s=30)
            self.assertIsNotNone(got)
            self.assertAlmostEqual(got[0], 1234.5, places=1)
            self.assertIn('111=1000MB', got[1])
            gpu_sampler.owned_mem_used(d, cache_s=30)     # 第二次命中缓存
            self.assertEqual(m.call_count, 1)
        gpu_sampler._owned_cache.update({'ts': 0.0, 'val': None})

    def test_owned_mem_used_handles_error_and_timeout(self):
        import tempfile
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, 'scripts'), exist_ok=True)
        open(os.path.join(d, 'scripts', 'gpu_owned_query.ps1'), 'w').close()
        for out in ('ERR|no-pids', ''):
            gpu_sampler._owned_cache.update({'ts': 0.0, 'val': None})
            fake = mock.MagicMock(returncode=0, stdout=out, stderr='')
            with mock.patch.object(gpu_sampler.subprocess, 'run', return_value=fake):
                self.assertIsNone(gpu_sampler.owned_mem_used(d, cache_s=30))
        gpu_sampler._owned_cache.update({'ts': 0.0, 'val': None})
        with mock.patch.object(gpu_sampler.subprocess, 'run',
                               side_effect=gpu_sampler.subprocess.TimeoutExpired('x', 1)):
            self.assertIsNone(gpu_sampler.owned_mem_used(d, cache_s=30))
        gpu_sampler._owned_cache.update({'ts': 0.0, 'val': None})


class TestChatProxy(unittest.TestCase):
    """LLM 智能问答透传（/chat → vendor /human type=chat）:
    不进队列、strip 校验、无会话/上游失败都如实上报不静默。"""

    def _orch(self):
        from apps.orchestrator.main import Orchestrator
        return Orchestrator({})

    def test_chat_ok_passes_type_chat(self):
        o = self._orch()
        o.link = mock.MagicMock(connected=True, sessionid='42')
        with mock.patch.object(o, '_lt_post', return_value=(True, {'code': 0})) as m:
            r = o.chat('  用一句话介绍产品  ')
        self.assertTrue(r['ok'])
        self.assertEqual(r['mode'], 'chat')
        path, payload = m.call_args[0]
        self.assertEqual(path, '/human')
        self.assertEqual(payload['type'], 'chat')
        self.assertEqual(payload['text'], '用一句话介绍产品')   # strip 生效
        self.assertEqual(payload['sessionid'], '42')

    def test_chat_empty_text_rejected(self):
        o = self._orch()
        r = o.chat('   ')
        self.assertFalse(r['ok'])
        self.assertEqual(r['code'], 'bad_input')

    def test_chat_no_session(self):
        o = self._orch()
        r = o.chat('你好')
        self.assertFalse(r['ok'])
        self.assertEqual(r['code'], 'no_session')

    def test_chat_upstream_failure_surfaced(self):
        o = self._orch()
        o.link = mock.MagicMock(connected=True, sessionid='42')
        with mock.patch.object(o, '_lt_post', return_value=(False, '业务失败: code=-1')):
            r = o.chat('你好')
        self.assertFalse(r['ok'])
        self.assertEqual(r['code'], 'upstream')
        self.assertTrue(r['msg'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
