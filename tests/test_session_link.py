# -*- coding: utf-8 -*-
"""test_session_link.py — PageSessionLink 会话粘性。

回归（2026-09-13）: 旧 refresh() 无条件改绑"最新会话", 多开任何标签页
(dashboard/第二个 index.html)都会抢走绑定 → 声音去了没人监听的页面,
"循环话术没声音"且关掉多余标签又自己好, 反复排查无果。
"""
import os
import sys
import unittest
from unittest import mock

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from apps.session_link import PageSessionLink, SessionLinkError  # noqa: E402


def _bound(sid: str) -> PageSessionLink:
    l = PageSessionLink('http://127.0.0.1:8010')
    l.sessionid = sid
    l.connected = True
    l.state = 'connected'
    return l


class TestRefreshPinning(unittest.TestCase):

    def test_keeps_current_when_still_alive(self):
        """当前会话还在 → 绝不改绑（多标签页不得抢走绑定）。"""
        l = _bound('aaa')
        with mock.patch.object(l, '_query_sessions', return_value=['aaa', 'bbb']):
            self.assertTrue(l.refresh())
        self.assertEqual(l.sessionid, 'aaa')
        self.assertTrue(l.connected)

    def test_adopts_newest_when_current_gone(self):
        """当前会话消失（页面刷新/断开）→ 收养最新会话。"""
        l = _bound('aaa')
        with mock.patch.object(l, '_query_sessions', return_value=['bbb', 'ccc']):
            self.assertTrue(l.refresh())
        self.assertEqual(l.sessionid, 'ccc')
        self.assertTrue(l.connected)

    def test_marks_failed_when_no_sessions(self):
        l = _bound('aaa')
        with mock.patch.object(l, '_query_sessions', return_value=[]):
            self.assertFalse(l.refresh())
        self.assertFalse(l.connected)
        self.assertEqual(l.state, 'failed')
        self.assertEqual(l.error, '外部会话消失')

    def test_query_error_keeps_state(self):
        """查询失败（LiveTalking 抖动）→ 保持现有绑定不变。"""
        l = _bound('aaa')
        with mock.patch.object(l, '_query_sessions',
                               side_effect=SessionLinkError('timeout')):
            self.assertTrue(l.refresh())
        self.assertEqual(l.sessionid, 'aaa')
        self.assertTrue(l.connected)


if __name__ == '__main__':
    unittest.main(verbosity=2)
