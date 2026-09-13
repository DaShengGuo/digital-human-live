# -*- coding: utf-8 -*-
"""RTC 收流诊断 — 独立于编排器，直连 LiveTalking /offer 收 5 秒帧。"""
import asyncio
import json
import sys
import urllib.request

from aiortc import RTCPeerConnection, RTCSessionDescription

BASE = 'http://127.0.0.1:8010'


async def main():
    pc = RTCPeerConnection()
    pc.addTransceiver('audio', direction='recvonly')
    pc.addTransceiver('video', direction='recvonly')
    counts = {'audio': 0, 'video': 0}
    kinds = []

    @pc.on('track')
    def on_track(track):
        kinds.append(track.kind)
        print('on_track:', track.kind)

        async def consume():
            while True:
                try:
                    await track.recv()
                    counts[track.kind] += 1
                except Exception as e:
                    print(f'consume {track.kind} end: {type(e).__name__} {e}')
                    break

        asyncio.ensure_future(consume())

    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    body = json.dumps({'sdp': pc.localDescription.sdp, 'type': 'offer'}).encode()
    req = urllib.request.Request(f'{BASE}/offer', data=body,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as r:
        ans = json.loads(r.read().decode())
    print('sessionid:', ans.get('sessionid'))
    await pc.setRemoteDescription(RTCSessionDescription(ans['sdp'], ans['type']))
    await asyncio.sleep(2)

    for t in pc.getTransceivers():
        print('transceiver kind=%s direction=%s current=%s' %
              (t.kind, t.direction, t.currentDirection))

    async def drain(kind):
        for t in pc.getTransceivers():
            if t.kind == kind:
                try:
                    while True:
                        f = await t.receiver.receive()
                        counts[kind] += 1
                except Exception as e:
                    print(f'drain {kind} end: {e}')

    await asyncio.sleep(5)
    print('received:', counts, 'tracks:', kinds)
    await pc.close()

asyncio.run(main())
