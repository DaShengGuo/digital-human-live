# -*- coding: utf-8 -*-
"""音频链路自检 — 用与浏览器页面完全相同的收发方式连 LiveTalking, 验证:
  1) answer SDP 里音频 m 行是否 sendonly / 是否带 msid（浏览器 evt.streams[0] 依赖它）
  2) 派发一句话后, 客户端能否真的收到音频 RTP 帧（并给出音量 RMS）

用法:
  .venv-lt\\Scripts\\python.exe -X utf8 scripts\\probe_audio_path.py [--text "..."] [--wait 60]

退出码: 0 = 收到音频; 2 = 没收到音频(链路问题); 1 = 连接/协议错误
"""
import argparse
import asyncio
import json
import sys
import time
import urllib.request

import numpy as np
from aiortc import RTCPeerConnection, RTCSessionDescription

BASE = 'http://127.0.0.1:8010'


def _post(path: str, payload: dict):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _print_sdp_media(sdp: str):
    print('--- answer SDP media 段 ---')
    for line in sdp.splitlines():
        s = line.strip()
        if s.startswith('m=') or s.startswith('a=mid:') or s.startswith('a=msid:') \
                or s.startswith('a=sendonly') or s.startswith('a=recvonly') or s.startswith('a=inactive'):
            print('   ', s)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--text', default='音频链路自检，一二三四五，六七八九十。')
    ap.add_argument('--wait', type=float, default=60.0, help='等待音频帧的最长秒数')
    ap.add_argument('--idle', action='store_true',
                    help='只连接不派发文本, 观察待机动画节流(定格)行为')
    args = ap.parse_args()

    pc = RTCPeerConnection()
    # 与 web/index.html 一致: 先 video 后 audio 的 recvonly transceiver
    pc.addTransceiver('video', direction='recvonly')
    pc.addTransceiver('audio', direction='recvonly')

    @pc.on('connectionstatechange')
    def _on_cs():
        print(f'  [pc] connectionState={pc.connectionState}')

    @pc.on('iceconnectionstatechange')
    def _on_ice():
        print(f'  [pc] iceConnectionState={pc.iceConnectionState}')

    stats = {'audio': 0, 'video': 0}
    audio_rms = []
    got = {'audio': asyncio.Event()}

    @pc.on('track')
    def on_track(track):
        print(f'on_track: {track.kind}')

        async def consume():
            while True:
                try:
                    frame = await track.recv()
                except Exception as e:
                    print(f'  [{track.kind}] 结束: {type(e).__name__} {e}')
                    return
                stats[track.kind] += 1
                if track.kind == 'audio':
                    arr = frame.to_ndarray().astype(np.float32)
                    rms = float(np.sqrt((arr ** 2).mean())) if arr.size else 0.0
                    audio_rms.append(rms)
                    if rms > 0.001:
                        got['audio'].set()
                    if stats['audio'] <= 3 or stats['audio'] % 100 == 0:
                        print(f'  [audio] 第{stats["audio"]}帧 samples={arr.shape} rms={rms:.4f}')
        asyncio.ensure_future(consume())

    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    ans = _post('/offer', {'sdp': pc.localDescription.sdp, 'type': 'offer'})
    if ans.get('code') not in (None, 0):
        print('服务端拒绝 offer:', ans)
        await pc.close()
        return 1
    sid = ans['sessionid']
    print('sessionid:', sid)
    _print_sdp_media(ans['sdp'])
    await pc.setRemoteDescription(RTCSessionDescription(ans['sdp'], ans['type']))

    t0 = time.time()
    if not args.idle:
        print(f'派发测试文本: {args.text!r}')
        print('  /human ->', _post('/human', {'sessionid': sid, 'type': 'echo', 'text': args.text}))
        deadline = t0 + args.wait
        while time.time() < deadline and not got['audio'].is_set():
            await asyncio.sleep(0.5)
    else:
        print(f'待机观察模式: 保持连接 {args.wait:g}s 不派发任何文本（看服务端日志是否出现"定格"）')
        await asyncio.sleep(args.wait)
    # 再多收 3 秒, 统计连续性
    end = time.time() + 3
    while time.time() < end:
        await asyncio.sleep(0.5)

    spoke = time.time() - t0
    # RTP 层统计: 区分"没建连"和"建连了但没数据"
    try:
        report = await pc.getStats()
        for st in report.values():
            if st.type == 'inbound-rtp':
                print(f'  [stats] inbound-rtp kind={st.kind} packetsReceived={st.packetsReceived} '
                      f'bytesReceived={st.bytesReceived}')
            if st.type == 'transport':
                print(f'  [stats] transport state={st.state}')
    except Exception as e:
        print('  [stats] 取统计失败:', e)

    print('--- 结果 ---')
    print(f'  等待 {spoke:.1f}s | video 帧={stats["video"]} | audio 帧={stats["audio"]}')
    if audio_rms:
        peak = max(audio_rms)
        nz = sum(1 for r in audio_rms if r > 0.001)
        print(f'  音频 RMS: 峰值={peak:.4f} 有声帧={nz}/{len(audio_rms)}')
    await pc.close()
    if args.idle:
        print('  结论: 待机观察模式 —— 请对照服务端日志里的「待机动画播完一遍, 定格等待新语音」')
        return 0
    if stats['audio'] == 0:
        print('  结论: ✗ 客户端一帧音频都没收到 —— 服务端 WebRTC 音频链路有问题')
        return 2
    if not got['audio'].is_set():
        print('  结论: △ 收到音频帧但全是静音(rms≈0) —— TTS→口型的音频内容为空')
        return 2
    print('  结论: ✓ 服务端确实在推送有内容的音频帧（问题只可能在浏览器播放/系统音量/伴侣采集）')
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
