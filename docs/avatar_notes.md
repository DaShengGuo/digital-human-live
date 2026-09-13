# Avatar 生成记录 — 用户真人视频 (my_avatar)

## 素材
- 源文件: `C:\Users\郭宝盛\OneDrive\桌面\8dc4d4d353b9e1da7f3be14bf9c2fc50.mp4`
- 规格: 2:28.83, 720x1280 竖屏, h264, 实际帧率 11.32fps（tbr 30）
- 音轨: 有（未使用，口型由 TTS 音频驱动）

## 处理流程
1. **补帧重定时**: 11.32fps → 25fps（LiveTalking fps 硬性要求 25）
   `ffmpeg -i 源.mp4 -vf fps=25 data/raw/source_25fps.mp4`
2. **抽帧+人脸检测+裁剪**: vendor/LiveTalking `avatars/wav2lip/genavatar.py`
   `generate_avatar(source_25fps.mp4, 'my_avatar', img_size=256, face_det_batch_size=8)`
   - 3721 帧 full_imgs / face_imgs（256×256）/ coords.pkl
3. **权重**: `D:\AI\wav2lip\wav2lip256.pth`（214MB，torch.load 验证合法，state_dict 380 参数）
   → 拷贝为 `vendor/LiveTalking/models/wav2lip.pth`（wav2lip256 官方命名要求）

## 关键决策记录
- **分辨率 96→256**: 首次用 img_size=96 生成后发现 wav2lip256.pth 要求 256 输入，
  官方 wav2lip256_avatar1 的 face_imgs 也是 256×256 → 重生成 256 版本。
- **口型模型选型**: ultralight.pth 需从 LiveTalking 官方网盘手动下载（无直链），
  而 wav2lip256.pth 已就位且用户素材适配 → 先走 **fallback_8g 档**（wav2lip256）
  跑通整链，ultralight 权重拿到后再切回 stable 档。
- 96 版资产已被 256 版覆盖（同名 my_avatar）。

## 备用资产
- `D:\AI\wav2lip\wav2lip256_avatar1\`（官方示例形象，550 帧）未部署，可作 fallback 的 fallback。

## 生成命令复现
```powershell
# 补帧
ffmpeg -i <源视频> -vf fps=25 D:\AI\digital-human-live\data\raw\source_25fps.mp4
# 生成 (在 vendor/LiveTalking 目录, repo venv)
cd D:\AI\digital-human-live\vendor\LiveTalking
..\..\.venv-lt\Scripts\python.exe -c "import sys; sys.path.insert(0,'.'); from avatars.wav2lip.genavatar import generate_avatar; generate_avatar(r'D:\AI\digital-human-live\data\raw\source_25fps.mp4','my_avatar',img_size=256,face_det_batch_size=8)"
```
