# digital-human-live

RTX 4060 Laptop 8GB 上可直播的实时数字人系统：
**LiveTalking**（口型驱动 + WebRTC/虚拟摄像头输出）+ **CosyVoice-300M**（流式 TTS，独立进程）+ **OBS NVENC**（推流）。

配置驱动、一键启动、显存超限自动降级。所有参数在 `configs/`，业务代码零硬编码。

## 档位

| profile | 口型 | TTS | 预期显存 | 状态 |
|---------|------|-----|---------|------|
| `stable_8g`（默认） | Ultralight | CosyVoice-300M CPU | ≤4.5G + OBS | 待 bench |
| `quality_8g` | MuseTalk batch=2 | CosyVoice CPU | ≤6.6G | bench 通过前禁用 |
| `fallback_8g` | Wav2Lip256 | CosyVoice-300M CPU | 更低 | 兼容档 |

## 快速开始

```powershell
# 0) 一次性环境准备（venv + 依赖 + 模型下载清单检查）
powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1

# 1) 下载模型（需要先看清楚每个模型的用途，体积大）
powershell -ExecutionPolicy Bypass -File scripts\download_models.ps1 -Profile stable

# 2) 启动（默认 stable 档）
powershell -ExecutionPolicy Bypass -File scripts\start.ps1

# 3) 验收 / 健康 / 停止
powershell -ExecutionPolicy Bypass -File scripts\healthcheck.ps1
powershell -ExecutionPolicy Bypass -File scripts\stop.ps1

# 4) 显存基准（15 分钟采样，产出 docs/bench_*.md）
powershell -ExecutionPolicy Bypass -File scripts\bench_vram.ps1 -DurationMin 15
```

启动后浏览器打开 `http://127.0.0.1:8010/dashboard.html`（WebRTC 预览），
或在 OBS 添加「视频捕获设备」选择虚拟摄像头（`--transport virtualcam` 模式）。

## 架构

```
文本队列(orchestrator:8020) ──► TTS 网关(8011, CosyVoice CPU) ──► LiveTalking(8010)
       │                                   │ omnitts 协议              │
       └── 弹幕/话术节奏限流                └── /v1/audio/speech        └── WebRTC / 虚拟摄像头 → OBS NVENC
                                    watchdog: nvidia-smi 采样 → OOM/FPS↓/显存爬升 → 自动降级
```

- 编排器不碰推理核，只负责：拉起进程、文本队列、看门狗、降级重启。
- TTS 与口型永不同时占 CUDA：stable 档 TTS 强制 CPU；quality 档 TTS 推理完释放。
- vendor 代码只打「官方参数 + 必要 bugfix」，见 `docs/vendor_patches.md`。

## 功能对照（对照公众号《LiveTalking 实时交互式流式数字人引擎》功能清单）

| 文章功能 | 本项目实现 | 入口 |
|---|---|---|
| 实时音视频同步对话 | WebRTC 预览页 + 虚拟摄像头 → OBS NVENC；rtmp/rtcpush 在 profile 注释示例中（`transport`/`push_url`） | `http://127.0.0.1:8010/index.html` |
| 多模型切换 | ultralight（stable）/ wav2lip256（fallback）/ musetalk（quality, bench 通过前禁用） | `configs/profile_*.yaml` |
| 文本驱动 | 编排器文本队列（合并/限流/过期/打断取消链）→ vendor `/human` echo | `POST :8020/say` |
| LLM 智能问答 | vendor `type=chat`（OpenAI 兼容 dashscope/orcarouter），编排器透传 | `POST :8020/chat`（开启方法见 operations.md 六·八） |
| 语音驱动 | vendor `/humanaudio` 上传 + dashboard「按住说话」（浏览器语音识别 → chat） | `http://127.0.0.1:8010/dashboard.html` |
| 声音克隆 | CosyVoice zero_shot（参考音频 3~10s） | operations.md 六·九 |
| 实时打断 | 编排器 `/interrupt`（队列取消 + vendor flush_talk，epoch 防迟到派发） | `POST :8020/interrupt` |
| 动作编排/自定义视频 | vendor `/set_audiotype`（index.html 卡片）+ 待机自然微动 patch | `index.html` |
| 录制 | vendor `/record` 开始/停止 + mp4 下载（index.html 录制卡） | `index.html` |
| 短视频批量制作 | 离线口型渲染（wav2lip 权重复用，TTS 音频 → MP4） | `apps/offline_lip_video.py` |
| 24h 无人值守直播 | 话术导演循环 + 定时开播调度 + 人工接管/结束 | `http://127.0.0.1:8030` 控制台 |
| 弹幕问答 + 知识库 | 弹幕管线（去重/频控/相似合并/时效）+ FAQ 知识库（不编造）+ OCR 自动读屏接入 | 控制台「实时互动」 |
| 浏览器接入 | index.html（主控）/ dashboard.html（交互演示） | `:8010` |

已知限制（8G 单机、单直播间定位）：
- 多并发：vendor 支持多路会话，本编排器按单直播间设计只绑定一路生产会话（`max_session: 2` 留备用）；
- 多语言：CosyVoice-300M-SFT 以中文为主；需多语言/更强克隆时把 `tts.model_dir` 换成 CosyVoice2/3 权重（配置驱动，代码零改动）；
- quality 档（MuseTalk）未通过 bench 前保持禁用。

## 目录

```
configs/          YAML profiles + OBS 配置样例（唯一参数来源）
scripts/          PowerShell 入口（bootstrap/start/stop/healthcheck/bench/download）
apps/orchestrator/  启停、文本队列、看门狗、降级
apps/tts_gateway/   CosyVoice HTTP 封装（omnitts 协议）
vendor/           LiveTalking、CosyVoice 上游（submodule 方式管理）
data/avatars|voices|scripts/
logs/
tests/
docs/             验收记录、bench 报告、vendor patches
```

## 验收标准（通过线）

- 进程组显存峰值 ≤ 6.6G（含 OBS 时）；无 15min 显存爬升
- 口型稳态 ≥ 24fps；TTFF（回车→嘴动）≤ 1.5s
- 桌面 GPU ≤ 80℃，笔记本 ≤ 75℃ 且不掉频
- 连续直播 ≥ 15 分钟不 OOM 不崩溃

详细验收记录见 `docs/acceptance*.md`。
