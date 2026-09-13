# -*- coding: utf-8 -*-
"""A1 回归测试 — WAV 封装正确性（无需模型）。

纪律：只检查文件头或 HTTP 200 不算通过；
这里用已知非零 PCM 封装后重新解码，逐样本核对。
"""
import io
import os
import sys
import wave
import unittest

import numpy as np

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from apps.tts_gateway.server import make_wav, pcm_to_int16_bytes, clamp_text  # noqa: E402


def _known_pcm(n=4800, sr=22050):
    """已知非零正弦波, int16 bytes。"""
    t = np.arange(n) / sr
    sig = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    return pcm_to_int16_bytes(sig), n


class TestWavRoundtrip(unittest.TestCase):
    """封装 → 解码 → 与原始 PCM 逐字节一致, 头字段与实际数据一致。"""

    def test_samples_identical(self):
        pcm, n = _known_pcm()
        wav = make_wav(pcm, 22050)
        with wave.open(io.BytesIO(wav), 'rb') as w:
            self.assertEqual(w.getframerate(), 22050)
            self.assertEqual(w.getnchannels(), 1)
            self.assertEqual(w.getsampwidth(), 2)
            self.assertEqual(w.getnframes(), n)               # 样本数一致
            decoded = w.readframes(n)
        self.assertEqual(decoded, pcm)                        # 无截断、无静音前缀、无尾部多余

    def test_header_sizes_consistent(self):
        pcm, n = _known_pcm(n=1000)
        wav = make_wav(pcm, 16000)
        self.assertEqual(len(wav), 44 + len(pcm))            # 总长 = 头 + 数据
        self.assertEqual(int.from_bytes(wav[4:8], 'little'), 36 + len(pcm))   # RIFF size
        self.assertEqual(wav[0:4], b'RIFF')
        self.assertEqual(int.from_bytes(wav[40:44], 'little'), len(pcm))      # data chunk size

    def test_no_silent_prefix(self):
        """历史 bug: _wav_header 写入 n 个静音样本再拼真数据 → 前部全是 0。"""
        pcm, n = _known_pcm()
        wav = make_wav(pcm, 22050)
        with wave.open(io.BytesIO(wav), 'rb') as w:
            first = np.frombuffer(w.readframes(160), dtype='<i2')
        self.assertGreater(int(np.max(np.abs(first))), 0)     # 前 160 样本必有非零

    def test_soundfile_can_decode(self):
        import soundfile as sf
        pcm, n = _known_pcm()
        wav = make_wav(pcm, 22050)
        data, sr = sf.read(io.BytesIO(wav), dtype='float32')
        self.assertEqual(sr, 22050)
        self.assertEqual(len(data), n)

    def test_clipping_guaranteed(self):
        """超范围样本被裁剪而非回绕（爆音防护）。"""
        hot = np.array([2.0, -3.0, 0.5], dtype=np.float32)
        b = pcm_to_int16_bytes(hot)
        arr = np.frombuffer(b, dtype='<i2')
        self.assertEqual(arr[0], 32767)
        self.assertEqual(arr[1], -32767)
        self.assertGreater(arr[2], 0)


class TestClampText(unittest.TestCase):

    def test_short_passthrough(self):
        self.assertEqual(clamp_text('你好。', 200), ['你好。'])

    def test_split_keeps_all_content(self):
        """合并不丢句: 拼接后与原文相同（顺序与内容）。"""
        text = '第一句话。' * 40
        parts = clamp_text(text, 50)
        self.assertEqual(''.join(parts), text)
        self.assertTrue(all(len(p) <= 50 for p in parts))

    def test_hard_cut_no_loss(self):
        text = '啊' * 301
        parts = clamp_text(text, 100)
        self.assertEqual(''.join(parts), text)

    def test_empty(self):
        self.assertEqual(clamp_text('   ', 200), [])


class TestResolveRefWav(unittest.TestCase):
    """zero_shot 参考音频按仓库根解析。
    回归: 旧实现剥 basename 拼到 data/ 根 → voices.yaml 注释示例写的
    data/voices/xxx.wav 按文档放文件必报 FileNotFoundError。"""

    def test_relative_voices_path(self):
        from apps.tts_gateway.server import resolve_ref_wav
        p = resolve_ref_wav({'ref_wav': 'data/voices/my_clone_ref.wav'})
        self.assertTrue(os.path.isabs(p))
        self.assertEqual(os.path.normpath(p),
                         os.path.normpath(os.path.join(_REPO, 'data', 'voices',
                                                       'my_clone_ref.wav')))

    def test_empty_returns_empty(self):
        from apps.tts_gateway.server import resolve_ref_wav
        self.assertEqual(resolve_ref_wav({}), '')


if __name__ == '__main__':
    unittest.main(verbosity=2)
