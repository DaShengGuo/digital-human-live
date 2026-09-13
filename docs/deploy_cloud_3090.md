# 云端部署 — 阶段 A 迁移到算家云 RTX 3090

> 目标：把本地 4060 Laptop 上跑通的阶段 A（文本→TTS→口型→WebRTC 收流）搬到云端 3090（Linux）复测。
> 形象与代码来源均为本仓库；云端只重装依赖与下载模型。

## 0. 连接参数

| 项 | 值 |
|----|-----|
| SSH | `ssh 17605205653@hb-a.suanjiayun.com -p 2020` |
| 真实 IP | `111.31.23.169`（DNS 被代理接管时用 IP 直连） |
| 网关 | `SSH-2.0-ssh2js1.15.0`（Node.js 网关，非 OpenSSH） |

> 注意：连续输错密码会触发平台 fail2ban，表现为密钥交换阶段 `Connection closed`。
> 等待 10–30 分钟自动解除；期间不要反复重试。

## 1. 传输清单

| 内容 | 体积 | 方式 |
|------|------|------|
| 代码 | 小 | `git bundle` 或 tar 排除 `.git`/模型/资产 |
| `vendor/LiveTalking/models/wav2lip.pth` | 215MB | scp |
| `vendor/LiveTalking/data/avatars/my_avatar_live/` | 391MB | scp（本地生成，无法下载） |
| `vendor/CosyVoice/pretrained_models/CosyVoice-300M-SFT/` | 5.4GB | 云端 ModelScope 下载（脚本） |
| vendor 上游仓库 | — | 可选：按记录 commit clone（见下） |

vendor 上游版本（本地 `.git` 记录，云端可选重建）：

| 目录 | 仓库 | commit |
|------|------|--------|
| `vendor/CosyVoice` | github.com/FunAudioLLM/CosyVoice | `074ca6d` |
| `vendor/LiveTalking` | github.com/lipku/LiveTalking | `c4f8c16` |
| `vendor/Ultralight-Digital-Human` | github.com/anliyuan/Ultralight-Digital-Human | `5aa8b5a` |
| `vendor/Wav2Lip-lfs` | hf-mirror.com/camenduru/Wav2Lip | `bfcd926` |
| `vendor/Wav2Lip-ref` | github.com/Rudrabha/Wav2Lip | `bac9a81` |

## 2. 部署步骤（云端）

```bash
# 2.1 解包代码到 ~/digital-human-live
cd ~/digital-human-live

# 2.2 环境准备（venv + torch cu124 + 依赖；约 10-20 分钟）
bash scripts/linux/bootstrap.sh

# 2.3 模型与资产检查（缺 CosyVoice 会自动下载；wav2lip.pth / avatar 需先传）
bash scripts/linux/download_models.sh --profile stage_a

# 2.4 启动（云端档位，后台）
bash scripts/linux/start.sh --profile stage_a_3090 --daemon
tail -f logs/orchestrator.out.log     # 等到 phase=live

# 2.5 健康检查
bash scripts/linux/healthcheck.sh --profile stage_a_3090

# 2.6 阶段 A 验收（真实文本→口型→录制→证据）
.venv-lt/bin/python -m apps.acceptance_stage_a

# 2.7 停止
bash scripts/linux/stop.sh
```

## 3. 本次为云端做的改动（均已回归测试，48 项单测通过）

| 文件 | 改动 | 原因 |
|------|------|------|
| `apps/proc.py` | 3 处平台分支 | `CREATE_NEW_PROCESS_GROUP` / `CTRL_BREAK_EVENT` / `taskkill` 是 Windows 专属；Linux 改用 `start_new_session` + `killpg(SIGTERM→SIGKILL)` |
| `apps/session_link.py` | `_fail` 提升为类方法 | 原实现是 `_run` 内的局部函数，但调用处写 `self._fail`，异常时抛 `AttributeError` 掩盖真实错误（如 `/offer` 失败） |
| `apps/acceptance_stage_a.py` | ffprobe 走 `shutil.which` | 原来硬编码 `C:\tools\ffmpeg\bin\ffprobe.exe` |
| `apps/offline_lip_video.py` | ffmpeg 走 `shutil.which` | 同上 |
| `configs/profile_stage_a_3090.yaml` | 新增 | Linux venv 路径 `bin/python`；3090 放宽显存/温度阈值 |
| `configs/default.yaml` | `available_profiles` 增加档位 | 让新档位可被 preflight 接受 |
| `scripts/linux/*.sh` | 新增 | PowerShell 的 Linux 对应实现 |

## 4. 已知风险

1. **WebRTC 与云端网络**：编排器在云端自连 `127.0.0.1:8010` 建立会话，不依赖外网；但如需从本地浏览器看预览，要额外做端口转发（`ssh -L 8010:127.0.0.1:8010`）。
2. **首启耗时**：云端磁盘慢 + CosyVoice 冷加载，`ready_timeout_s` 已放宽到 300s。
3. **TTS 仍跑 CPU**：阶段 A 只证功能，与本地档位保持一致。3090 有 24G 余量，后续可把 `tts.device` 改 `cuda` 提升 TTFF（需先过显存关）。
4. **`max_session: 2`**：LiveTalking 会话槽位有限，编排器异常退出会残留会话；重跑前先 `stop.sh` 或重启 LiveTalking。
