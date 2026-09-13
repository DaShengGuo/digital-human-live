# vendor_patches.md — 对 vendor 代码的全部改动记录

## 原则
vendor 只打「官方支持的启动参数 + 必要 bugfix」。任何改动必须在此登记：文件、原因、内容、可回滚方式。

---

## 当前状态：4 处改动

### Patch 1: `avatars/ultralight/face_detect_utils/get_landmark.py`
- **日期**: 2026-09-07
- **原因**: 该文件顶部 `from os import wait3` 在 Windows 上必然 ImportError（`os.wait3` 是 POSIX 专用），
  导致 ultralight/wav2lip 的 genavatar 全流程无法启动。
- **改动**: 删除该 import（全文件无任何使用点）。
- **回滚**: `git checkout -- avatars/ultralight/face_detect_utils/get_landmark.py`
- **上游状态**: 已知 Windows 兼容 bug，建议向上游提 PR。

### Patch 2: `tts/omnitts.py` — WAV 响应数据边界
- **日期**: 2026-09-08（阶段 A / A1）
- **原因**: 客户端 `stream_tts` 假设「首个网络块包含完整 WAV」即用 soundfile 解码；
  TCP 分块到达时首块可能截断，导致解码失败或丢音频。本项目网关返回完整 WAV+Content-Length，
  接收端必须按长度收全再解码。
- **改动**: `_synthesize` 中当 `response_format=='wav'` 且响应带 `Content-Length` 时，
  `res.content` 一次收全后作为单块 yield；纯 PCM 流式响应保持原逐块逻辑。
- **回滚**: `git checkout -- tts/omnitts.py`
- **必要性**: 没有它，音频正确性取决于网络分块时机，属于隐性竞态。

### Patch 3: `avatars/base_avatar.py` — 待机动作（用户需求，非 bugfix；v2 2026-09-11 晚）
- **日期**: 2026-09-11（v1 下午 / v2 当晚）
- **原因**:
  - v1 起因: 无声时上游把 `frame_list_cycle`（375 帧＝15s）无限乒乓循环，看起来
    "一直在动嘴却没声音" → v1 做成"播一遍 → 定格成一张静止照片 → 空闲 5 分钟后再播"。
  - v2 起因（用户反馈）: **不要一张照片**，待机时人要继续动（身体/手/头/眨眼）。
- **实测约束**: 源视频 `data/raw/source_25fps.mp4`（3721 帧）**全片都在讲话**，
  最长"低嘴动"片段只有 0.2 秒 —— 没有现成的"不说话的镜头"可用。
- **v2 改动**:
  1. `_resolve_idle_window()`: 读 `LT_IDLE_WINDOW="起,止"`（帧号半开区间）；
     留空 → `_pick_idle_window()` 自动挑；`off` → 保持上游整段循环。
  2. `_pick_idle_window()`: 对 `frame_list_cycle` 下半脸做灰度降采样帧间差，
     取**均值最小**的连续 2 秒窗口（采样上限 220 帧，启动一次约 1~2s）。
     该窗口内人仍在眨眼/点头/身体微动，但不是"在讲话"的样子。
     本项目素材自动挑出的是 **帧 276~326（11.0~13.0s）**，人工看过：嘴闭合、有眨眼、头部微动。
  3. `inference()` 全静音分支：在该窗口内**乒乓循环**（`idle_pos` 独立计数，
     不动说话用的 `index`）；静音→说话时重置 `idle_pos`，说话帧立即接管。
  4. 删掉 v1 的定格逻辑（`LT_IDLE_HOLD_S` / `_pick_idle_hold_index`）。
- **开关**: `configs/profile_*.yaml` → `livetalking.env.LT_IDLE_WINDOW`（默认 `''`＝自动挑）。
- **回滚**: `git checkout -- avatars/base_avatar.py`
- **代价**: 无额外显存；仅启动时一次帧分析。

### Patch 4: `web/index.html` — 音频自检面板（本次"没声音"排查的产物）
- **日期**: 2026-09-11
- **原因**: 排查"数字人没有声音"时，浏览器控制台粘贴诊断命令被 Chrome 的
  "allow pasting" 拦截，无法取证。把诊断做进页面，任何人打开页面即可自证。
- **改动**:
  1. `<audio>` 下方新增 `#audioBadge` 状态条与 `#btnResumeAudio` 按钮。
  2. `pc.addEventListener('track')`：改为 `evt.streams[0] || new MediaStream([evt.track])` 兜底
     （服务端 addTrack 未带 stream 时 `evt.streams` 可能为空 → 原写法会赋 undefined 静默失声），
     并对音频轨道调用 `attachAudioMeter()` + `tryPlayAudio()`。
  3. 新增 `attachAudioMeter()`：用 WebAudio AnalyserNode 实时显示电平、有声数据块计数、峰值、
     `audio.paused`/`muted`/`volume`，并给出"数据为 0 = 服务端没推" 与
     "有数据但听不到 = 标签页/系统音量" 的判别提示；`tryPlayAudio()` 捕获 NotAllowedError
     并显示"点此启用声音"按钮；`stop()` 清理计时器与状态。
- **回滚**: `git checkout -- web/index.html`
- **补充证据**: 同一改动配套 `scripts/probe_audio_path.py`（服务端侧证据：客户端实收音频帧数与 RMS）。

### Patch 5: `web/index.html` — 可见静音/音量控件同步到真实音频（"关闭声音没有作用"修复）
- **日期**: 2026-09-11
- **原因**: 页面唯一可见的音量/静音控件是 `<video controls>` 的原生按钮，但 video 元素
  只接**画面**轨道 —— 真正出声的是隐藏的 `<audio id="audio">`。所以点视频播放条上的
  「关闭声音」对声音毫无作用（静音了却还响）。用户报告: "关闭声音没有作用。还是有作用"。
- **改动**: 新增 `_syncAudioControls()`（脚本尾部立即执行）：
  1. `video.volumechange → audio`：同步 `muted` 与 `volume`，用户点开声音时顺手补一次
     `tryPlayAudio()`（此时有用户手势，自动播放限制不拦）。
  2. `audio.volumechange → video`：反向同步，两元素状态永不打架。
  3. 音频自检面板提示语更新：告知"关声音 = 点视频播放条上的🔇"。
- **回滚**: `git checkout -- web/index.html`
- **注意**: 待机窗口循环（patch 3 帧区间）**设计上就是无声的** —— 它是"没在讲话"的
  状态（眨眼/点头/微动），不是循环播放有音频的素材；说话时声音随 TTS 自动出现。

## 已核实的官方行为（不改动，但编排器依赖这些事实）

| 事实 | 位置 | 对编排器的意义 |
|------|------|----------------|
| CLI+YAML 双参数入口，优先级 CLI > YAML | `LiveTalking/config.py:39-118` | 我们全走 CLI 参数，不下发 vendor yaml |
| `--model` ∈ {musetalk, wav2lip, ultralight} | `config.py:61` | profile 的 model 字段只能取这三个 |
| cosyvoice TTS 插件被官方注释掉 | `avatars/base_avatar.py:93` | 改用 `--tts omnitts` 协议对接自建网关 |
| omnitts 客户端 POST `{TTS_SERVER}/v1/audio/speech`，JSON body，流式 WAV | `tts/omnitts.py:96-154` | tts_gateway 必须实现该协议 |
| FPS 日志格式 `------actual avg infer fps:{x}`（无 finalfps） | `avatars/base_avatar.py:371` | watchdog/bench 的 FPS 解析已兼容 |
| FPS 官方输出在 INFO 级文件日志 `livetalking.log`（进程工作目录） | `utils/logger.py` | bench 从 `logs/livetalking.log` 读，需确认工作目录映射 |
| ultralight 音频特征模型固定路径 `./models/hubert-large-ls960-ft` | `avatars/ultralight/audio2feature.py:10` | 下载脚本按此路径放置 |
| avatar 资产固定相对路径 `./data/avatars/<avatar_id>` | `avatars/ultralight_avatar.py:73` | avatar 必须放 vendor 内 data/avatars |
| CosyVoice 设备判断硬编码 `cuda if available` | `cosyvoice/cli/model.py:36` | tts_gateway CPU 模式通过 `CUDA_VISIBLE_DEVICES=''` 屏蔽 |
| CosyVoice 300M 输出采样率 22050 | `cosyvoice/hifigan/generator.py:388` | 网关重采样至 16k 交口型 |

## 待办 / 观察项（bench 后决定是否打 patch）

1. **virtualcam 分辨率**：`streamout/virtualcam.py:21-22` 读 `opt.W/opt.H`，但 `config.py` 未暴露 W/H 参数，默认 450×450。
   若实测预览 720p 需求成立，patch 方案：config.py 增补 `--W/--H` 两个 argparse 参数（官方风格兼容）。
   在此之前先用 WebRTC 预览（`--transport webrtc`），分辨率由浏览器窗口决定，无需改代码。
2. **FPS 日志落盘位置**：LiveTalking 的 logger 写进程 cwd 下的 `livetalking.log`。
   编排器以 vendor/LiveTalking 为 cwd 启动，bench 需要 `logs/livetalking.log` →
   现方案：orchestrator 启动后把 `vendor/LiveTalking/livetalking.log` 复制/映射到 `logs/`（优先用符号链接不可行则 copy tail）。
   若 bench 确认路径不符再登记为正式 patch。
