# -*- coding: utf-8 -*-
"""test_stage_g_ocr.py — 阶段G 弹幕OCR 的纯逻辑测试（不抓屏、不跑 OCR 引擎）。

覆盖:
  1) LineFilter 的过滤/去重/昵称分离规则（实测归纳于 docs/bench_danmaku_ocr_*.md）
  2) 弹幕管线接受 source='ocr'（自动接入的溯源标签）
"""
import os
import sys
import unittest
from unittest import mock

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from apps.danmaku_ocr import LineFilter      # noqa: E402
from apps.danmaku import DanmakuPipeline     # noqa: E402
from apps import storage                     # noqa: E402


class TestLineFilter(unittest.TestCase):

    def test_nickname_split(self):
        f = LineFilter()
        self.assertEqual(f.accept('小明：这个课多少钱', 0.95), ('小明', '这个课多少钱'))

    def test_plain_text_no_nickname(self):
        f = LineFilter()
        self.assertEqual(f.accept('这个课多少钱', 0.95), ('', '这个课多少钱'))

    def test_low_confidence_dropped(self):
        f = LineFilter(min_conf=0.6)
        self.assertIsNone(f.accept('这个课多少钱', 0.31))

    def test_ui_noise_and_numbers_dropped(self):
        f = LineFilter()
        for line in ('在线人数', '点赞 1.2万', '12:34', '2026', '已关注',
                     '已将剪贴板中的内容粘贴到抖音', '好的'):
            self.assertIsNone(f.accept(line, 0.99), msg=f'应被过滤: {line}')

    def test_dedupe_within_ttl(self):
        f = LineFilter(dedupe_ttl=60)
        self.assertIsNotNone(f.accept('老师讲得真好', 0.9))
        self.assertIsNone(f.accept('老师讲得真好', 0.9))          # 60s 内重复
        self.assertIsNone(f.accept('老师讲得真好！！', 0.9))       # 归一化后仍算重复

    def test_dedupe_expired(self):
        f = LineFilter(dedupe_ttl=0)
        self.assertIsNotNone(f.accept('老师讲得真好', 0.9))
        self.assertIsNotNone(f.accept('老师讲得真好', 0.9))        # TTL=0 → 不再算重复


class TestOcrSourceAccepted(unittest.TestCase):

    def _pipe(self):
        return DanmakuPipeline(say_fn=lambda t: {'ok': True})

    def test_ocr_source_passes_validation(self):
        pipe = self._pipe()
        with mock.patch.object(storage, 'query', return_value=[]), \
             mock.patch.object(storage, 'execute'), \
             mock.patch.object(storage, 'log_event'), \
             mock.patch.object(storage, 'session_state', return_value=None):
            r = pipe.ingest('ocr', '观众A', '这个课怎么报名？')
        self.assertTrue(r.get('ok'), r)

    def test_unknown_source_rejected(self):
        pipe = self._pipe()
        r = pipe.ingest('telepathy', '', '乱来源')
        self.assertFalse(r.get('ok'))
        self.assertEqual(r.get('msg'), 'bad source')


if __name__ == '__main__':
    unittest.main(verbosity=2)
