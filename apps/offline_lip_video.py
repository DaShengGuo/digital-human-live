# -*- coding: utf-8 -*-
"""离线口型视频生成 — my_avatar + TTS 音频 → MP4 成品预览。

复用 vendor/LiveTalking 的 wav2lip 推理（同一份权重与 avatar 资产），
mel 特征走 vendor avatars/wav2lip/audio.melspectrogram（与在线 MelASR 完全同口径）。
仅用于快速出成品；在线直播链路仍是 orchestrator + tts_gateway + LiveTalking。
"""
import os
import sys
import pickle
import shutil
import subprocess
import types

import numpy as np
import torch
import cv2

REPO = r'D:\AI\digital-human-live'
LT = os.path.join(REPO, 'vendor', 'LiveTalking')
os.chdir(LT)
sys.path.insert(0, LT)

from avatars.wav2lip.models import Wav2Lip           # noqa: E402
from avatars.wav2lip import audio as vaudio          # noqa: E402
from utils.image import read_imgs, mirror_index      # noqa: E402

FPS = 25
BATCH = 8
WAV16K = os.path.join(REPO, 'logs', 'lipgen_tts_16k.wav')
OUT_DIR = os.path.join(REPO, 'data', 'out')
OUT = os.path.join(OUT_DIR, 'li_teacher_hello.mp4')
AVATAR = 'my_avatar'

device = 'cuda' if torch.cuda.is_available() else 'cpu'
print('device:', device)

# ── 1. 口型模型 ─────────────────────────────────────────────────
model = Wav2Lip()
ckpt = torch.load(os.path.join(LT, 'models', 'wav2lip.pth'), map_location='cpu')
sd = ckpt['state_dict']
model.load_state_dict({k.replace('module.', ''): v for k, v in sd.items()})
model = model.to(device).eval()
print('wav2lip loaded')

# ── 2. avatar 资产 ──────────────────────────────────────────────
# 只加载前 MAX_AVATAR_FRAMES 帧（口型循环取用，几百帧足够；
# 之前全量读 3721 张 720x1280 PNG 耗时数分钟）
MAX_AVATAR_FRAMES = 400
apath = os.path.join(LT, 'data', 'avatars', AVATAR)
with open(os.path.join(apath, 'coords.pkl'), 'rb') as f:
    coords = pickle.load(f)
def _load_prefix(dirpath, n):
    files = sorted(os.listdir(dirpath))[:n]
    return [cv2.imread(os.path.join(dirpath, x)) for x in files]
full_imgs = _load_prefix(os.path.join(apath, 'full_imgs'), MAX_AVATAR_FRAMES)
face_imgs = _load_prefix(os.path.join(apath, 'face_imgs'), MAX_AVATAR_FRAMES)
n_avatar = len(face_imgs)
print('avatar frames:', n_avatar)

# ── 3. 音频 → mel → 逐帧 mel chunk（与在线 MelASR 同口径）──────
speech, _ = __import__('librosa').load(WAV16K, sr=16000)
# vendor MelASR: 16k 单声道, l/r stride 各 10 帧, chunk=320 样本 (20ms)
chunk = 320
stride = 10
# 头尾补静音, 模拟在线流式上下文
sil = np.zeros(chunk * stride, dtype=np.float32)
speech_padded = np.concatenate([sil, speech.astype(np.float32), sil])
frames = [speech_padded[i * chunk:(i + 1) * chunk]
          for i in range(len(speech_padded) // chunk)]
mel = vaudio.melspectrogram(np.concatenate(frames))
mel_step_size = 16
mel_idx_multiplier = 80. / FPS
mel_chunks = []
i = 0
n_out = (len(frames) - 2 * stride) // 2   # 每 2 个 20ms chunk 出 1 帧
while i < n_out:
    start = int(stride * 80 / 50 + i * mel_idx_multiplier)
    if start + mel_step_size > len(mel[0]):
        mel_chunks.append(mel[:, len(mel[0]) - mel_step_size:])
    else:
        mel_chunks.append(mel[:, start: start + mel_step_size])
    i += 1
print('mel chunks:', len(mel_chunks))

# ── 4. 批量推理 + 贴回 ──────────────────────────────────────────
os.makedirs(OUT_DIR, exist_ok=True)
tmp_frames = os.path.join(OUT_DIR, '_tmp_frames')
os.makedirs(tmp_frames, exist_ok=True)
for f in os.listdir(tmp_frames):
    os.remove(os.path.join(tmp_frames, f))

res_q = []
idx = 0
n_write = 0
for mc_i in range(0, len(mel_chunks), BATCH):
    batch_mel = mel_chunks[mc_i: mc_i + BATCH]
    if len(batch_mel) < BATCH:
        batch_mel = batch_mel + [mel_chunks[-1]] * (BATCH - len(batch_mel))
    img_batch = []
    for j in range(BATCH):
        aidx = mirror_index(n_avatar, idx + j)
        img_batch.append(face_imgs[aidx])
    img_batch = np.asarray(img_batch)
    img_masked = img_batch.copy()
    img_masked[:, img_batch.shape[1] // 2:] = 0
    inp = np.concatenate((img_masked, img_batch), axis=3) / 255.
    mel_b = np.reshape(np.asarray(batch_mel), [BATCH, batch_mel[0].shape[0], batch_mel[0].shape[1], 1])
    inp_t = torch.FloatTensor(np.transpose(inp, (0, 3, 1, 2))).to(device)
    mel_t = torch.FloatTensor(np.transpose(mel_b, (0, 3, 1, 2))).to(device)
    with torch.no_grad():
        pred = model(mel_t, inp_t)
    pred = pred.cpu().numpy().transpose(0, 2, 3, 1) * 255.
    for j in range(len(mel_chunks) - mc_i if mc_i + BATCH > len(mel_chunks) else BATCH):
        aidx = mirror_index(n_avatar, idx + j)
        y1, y2, x1, x2 = coords[aidx]
        frame = full_imgs[aidx].copy()
        rf = cv2.resize(pred[j].astype(np.uint8), (x2 - x1, y2 - y1))
        frame[y1:y2, x1:x2] = rf
        cv2.imwrite(os.path.join(tmp_frames, f'{n_write:08d}.png'), frame)
        n_write += 1
    idx += BATCH
    if mc_i % 80 == 0:
        print(f'render {mc_i}/{len(mel_chunks)}')
print('frames rendered:', n_write)

# ── 5. ffmpeg 合成 (视频 + 原始 22k 音频) ───────────────────────
FF = shutil.which('ffmpeg') or r'C:\tools\ffmpeg\bin\ffmpeg.exe'
subprocess.run([FF, '-y', '-framerate', str(FPS), '-i', os.path.join(tmp_frames, '%08d.png'),
                '-i', os.path.join(REPO, 'logs', 'lipgen_tts.wav'),
                '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-r', str(FPS),
                '-c:a', 'aac', '-shortest', OUT], check=True, capture_output=True)
print('OUTPUT:', OUT, os.path.getsize(OUT), 'bytes')
