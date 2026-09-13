# 验收记录 — 冒烟测试（阶段 1）

> 本文件为模板+当前已知状态。每完成一项实测，把 [ ] 改 [x] 并填实测值。
> 通过线：进程组显存峰值 ≤6.6G；TTFF ≤1.5s；口型稳态 ≥24fps；温度桌面 ≤80℃ / 笔记本 ≤75℃。

## 环境

| 项 | 值 |
|----|-----|
| GPU | RTX 4060 Laptop 8GB（8188MiB） |
| 驱动 | 610.88 |
| 系统 | Windows 11 Home China 26200 |
| Python | 3.11.9（C:\tools\python311）+ .venv-lt |
| 仓库 | D:\AI\digital-human-live |

## A. 环境与脚本验收

- [x] `bootstrap.ps1` 创建 .venv-lt 并安装依赖
- [x] `download_models.ps1 -Profile stable -Only check` 列出全部必需模型且 [OK]/[MISS] 状态正确
- [x] `tests/` 全部通过（23 项，python 3.11）
- [ ] `start.ps1` 一键拉起 tts_gateway + LiveTalking，两端口就绪
- [ ] `healthcheck.ps1` 输出全 [OK] 且退出码 0
- [ ] `stop.ps1` 干净停止（无残留 python 进程）

## B. 链路功能验收

- [x] TTS 网关独立冒烟：`POST /v1/audio/speech` 返回合法 RIFF WAV（128044 bytes，1.45s 音频 @22050Hz），响应 4.13s（CPU 推理 RTF≈2.9，首次含预热）
- [x] CosyVoice-300M-SFT CPU 加载成功（12.3s），内置音色：中文女/中文男/粤语女/英文女/英文男/日语女/韩语女
- [ ] 浏览器打开 http://127.0.0.1:8010/dashboard.html，WebRTC 连接成功、画面出现
- [ ] `POST 127.0.0.1:8020/say {"text":"你好，欢迎来到直播间"}` → 数字人开口说话
- [ ] 回车 → 嘴动 TTFF 实测 ______ s（≤1.5s；当前 CPU TTS 首句 4.2s 未达标，见已知限制 6）
- [ ] `POST /interrupt` → 立刻闭嘴
- [ ] 连续 10 条弹幕节奏文本 → 无卡死、无丢句报错

## C. 稳定性验收（bench_vram 15 分钟）

- [ ] `bench_vram.ps1 -DurationMin 15` 报告 PASS
- [ ] 显存峰值 ______ MiB（≤6600）
- [ ] 15min 显存爬升 ______ MiB（≤300，否则判泄漏）
- [ ] avg infer fps 最小值 ______（≥24）
- [ ] 温度峰值 ______ ℃（≤75 笔记本）
- [ ] 15 分钟内无 OOM、无进程崩溃（logs/ 无 Traceback）

## D. OBS（真直播前补测）

- [ ] OBS 添加「视频捕获设备」→ 虚拟摄像头模式正常（需 `--transport virtualcam` + pyvirtualcam + OBS 虚拟摄像头驱动）
- [ ] OBS NVENC 720p25 推流 30 分钟，编排骨架显存 +OBS 总峰值 ≤6.6G
- [ ] 温度仍 ≤75℃

## 已知限制（当前阶段）

1. MuseTalk（quality 档）未 bench，default.yaml 标记 `disabled_profiles.quality_8g.need_bench_evidence`。
2. ultralight_avatar1 / wav2lip256_avatar1 形象资产需从 LiveTalking 官方网盘手动下载（CosyVoice-300M-SFT 与 hubert 已由脚本命令自动下载完成）。
3. virtualcam 分辨率默认 450×450（vendor 未暴露 W/H 参数），720p 需 WebRTC 预览或后续 patch（见 vendor_patches.md 待办 1）。
4. TTS 网关最终采用「整段合成 + Content-Length 一次性返回」（chunked 流式在本地回环会被客户端提前重置，实测多次 10054）；LiveTalking omnitts 客户端按 RIFF 头解码无影响。TTFF 因此等于整段合成时间，CPU 上首句 ≈4.2s。
5. 首次请求 TTFB 受模型冷启动影响，网关在启动期已后台预热；验收时先打 1 条预热文本再计时。
6. **TTFF 未达标**：CPU TTS 合成 11 字 ≈4.2s（RTF≈2.9），超过 1.5s 通过线。改善路径（按优先级）：
   a. 网关预热后常驻（已完成，预热句耗时 45s→后续 4.2s）；
   b. 文本先行：orchestrator 先把首句前 10 字发 TTS，边播边合剩余；
   c. bench 若证明 GPU 有余量，CosyVoice 300M 上 GPU（RTF 预期 <1）——但必须先过显存关。

## 下一阶段入口命令

```powershell
# 阶段2（bench 决策）：
powershell -ExecutionPolicy Bypass -File scripts\bench_vram.ps1 -DurationMin 15
# 报告产出 docs/bench_stable_8g_*.md → 据此回写 configs/default.yaml 决策树
```
