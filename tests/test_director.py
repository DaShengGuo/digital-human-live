# -*- coding: utf-8 -*-
"""导演循环规则单测（用户 2026-09-11 指定规则, 纯逻辑不碰真服务）。

规则:
- 播完一轮(cycle) → 须连续空闲 idle_gap_s → 才播下一轮
- 忙(队列有待播/已派发/正在说话) → 不循环, 且空闲计时从"最后一次忙"重新起算
"""
import os
import sys
import time
import unittest
from unittest import mock

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from apps.director import Director, split_lines   # noqa: E402


def make_director(status=None, gap=300.0):
    d = Director(orch=mock.Mock(RUN_ID='t'),
                 say_fn=mock.Mock(return_value={'ok': True}),
                 interrupt_fn=mock.Mock(),
                 status_fn=lambda: (status or {}),
                 poll_s=0.01, line_pause_s=0.0, idle_gap_s=gap)
    return d


class TestBusy(unittest.TestCase):

    def test_pending_is_busy(self):
        self.assertTrue(make_director({'pending': 2})._busy())

    def test_active_is_busy(self):
        self.assertTrue(make_director({'active': 1})._busy())

    def test_speaking_is_busy(self):
        self.assertTrue(make_director({'speaking': True})._busy())

    def test_idle_not_busy(self):
        self.assertFalse(make_director({'pending': 0, 'active': 0, 'speaking': False})._busy())

    def test_status_error_not_busy(self):
        d = make_director()
        d.status_fn = mock.Mock(side_effect=RuntimeError('down'))
        self.assertFalse(d._busy())


class TestSuperviseLoop(unittest.TestCase):

    def test_cycle_requires_idle_gap(self):
        """刚播完一轮 → 空闲不足 → 不播; 空闲超过 gap → 播。"""
        d = make_director({'pending': 0}, gap=60.0)
        d._play_enabled_scripts_once = mock.Mock()      # 监视是否触发 cycle
        # 刚忙完 10 秒 → 不应开始 cycle
        d.last_busy = time.time() - 10
        d._supervise()
        d._play_enabled_scripts_once.assert_not_called()
        # 空闲 61 秒 → 应开始 cycle
        d.last_busy = time.time() - 61
        d._supervise()
        d._play_enabled_scripts_once.assert_called_once()

    def test_busy_resets_idle_timer(self):
        """忙 → last_busy 刷新, 且不开始 cycle（新文本未读完不许循环）。"""
        d = make_director({'pending': 1, 'speaking': True}, gap=60.0)
        d._play_enabled_scripts_once = mock.Mock()
        d.last_busy = time.time() - 120   # 空闲本来已超
        d._supervise()
        d._play_enabled_scripts_once.assert_not_called()
        self.assertGreater(d.last_busy, time.time() - 2)   # 计时被刷新

    def test_after_cycle_timer_restarts(self):
        """cycle 结束后 idle 计时立即重置（下一轮至少等 gap）。"""
        d = make_director({'pending': 0}, gap=60.0)
        d._play_enabled_scripts_once = mock.Mock()
        d.last_busy = time.time() - 999
        d._supervise()
        d._play_enabled_scripts_once.assert_called_once()
        self.assertGreater(d.last_busy, time.time() - 2)


class TestSplitLines(unittest.TestCase):

    def test_natural_split(self):
        self.assertEqual(split_lines('第一句。第二句！第三句？'),
                         ['第一句。', '第二句！', '第三句？'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
