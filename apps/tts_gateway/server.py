# -*- coding: utf-8 -*-
"""tts_gateway 服务 — 把 CosyVoice 包成 LiveTalking omnitts 客户端期望的 HTTP 服务。

协议（LiveTalking tts/omnitts.py 的期望, 已核实）:
  POST /v1/audio/speech
  JSON body: {input, voice, response_format, speed, ...}
  响应: 完整 WAV（Content-Length 明确, RIFF/data 长度与实际数据一致）

A1 修复要点（阶段 A, 2026-09-08）:
- WAV 封装正确: 44 字节头一次写对（nframes/bytes 与实际 payload 一致）,
  不再有静音前缀/尾部多余数据。
- 推理并发有界: INFER_SEMAPHORE（默认 1）, 模型锁只保护加载, 不排队推理。
- 请求校验: input 非空且 ≤ max_text_len（切句兜底）, speed 限 0.5~2.0。
- 就绪状态可查询: /healthz 报告 model_loaded / load_error / warmup_done。
"""
import io
import os
import sys
import json
import struct
import time
import argparse
import logging
import threading
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO)

from apps import common_config            # noqa: E402

log = logging.getLogger('tts_gateway')

MODEL = None          # 加载成功后非 None
MODEL_LOCK = threading.Lock()
LOAD_STATE = {'model_loaded': False, 'load_error': '', 'warmup_done': False}
CFG = None            # profile 的 tts 段
VOICES = {}           # voices.yaml
SR_OUT = 22050
INFER_SEMAPHORE = threading.Semaphore(1)   # 单路直播: 串行合成, 有界排队
INFERENCE_WAIT_S = 30.0                    # 排队等推理槽的上限


def _load_model(tts_cfg: dict):
    """加载 CosyVoice（线程安全, 启动期触发; 失败记录原因, 不静默）。"""
    global MODEL, SR_OUT
    with MODEL_LOCK:
        if MODEL is not None:
            return MODEL
        try:
            model_dir = common_config.resolve_path('', tts_cfg.get('model_dir', ''))
            device = tts_cfg.get('device', 'cpu')
            t0 = time.time()
            log.info('加载 CosyVoice: %s (device=%s)', model_dir, device)
            # device=cpu 时屏蔽 CUDA, 强制官方代码走 CPU 分支（官方按 cuda.is_available 判断）
            if device == 'cpu':
                os.environ['CUDA_VISIBLE_DEVICES'] = ''
            sys.path.insert(0, common_config.resolve_path('', 'vendor/CosyVoice'))
            sys.path.insert(0, common_config.resolve_path('', 'vendor/CosyVoice/third_party/Matcha-TTS'))
            from cosyvoice.cli.cosyvoice import AutoModel   # noqa: E402
            MODEL = AutoModel(model_dir=model_dir)
            SR_OUT = MODEL.sample_rate
            LOAD_STATE['model_loaded'] = True
            LOAD_STATE['load_error'] = ''
            log.info('CosyVoice 就绪 in %.1fs, sr=%s, spks=%s',
                     time.time() - t0, SR_OUT, MODEL.list_available_spks())
            return MODEL
        except Exception as e:
            LOAD_STATE['model_loaded'] = False
            LOAD_STATE['load_error'] = f'{type(e).__name__}: {e}'
            log.exception('CosyVoice 加载失败')
            raise


def _warmup(tts_cfg: dict):
    """短句预热: 真实走一遍完整合成路径, 失败体现在 LOAD_STATE。"""
    try:
        wav = synthesize_wav('测试。', 'default', 1.0)
        LOAD_STATE['warmup_done'] = len(wav) > 44 and wav[:4] == b'RIFF'
        if LOAD_STATE['warmup_done']:
            log.info('TTS 预热完成 (%d bytes wav)', len(wav))
        else:
            LOAD_STATE['load_error'] = 'warmup produced empty wav'
    except Exception as e:
        LOAD_STATE['warmup_done'] = False
        LOAD_STATE['load_error'] = LOAD_STATE['load_error'] or f'warmup: {e}'
        log.exception('TTS 预热失败')


# ── 文本处理 ────────────────────────────────────────────────────
_SENT_SPLIT = re.compile(r'(?<=[。！？!?；;])')

def clamp_text(text: str, max_len: int) -> list:
    """按配置上限切句; 返回分段列表。超限不再直接拒绝（直播需要能读长稿）,
    但段长有上限, 单段为空视为无有效内容。"""
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_len:
        return [text]
    parts, cur = [], ''
    for sent in _SENT_SPLIT.split(text):
        if not sent:
            continue
        if len(cur) + len(sent) <= max_len:
            cur += sent
        else:
            if cur:
                parts.append(cur)
            # 单句仍超长 → 按 max_len 硬切
            while len(sent) > max_len:
                parts.append(sent[:max_len])
                sent = sent[max_len:]
            cur = sent
    if cur:
        parts.append(cur)
    return parts


def pcm_to_int16_bytes(speech: np.ndarray) -> bytes:
    """float32 [-1,1] → int16 little-endian。"""
    clipped = np.clip(speech, -1.0, 1.0)
    return (clipped * 32767).astype('<i2').tobytes()


def make_wav(samples_int16: bytes, sample_rate: int, channels: int = 1) -> bytes:
    """构造字节级正确的 WAV: RIFF size = 36+data_len, data size 与实际一致, 无静音前缀。"""
    bits = 16
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    data_len = len(samples_int16)
    header = b'RIFF' + struct.pack('<I', 36 + data_len) + b'WAVE'
    header += b'fmt ' + struct.pack('<IHHIIHH', 16, 1, channels, sample_rate,
                                     byte_rate, block_align, bits)
    header += b'data' + struct.pack('<I', data_len)
    return header + samples_int16


def _resolve_voice(name: str) -> dict:
    v = VOICES.get(name) or VOICES.get('default') or {}
    return v if isinstance(v, dict) else {}


# ── 阶段C: 音频缓存（已知话术预生成/复用）─────────────────────
# 键 = 文本+音色+语速+模型目录+格式; LRU 容量有限, 防内存无限增长
import hashlib
import threading as _th

CACHE_LOCK = _th.Lock()
AUDIO_CACHE = {}          # key → wav bytes
CACHE_ORDER = []          # LRU 顺序
CACHE_MAX_BYTES = 200 * 1024 * 1024   # 200MB（约 30 分钟 22k 单声道）
CACHE_USED = 0
# 磁盘缓存: 内存未命中时回读, 落盘后**重启不丢**（本机 CPU 合成 RTF≈10, 预热一次
# 之后整场话术都是 0 延迟; 只在内存里的话一重启就得重来）。空字符串=只用内存。
CACHE_DIR = ''


def _cache_path(key: str) -> str:
    return os.path.join(CACHE_DIR, key[:2], key + '.wav')

def cache_key(text: str, voice: str, speed: float) -> str:
    model_sig = f'{CFG.get("model_dir","")}|{SR_OUT}|{CFG.get("spk_id","")}'
    raw = f'{text}|{voice}|{speed:.3f}|{model_sig}'
    return hashlib.sha256(raw.encode()).hexdigest()

def cache_get(key: str):
    global CACHE_USED
    with CACHE_LOCK:
        if key in AUDIO_CACHE:
            CACHE_ORDER.remove(key)
            CACHE_ORDER.append(key)      # 触碰移到最新
            return AUDIO_CACHE[key]
    # 内存未命中 → 回读磁盘（预热过的音频重启后仍可用）
    if CACHE_DIR:
        p = _cache_path(key)
        try:
            if os.path.isfile(p):
                with open(p, 'rb') as f:
                    wav = f.read()
                if wav:
                    cache_put(key, wav, persist=False)
                    log.info('TTS 磁盘缓存命中 %d bytes（零推理）', len(wav))
                    return wav
        except OSError:
            log.warning('读磁盘缓存失败: %s', p)
    return None

def cache_put(key: str, wav: bytes, persist: bool = True):
    global CACHE_USED
    if persist and CACHE_DIR:
        try:
            p = _cache_path(key)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = p + '.tmp'
            with open(tmp, 'wb') as f:
                f.write(wav)
            os.replace(tmp, p)           # 原子替换, 防半截文件
        except OSError:
            log.warning('写磁盘缓存失败: %s', _cache_path(key))
    with CACHE_LOCK:
        if key in AUDIO_CACHE:
            return
        AUDIO_CACHE[key] = wav
        CACHE_ORDER.append(key)
        CACHE_USED += len(wav)
        while CACHE_USED > CACHE_MAX_BYTES and CACHE_ORDER:
            old = CACHE_ORDER.pop(0)
            CACHE_USED -= len(AUDIO_CACHE.pop(old))
        if key in AUDIO_CACHE:
            log.info('TTS 缓存: %d 项, %.0fMB', len(AUDIO_CACHE), CACHE_USED / 1048576)


def resolve_ref_wav(v: dict) -> str:
    """zero_shot 参考音频路径: 相对路径按仓库根解析（voices.yaml 写 data/voices/x.wav
    即实际所在位置; 旧实现剥 basename 拼到 data/ 根, 按注释示例放文件必然找不到）。"""
    return common_config.resolve_path('', v.get('ref_wav', ''))


def _synthesize_once(text: str, voice: str, speed: float):
    """单段推理, 返回 int16 PCM bytes。调用方负责持有 INFER_SEMAPHORE。"""
    model = _load_model(CFG)
    v = _resolve_voice(voice)
    mode = CFG.get('mode', v.get('mode', 'sft'))
    stream = bool(CFG.get('stream', True))
    spk = v.get('spk_id', CFG.get('spk_id', '中文女'))
    if mode == 'zero_shot':
        ref_wav = resolve_ref_wav(v)
        ref_text = v.get('ref_text', '')
        if not os.path.exists(ref_wav):
            raise FileNotFoundError(f'zero_shot 参考音频不存在: {ref_wav}')
        import torchaudio
        prompt = torchaudio.load(ref_wav)[0]
        gen = model.inference_zero_shot(text, ref_text, prompt, stream=stream, speed=speed)
    else:
        gen = model.inference_sft(text, spk, stream=stream, speed=speed)
    out = bytearray()
    first_t = None
    t0 = time.perf_counter()
    for chunk in gen:
        if first_t is None:
            first_t = time.perf_counter() - t0
        arr = chunk['tts_speech'].numpy().squeeze(0)   # [1,T] → [T]
        out.extend(pcm_to_int16_bytes(arr))
    if first_t is not None:
        log.info('TTS 首块 %.3fs, 总 %.2fs, %d 样本',
                 first_t, time.perf_counter() - t0, len(out) // 2)
    # 注意: device=gpu 时空缓存不等于卸载, 仅作分配器归还; 直播档禁止 gpu 常驻。
    if CFG.get('device') == 'gpu':
        import torch
        torch.cuda.empty_cache()
    return bytes(out)


def synthesize_wav(text: str, voice: str, speed: float) -> bytes:
    """公开入口: 文本 → 完整 WAV bytes（多段拼接, 段间 20ms 静音）。
    有界并发: 同一时刻仅 1 路推理在跑。命中缓存则零推理直接返回。"""
    max_len = int(CFG.get('max_text_len', 200))
    segments = clamp_text(text, max_len)
    if not segments:
        raise ValueError('无有效文本')
    key = cache_key(text, voice, speed)
    hit = cache_get(key)
    if hit is not None:
        log.info('TTS 缓存命中 %d bytes（零推理）, text=%d chars', len(hit), len(text))
        return hit
    if not INFER_SEMAPHORE.acquire(timeout=INFERENCE_WAIT_S):
        raise TimeoutError(f'推理槽等待超过 {INFERENCE_WAIT_S}s（上游应限流）')
    try:
        pcm = bytearray()
        gap = b'\x00\x00' * int(SR_OUT * 0.02)   # 20ms
        for i, seg in enumerate(segments):
            if i:
                pcm.extend(gap)
            pcm.extend(_synthesize_once(seg, voice, speed))
        wav = make_wav(bytes(pcm), SR_OUT)
        cache_put(key, wav)
        return wav
    finally:
        INFER_SEMAPHORE.release()


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ('/healthz', '/'):
            self._json({'ok': LOAD_STATE['model_loaded'], **LOAD_STATE, 'sr': SR_OUT})
        else:
            self._json({'msg': 'not found'}, 404)

    def do_POST(self):
        if self.path != '/v1/audio/speech':
            return self._json({'msg': 'not found'}, 404)
        try:
            n = int(self.headers.get('Content-Length') or 0)
            if n <= 0 or n > 65536:
                return self._json({'msg': 'bad content length'}, 400)
            try:
                body = json.loads(self.rfile.read(n))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return self._json({'msg': 'invalid json'}, 400)
            if not isinstance(body, dict):
                return self._json({'msg': 'json must be object'}, 400)
            text = body.get('input')
            if not isinstance(text, str) or not text.strip():
                return self._json({'msg': 'empty input'}, 400)
            voice = body.get('voice') or 'default'
            if not isinstance(voice, str):
                return self._json({'msg': 'voice must be string'}, 400)
            try:
                speed = float(body.get('speed', 1.0))
            except (TypeError, ValueError):
                return self._json({'msg': 'speed must be number'}, 400)
            if not (0.5 <= speed <= 2.0):
                return self._json({'msg': 'speed out of range 0.5~2.0'}, 400)

            t0 = time.perf_counter()
            wav = synthesize_wav(text, voice, speed)
            self.send_response(200)
            self.send_header('Content-Type', 'audio/wav')
            self.send_header('Content-Length', str(len(wav)))
            self.end_headers()
            self.wfile.write(wav)
            self.wfile.flush()
            log.info('TTS 完成 %.2fs, %d bytes text=%d chars',
                     time.perf_counter() - t0, len(wav), len(text))
        except TimeoutError as e:
            self._json({'msg': str(e)}, 503)
        except (BrokenPipeError, ConnectionResetError):
            log.warning('客户端断开（可能被打断）')
        except Exception as e:
            log.exception('合成失败')
            try:
                self._json({'msg': str(e)}, 500)
            except Exception:
                pass

    def log_message(self, fmt, *args):
        log.debug(fmt, *args)


def main():
    global CFG, VOICES, CACHE_DIR
    ap = argparse.ArgumentParser(description='CosyVoice HTTP 网关 (omnitts 协议)')
    ap.add_argument('--profile', default=None)
    ap.add_argument('--log-level', default='INFO')
    args = ap.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format='%(asctime)s %(name)s %(levelname)s %(message)s',
                        handlers=[logging.StreamHandler(sys.stdout)])

    profile = common_config.load_profile(args.profile)
    CFG = profile.get('tts', {})
    # 磁盘缓存目录（配置 tts.cache_dir; 默认 data/tts_cache）; 传 '' 关掉落盘
    _cd = CFG.get('cache_dir', 'data/tts_cache')
    CACHE_DIR = common_config.resolve_path('', _cd) if _cd else ''
    if CACHE_DIR:
        os.makedirs(CACHE_DIR, exist_ok=True)
        log.info('TTS 磁盘缓存目录: %s', CACHE_DIR)
    host = CFG.get('listen_host', '127.0.0.1')
    port = CFG.get('listen_port', 8011)
    voices_path = os.path.join(common_config.CONFIGS_DIR, 'voices.yaml')
    if os.path.exists(voices_path):
        VOICES = (common_config.load_yaml(voices_path).get('voices') or {})

    global INFER_SEMAPHORE
    INFER_SEMAPHORE = threading.Semaphore(int(CFG.get('max_infer_concurrency', 1)))

    log.info('tts_gateway 监听 %s:%s (device=%s, model=%s)',
             host, port, CFG.get('device'), CFG.get('model_dir'))
    # 加载+预热放启动期; 失败通过 LOAD_STATE 暴露给 /healthz 与编排器, 不静默
    def _boot():
        try:
            _load_model(CFG)
            _warmup(CFG)
        except Exception:
            pass   # LOAD_STATE 已记录
    threading.Thread(target=_boot, daemon=True).start()

    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == '__main__':
    main()
