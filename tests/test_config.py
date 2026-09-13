# -*- coding: utf-8 -*-
"""test_config.py — 配置加载/参数映射的单元测试。

运行: python -m pytest tests/test_config.py -v   （或 python -m unittest）
无 pytest 时也可直接 python tests/test_config.py
"""
import os
import sys
import unittest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from apps import common_config   # noqa: E402


class TestLoadProfiles(unittest.TestCase):

    def test_default_profile_exists(self):
        d = common_config.load_default()
        self.assertIn('default_profile', d)
        self.assertTrue(d['default_profile'])

    def test_load_stable(self):
        cfg = common_config.load_profile('stable_8g')
        self.assertEqual(cfg['_profile_name'], 'stable_8g')
        self.assertEqual(cfg['livetalking']['model'], 'ultralight')
        self.assertEqual(cfg['tts']['device'], 'cpu')   # 硬约束: stable 档 TTS 必须 CPU

    def test_load_fallback(self):
        cfg = common_config.load_profile('fallback_8g')
        self.assertEqual(cfg['livetalking']['model'], 'wav2lip')

    def test_quality_batch_size_2(self):
        """纪律: MuseTalk batch 从 2 起, 禁止默认 8/16。"""
        cfg = common_config.load_profile('quality_8g')
        self.assertEqual(cfg['livetalking']['batch_size'], 2)
        self.assertEqual(cfg['livetalking']['model'], 'musetalk')

    def test_disabled_profile_reason(self):
        allowed, reason = common_config.is_profile_enabled('quality_8g')
        # 当前 default.yaml 把 quality 标记为 need_bench_evidence
        self.assertFalse(allowed)
        self.assertIn('need_bench_evidence', reason)

    def test_vram_limit_all_profiles(self):
        """所有档位看门狗显存上限不得超过 6600MiB（含 OBS 预算的硬线）。"""
        for name in ('stable_8g', 'quality_8g', 'fallback_8g'):
            cfg = common_config.load_profile(name)
            limit = cfg['orchestrator']['watchdog']['vram_limit_mb']
            self.assertLessEqual(limit, 6600, f'{name} 显存上限超标: {limit}')

    def test_tts_ports_unique(self):
        """LiveTalking(8010) / TTS(8011) / 编排器(8020) 端口不得冲突。"""
        for name in ('stable_8g', 'quality_8g', 'fallback_8g'):
            cfg = common_config.load_profile(name)
            ports = {cfg['livetalking']['listenport'],
                     cfg['tts']['listen_port'],
                     cfg['orchestrator']['listen_port']}
            self.assertEqual(len(ports), 3, f'{name} 端口冲突: {ports}')


class TestCliArgs(unittest.TestCase):

    def test_livetalking_cli_mapping(self):
        cfg = common_config.load_profile('stable_8g')
        args = common_config.livetalking_cli_args(cfg)
        s = ' '.join(args)
        self.assertIn('--model ultralight', s)
        self.assertIn('--tts omnitts', s)
        self.assertIn('--TTS_SERVER http://127.0.0.1:8011', s)
        self.assertIn('--batch_size 8', s)
        # 编排字段不能泄进 CLI
        self.assertNotIn('enabled', s)
        self.assertNotIn('workdir', s)
        self.assertNotIn('env', s)

    def test_cli_whitelist_blocks_orchestrator_fields(self):
        """回归（2026-09-08 事故）: --avatar_source_id 泄入 CLI 导致 vendor argparse rc=2。
        白名单机制必须屏蔽所有编排专用字段。"""
        cfg = common_config.load_profile('fallback_8g')
        args = ' '.join(common_config.livetalking_cli_args(cfg))
        for field in ('avatar_source_id', 'avatar_live_frames', 'avatar_live_id',
                      'enabled', 'workdir', 'entry', 'config_file'):
            self.assertNotIn(f'--{field}', args, f'编排字段 {field} 泄入了 CLI')
        self.assertIn('--avatar_id my_avatar_live', args)

    def test_cli_whitelist_passes_llm_and_push_url(self):
        """公众号功能对照增量: llm_provider/llm_model/push_url 在白名单内, 配了就透传;
        编排字段照旧被拦。"""
        cfg = {'livetalking': {'llm_provider': 'dashscope', 'llm_model': 'qwen-plus',
                               'push_url': 'rtmp://x/live', 'enabled': True}}
        s = ' '.join(common_config.livetalking_cli_args(cfg))
        self.assertIn('--llm_provider dashscope', s)
        self.assertIn('--llm_model qwen-plus', s)
        self.assertIn('--push_url rtmp://x/live', s)
        self.assertNotIn('--enabled', s)

    def test_resolve_path(self):
        p = common_config.resolve_path('', 'vendor/LiveTalking')
        self.assertTrue(os.path.isabs(p))
        self.assertTrue(p.endswith('LiveTalking'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
