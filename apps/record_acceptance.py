# -*- coding: utf-8 -*-
"""阶段C: 本地录制验收 — 用 ffmpeg (NVENC) 抓取浏览器预览画面不可行时,
退而求其次: 录制 LiveTalking 的 WebRTC 流需要浏览器; 这里提供的是
「验收录制」入口 — 调用系统默认浏览器打开预览 + ffmpeg gdigrab 屏幕区域录制。

更可靠的路径（推荐）: 用户手动打开预览页面后运行本脚本, 它只负责录屏+录音。
真实直播时 OBS 才是主输出; 本脚本用于无 OBS 环境的阶段C/D 本地验收证据。
"""
import os
import sys
import time
import argparse
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FF = r'C:\tools\ffmpeg\bin\ffmpeg.exe'


def main():
    ap = argparse.ArgumentParser(description='本地录制验收（gdigrab + NVENC）')
    ap.add_argument('--duration', type=float, default=30, help='录制秒数')
    ap.add_argument('--out', default=os.path.join(REPO, 'data', 'out', 'webrtc_acceptance.mp4'))
    ap.add_argument('--fps', type=int, default=25)
    ap.add_argument('--no-audio', action='store_true')
    args = ap.parse_args()

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    cmd = [FF, '-y', '-f', 'gdigrab', '-framerate', str(args.fps),
           '-i', 'desktop', '-c:v', 'h264_nvenc', '-preset', 'p4', '-b:v', '4M']
    if not args.no_audio:
        cmd += ['-f', 'dshow', '-i', 'audio=virtual-audio-capturer']
    cmd += ['-t', str(args.duration), args.out]
    print('录制 {}s → {}'.format(args.duration, args.out))
    print('提示: 请先在浏览器打开 http://127.0.0.1:8010/index.html 并点击开始连接')
    try:
        subprocess.run(cmd, check=True, timeout=args.duration + 60)
        print('完成:', args.out, os.path.getsize(args.out), 'bytes')
    except FileNotFoundError:
        print('错误: 虚拟声卡(dshow virtual-audio-capturer)未安装或 ffmpeg 缺失')
        print('改用无音频录制: --no-audio')
        sys.exit(2)
    except subprocess.CalledProcessError:
        print('录制失败（常见原因: 无虚拟声卡 / 用户取消窗口选择）')
        sys.exit(2)


if __name__ == '__main__':
    main()
