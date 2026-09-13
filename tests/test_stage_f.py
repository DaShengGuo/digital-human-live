# -*- coding: utf-8 -*-
"""阶段F 回归测试 — 冲突检测/重启恢复/人工结束优先（纯逻辑, 不连真 OBS/编排器）。"""
import os
import sys
import datetime
import unittest
from unittest import mock

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from apps import storage                      # noqa: E402
from apps.scheduler import LiveScheduler      # noqa: E402


def make_sched():
    calls = {'start': [], 'end': []}
    s = LiveScheduler(lambda sc, rid: (calls['start'].append(rid) or (True, 'stub')),
                      lambda rid: calls['end'].append(rid))
    s._calls = calls
    return s


NOW = datetime.datetime(2026, 9, 10, 20, 0)    # 固定"当前时间"便于断言

DAILY_2000_60 = {'id': 1, 'name': '晚八点', 'repeat_rule': 'daily',
                 'start_time': '20:00', 'duration_min': 60}
DAILY_2030_30 = {'id': 2, 'name': '重叠的', 'repeat_rule': 'daily',
                 'start_time': '20:30', 'duration_min': 30}
DAILY_2200_60 = {'id': 3, 'name': '不重叠', 'repeat_rule': 'daily',
                 'start_time': '22:00', 'duration_min': 60}


class TestConflict(unittest.TestCase):

    def test_overlap_rejected(self):
        s = make_sched()
        with mock.patch.object(storage, 'list_schedules',
                               return_value=[DAILY_2000_60]):
            c = s.find_conflict(DAILY_2030_30, now=NOW)
        self.assertIsNotNone(c)
        self.assertEqual(c[0], 1)

    def test_no_overlap_ok(self):
        s = make_sched()
        with mock.patch.object(storage, 'list_schedules',
                               return_value=[DAILY_2000_60]):
            self.assertIsNone(s.find_conflict(DAILY_2200_60, now=NOW))

    def test_self_excluded(self):
        s = make_sched()
        with mock.patch.object(storage, 'list_schedules',
                               return_value=[DAILY_2000_60]):
            self.assertIsNone(s.find_conflict(DAILY_2000_60, now=NOW))

    def test_boundary_touching_not_conflict(self):
        """A 20:00-21:00, B 21:00 开始 → 相接不算重叠。"""
        s = make_sched()
        b = {'id': 4, 'name': '接档', 'repeat_rule': 'daily',
             'start_time': '21:00', 'duration_min': 60}
        with mock.patch.object(storage, 'list_schedules',
                               return_value=[DAILY_2000_60]):
            self.assertIsNone(s.find_conflict(b, now=NOW))


class TestRecoveryOnStart(unittest.TestCase):

    def test_open_session_reused_not_relunched(self):
        s = make_sched()
        s.start_fn = lambda sc, rid: (_ for _ in ()).throw(
            AssertionError('不应触发开播'))
        with mock.patch.object(storage, 'query',
                               return_value=[{'id': '20260910-192900-sch4'}]), \
             mock.patch.object(storage, 'list_schedules', return_value=[]), \
             mock.patch.object(storage, 'log_event'):
            s._recover_on_start()     # 不抛异常即通过
        self.assertEqual(s.active_session, '20260910-192900-sch4')

    def test_once_plan_past_fire_not_refired(self):
        """once 计划已过触发点: 标记已发, 重启不会立即重开。"""
        s = make_sched()
        past = dict(DAILY_2000_60, id=9, repeat_rule='once',
                    start_time='2026-09-10 19:50')
        with mock.patch.object(storage, 'query', return_value=[]), \
             mock.patch.object(storage, 'list_schedules', return_value=[past]), \
             mock.patch.object(storage, 'log_event'):
            s._recover_on_start()
        self.assertIn('9', s._fired)


class TestManualEnd(unittest.TestCase):

    def test_manual_end_sets_state_and_stops(self):
        s = make_sched()
        s.active_session = 'run-x'
        with mock.patch.object(storage, 'session_state') as ss, \
             mock.patch.object(storage, 'log_event'), \
             mock.patch.object(storage, 'query', return_value=[]):
            ended = s.manual_end_active()
        self.assertEqual(ended, 'run-x')
        ss.assert_called_with('run-x', 'manual_end', '人工结束')
        self.assertIsNone(s.active_session)
        self.assertIn('run-x', s._calls['end'])

    def test_manual_end_blocks_autorestart_of_same_fire(self):
        """manual_end 后 _tick 释放 active; 该次 fire 时间标记已发, 不再自动重开。"""
        s = make_sched()
        s.active_session = 'run-y'
        fired_key = 'str-fire'
        s._fired[fired_key] = 'marked'
        with mock.patch.object(storage, 'get_session',
                               return_value={'state': 'manual_end', 'started_at': 0}), \
             mock.patch.object(storage, 'list_schedules', return_value=[]), \
             mock.patch.object(storage, 'log_event'):
            s._tick()
        self.assertIsNone(s.active_session)


class TestObsUnavailableHonest(unittest.TestCase):

    def test_unconnected_status_is_unknown(self):
        from apps.obs_control import ObsController
        c = ObsController()
        st = c.output_status()
        self.assertEqual(st['streaming'], 'unknown')   # 不猜测
        ok, msg = c.start_output()
        self.assertFalse(ok)                            # 未连接不得报开播成功


if __name__ == '__main__':
    unittest.main(verbosity=2)
