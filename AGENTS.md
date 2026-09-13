# digital-human-live — AGENTS.md（给本仓库开发代理的守则）

## 仓库定位
在 RTX 4060 Laptop 8GB 上交付可直播的实时数字人系统。
组合：LiveTalking（口型/推流）+ CosyVoice-300M（TTS，独立进程 HTTP 服务）+ OBS NVENC（推流）。

## 硬约束（违反=返工）
1. 显存预算：整链（口型 + TTS + OBS/NVENC）峰值 ≤ 6.6G；8G 卡桌面合成已占 0.3–0.6G。
2. 两个大模型同时常驻 CUDA = 失败模式。口型独占 GPU，TTS 走 CPU（stable 档）或推理完即释放（quality 档）。
3. 禁止把模型名、端口、batch、路径硬编码进业务代码，一律读 configs/*.yaml。
4. vendor 目录只允许「官方启动参数 + 必要 bugfix」，改动必须记录 vendor_patches.md。
5. PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True,max_split_size_mb:128 由编排器注入，不要在各脚本里重复写死。
6. 新增任何常驻 CUDA 模型前必须先跑 bench_vram。
7. 密钥走环境变量（DASHSCOPE_API_KEY 等），绝不入库。
8. Windows PowerShell 为主脚本语言；Python 做跨平台逻辑；文件编码 UTF-8。
9. 输出文档/注释用中文，代码标识符用英文。
10. 不确定时选更省显存的方案。

## 关键事实（已核实，写脚本时以此为准）
- LiveTalking 启动：`python app.py --config <yaml>`，参数解析在 vendor/LiveTalking/config.py。
  - `--model` ∈ {musetalk, wav2lip, ultralight}（ernerf 已移除）
  - `--tts` 可用插件：edgetts / gpt-sovits / xtts / tencent / doubao / azuretts / qwentts / omnitts
    （cosyvoice 插件在 base_avatar.py 中被注释掉——本项目用 omnitts 协议对接自家 TTS 网关）
  - `--transport` ∈ {rtcpush, webrtc, rtmp, virtualcam}
  - `--listenport` 默认 8010；`--batch_size` 默认 16（8G 卡上 ultralight 建议 8）
  - YAML 配置文件：key 支持 `model` 或 `model-name` 两种写法（config.py `_yaml_to_args`）
- TTS 网关协议（stable 档）：实现 LiveTalking omnitts 客户端期望的
  `POST {TTS_SERVER}/v1/audio/speech`，JSON body `{input, voice, response_format, speed, ...}`，
  流式返回 WAV（RIFF）字节块。采样率任意（网关/客户端会重采样到 16k）。
- CosyVoice 推理：`AutoModel(model_dir=...)` 按 yaml 文件名自动分派 CosyVoice/2/3。
  `inference_sft(text, spk_id, stream=True)` / `inference_zero_shot(...)` 返回生成器，
  每项 `{'tts_speech': tensor, ...}`；300M 输出 22050Hz。
  官方 FastAPI runtime 在 vendor/CosyVoice/runtime/python/fastapi/server.py（协议是 Form 上传，不是 omnitts JSON），
  本仓库的 tts_gateway 自己包了一层以对齐 omnitts 协议。
- ultralight 口型需要：
  - `models/hubert-large-ls960-ft`（HF transformers 在线加载）
  - `models/scrfd_2.5g_kps.onnx`、`models/checkpoint_epoch_335.pth.tar`（genavatar 人脸检测用）
  - `data/avatars/<avatar_id>/`：full_imgs/ face_imgs/ coords.pkl ultralight.pth
  - avatar 资产与 ultralight.pth 需从 LiveTalking 官方网盘/模型页获取（download_models.ps1 列出）
- LiveTalking API：POST /human {sessionid, type:'echo', text}、POST /interrupt_talk、
  POST /is_speaking、POST /offer（WebRTC）、GET /api/admin/sessions、/sse?sessionid=
- 虚拟摄像头：`--transport virtualcam` 需 pyvirtualcam + OBS 装了虚拟摄像头驱动 + pyaudio。
  分辨率取 opt.W/opt.H（config.py 未暴露 CLI 参数，默认 450×450 —— 待 bench 后补 patch）。
- Observed 性能基准（README）：wav2lip256@3060≈60fps；musetalk@3080Ti≈42fps。
  4060 Laptop 跑 ultralight 预期 ≥25fps，wav2lip256 为兼容档。

## 阶段纪律
用户只给「阶段 N」就只做阶段 N。每个阶段结束必须留下：可运行命令、验收记录模板、已知限制。
未拿到验收证据不得进入 N+1。缺 GPU 时脚本/配置/编排器照写，bench 支持 --skip-gpu 出 mock 报告。

## 目录速记
- apps/orchestrator/  编排器（本仓库核心，纯配置驱动）
- apps/tts_gateway/   CosyVoice HTTP 封装（omnitts 协议）
- configs/            唯一的参数来源
- scripts/            PowerShell 入口
- vendor/             上游代码，尽量不动
- docs/               验收记录、vendor_patches、bench 报告
