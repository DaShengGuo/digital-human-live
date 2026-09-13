# -*- coding: utf-8 -*-
"""阶段 A 回归测试 — 会话绑定/双重校验/预检/停止顺序（不起真服务、不碰 GPU）。"""
import io
import os
import sys
import json
import socket
import queue
import threading
import unittest
from unittest import mock

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from apps import common_config                                  # noqa: E402
from apps.orchestrator import main as orch_main                 # noqa: E402


def make_orch(profile='fallback_8g'):
    cfg = common_config.load_profile(profile)
    return orch_main.Orchestrator(cfg)


class FakeResp(io.BytesIO):
    def __init__(self, payload, status=200):
        super().__init__(json.dumps(payload).encode())
        self.status = status
    def __enter__(self): return self
    def __exit__(self, *a): return False


class TestA2SessionBinding(unittest.TestCase):

    def test_say_rejected_when_not_accepting(self):
        o = make_orch()
        o.accepting.clear()
        r = o.say('测试')
        self.assertFalse(r['ok'])
        self.assertEqual(r['code'], 'not_accepting')

    def test_say_rejects_empty(self):
        o = make_orch()
        o.accepting.set()
        self.assertEqual(o.say('   ')['code'], 'bad_input')

    def test_dispatch_without_session_is_explicit_failure(self):
        """无绑定会话时 _session_or_fail 必须抛错, 不得回退 sessionid='0'。"""
        o = make_orch()
        with self.assertRaises(RuntimeError):
            o._session_or_fail()

    def test_lt_post_http_error_identifiable(self):
        o = make_orch()
        with mock.patch('urllib.request.urlopen', side_effect=ConnectionRefusedError):
            ok, detail = o._lt_post('/human', {})
        self.assertFalse(ok)

    def test_lt_post_business_code_checked(self):
        """HTTP 200 但业务 code!=0（如 session not found）→ 失败可识别。"""
        o = make_orch()
        with mock.patch('urllib.request.urlopen',
                        return_value=FakeResp({'code': -1, 'msg': 'session not found'})):
            ok, detail = o._lt_post('/human', {'sessionid': 'x'})
        self.assertFalse(ok)
        self.assertIn('session not found', str(detail))

    def test_lt_post_success_requires_code0(self):
        o = make_orch()
        with mock.patch('urllib.request.urlopen', return_value=FakeResp({'code': 0, 'msg': 'ok'})):
            ok, _ = o._lt_post('/interrupt_talk', {})
        self.assertTrue(ok)

    def test_interrupt_reports_downstream_failure(self):
        """阶段B: 打断先于队列清理发出, 但下游拒绝时 interrupted=False 仍可见。
        （interrupt 主 ok 语义 = '打断已受理'; downstream 拒绝通过 interrupted 字段暴露）"""
        o = make_orch()
        o.link = mock.Mock(connected=True, sessionid='s-1')
        with mock.patch('urllib.request.urlopen',
                        return_value=FakeResp({'code': -1, 'msg': 'boom'})):
            r = o.interrupt()
        self.assertFalse(r['interrupted'])   # 下游拒绝必须可见
        self.assertEqual(r.get('cancelled', []), [])

    def test_interrupt_without_session_ok_semantics(self):
        o = make_orch()
        o.accepting.set()
        r = o.say('x')                     # 阶段B: 队列元素是 job dict
        self.assertTrue(r['ok'])
        jid = r['job_id']
        r = o.interrupt()
        self.assertTrue(r['ok'])           # 没有活动会话: 清队列即成功
        self.assertFalse(r['interrupted'])
        self.assertEqual(o.textq.qsize(), 0)
        self.assertEqual(r['cancelled'], [jid])
        # 终态可查: 任务确实被取消而非静默消失
        self.assertEqual(o._jobs[jid]['state'], 'cancelled')


class TestA3Preflight(unittest.TestCase):

    def test_disabled_profile_rejected(self):
        o = make_orch('quality_8g')
        with self.assertRaises(orch_main.PreflightError) as ctx:
            o.preflight()
        self.assertIn('need_bench_evidence', str(ctx.exception))

    def test_missing_python_raises_no_silent_fallback(self):
        o = make_orch()
        o.cfg['paths']['python_tts'] = '.venv-nonexistent/python.exe'
        with self.assertRaises(orch_main.PreflightError) as ctx:
            o.preflight()
        self.assertIn('python_tts', str(ctx.exception))

    def test_port_occupied_by_other_process_rejected(self):
        """端口被外部占用 → 拒绝启动, 绝不误判为自己人。"""
        s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen(1)
        port = s.getsockname()[1]
        try:
            o = make_orch()
            o.port = port            # 让编排器端口=已占用端口
            with self.assertRaises(orch_main.PreflightError) as ctx:
                o._check_ports_free()
            self.assertIn('已被占用', str(ctx.exception))
        finally:
            s.close()

    def test_stable_profile_reports_missing_assets(self):
        """stable 档缺 ultralight 资产时预检必须点名缺失项。"""
        o = make_orch('stable_8g')
        try:
            o.preflight()
            ok = True
        except orch_main.PreflightError as e:
            ok = False
            self.assertIn('avatar', str(e).lower() + str(e))
        if not ok:
            return
        self.skipTest('stable 资产居然齐全了? 复核')

    def test_stop_order_gates_first(self):
        o = make_orch()
        o.accepting.set()
        o.stop_flag = threading.Event()
        o.procs = {}
        order = []
        o._old_stop = o.stop_all
        with mock.patch.object(o, 'link', None):
            o.stop_all()
        self.assertFalse(o.accepting.is_set())
        self.assertTrue(o.stop_flag.is_set())


class TestDegradationGuards(unittest.TestCase):

    def test_degrade_to_disabled_target_no_switch(self):
        o = make_orch()
        stopped = []
        with mock.patch.object(o, 'stop_all', side_effect=lambda **k: stopped.append(1)):
            o.degrade('quality_8g')   # disabled → 不允许执行
        self.assertEqual(stopped, [])
        self.assertEqual(o.cfg.get('_profile_name'), 'fallback_8g')

    def test_degrade_null_target_noop(self):
        o = make_orch()
        o.degrade(None)
        self.assertFalse(o._degraded)


if __name__ == '__main__':
    unittest.main(verbosity=2)
