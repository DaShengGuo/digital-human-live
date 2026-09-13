# -*- coding: utf-8 -*-
"""test_bench_parse.py — bench 报告解析/判定的单元测试（含 mock 数据路径）。"""
import os
import sys
import time
import unittest
from unittest import mock

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from apps.bench_vram import parse_infer_fps_from_log   # noqa: E402
from apps import gpu_sampler                          # noqa: E402


class TestBenchParse(unittest.TestCase):

    def test_parse_infer_fps(self):
        import tempfile
        with tempfile.NamedTemporaryFile('w', suffix='.log', delete=False, encoding='utf-8') as f:
            f.write('2025-01-01 INFO ------actual avg infer fps:61.5\n')
            f.write('2025-01-01 INFO ------actual avg infer fps:23.0\n')
            path = f.name
        try:
            vals = parse_infer_fps_from_log(path)
            self.assertEqual(len(vals), 2)
            self.assertEqual(min(vals), 23.0)
        finally:
            os.unlink(path)

    def test_parse_missing_file(self):
        self.assertEqual(parse_infer_fps_from_log('Z:/no/such/file.log'), [])
        self.assertEqual(parse_infer_fps_from_log(None), [])


class TestBenchMock(unittest.TestCase):

    def test_gpu_sampler_none_without_smi(self):
        """nvidia-smi 不存在时 sample() 返回 None → bench 可跳过 GPU。"""
        with mock.patch('subprocess.run', side_effect=FileNotFoundError):
            self.assertIsNone(gpu_sampler.sample(timeout_s=2))


if __name__ == '__main__':
    unittest.main(verbosity=2)
