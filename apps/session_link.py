# -*- coding: utf-8 -*-
"""live session link — 编排器用 aiortc 建立真实 WebRTC 会话并持有 sessionid。

A2 修复: 不再向 sessionid='0' 盲发文本。流程:
  1. aiortc RTCPeerConnection + recvonly audio/video
  2. POST /offer (SDP) → 校验 HTTP 200 且响应含 sessionid（vendor 错误时返回 code=-1）
  3. setRemoteDescription(answer) → ICE connected
  4. 绑定 sessionid；连接失败/关闭时置 invalid，由编排器重绑
后台 drain 线程持续 recv()，避免 aiortc 远端轨道队列积压反压渲染管线。
"""
import asyncio
import json
import logging
import threading
import time
import urllib.request

log = logging.getLogger('session_link')


class SessionLinkError(RuntimeError):
    pass


class PageSessionLink:
    """外部页面会话绑定（生产模式, 阶段H后新增）:

    不自己建 WebRTC 会话, 而是轮询 LiveTalking /api/admin/sessions,
    绑定浏览器页面(如 OBS/伴侣捕获的 index.html)建立的会话。
    这样导演/知识库/手动插播的语音口型全部出现在你看到的页面里。
    与 LiveSessionLink 接口同形（connect/status/invalidate/close）。
    """

    def __init__(self, base_url: str, avatar_id: str = None):
        self.base_url = base_url.rstrip('/')
        self.avatar_id = avatar_id
        self.sessionid = None
        self.connected = False
        self.state = 'idle'
        self.error = ''
        self.frames_received = None      # 外部会话无法计数, 如实 None
        self.audio_frames_received = None

    def _query_sessions(self):
        try:
            with urllib.request.urlopen(f'{self.base_url}/api/admin/sessions',
                                        timeout=5) as r:
                d = json.loads(r.read().decode())
        except Exception as e:
            raise SessionLinkError(f'查询会话失败: {e}')
        if not isinstance(d, dict) or d.get('code') != 0:
            raise SessionLinkError(f'业务失败: {d}')
        return [s.get('sessionid') for s in (d.get('data', {}).get('sessions') or [])
                if s.get('sessionid')]

    def connect(self, timeout_s: float = 60.0) -> str:
        import time as _t
        deadline = _t.time() + timeout_s
        last = []
        while _t.time() < deadline:
            last = self._query_sessions()
            if last:
                self.sessionid = str(last[-1])   # 取最新会话（页面刚建立）
                self.connected = True
                self.state = 'connected'
                self.error = ''
                return self.sessionid
            self.state = 'connecting'
            _t.sleep(2)
        raise SessionLinkError(
            f'{timeout_s}s 内未发现外部会话, 请先在浏览器页面点「开始连接」'
            f'（当前 sessions={last}）')

    def refresh(self) -> bool:
        """外部页面刷新/重连后 sessionid 会变, 每轮派发前校准。

        绑定粘性（2026-09-13 修复"循环话术没声音"元凶之一）:
        当前会话还活着就绝不换——旧实现无条件改绑"最新创建的会话", 只要
        多开任何一个标签页(dashboard/第二个 index.html), 绑定几秒内被抢走,
        声音去了没人监听的页面; 关掉多余标签又"自己好了", 极难排查。
        只有当前会话消失(页面刷新/断开)才收养最新会话。
        """
        try:
            sids = self._query_sessions()
        except SessionLinkError:
            return self.connected
        if not sids:
            if self.connected:
                self.connected = False
                self.state = 'failed'
                self.error = '外部会话消失'
            return self.connected
        if self.sessionid and self.sessionid in sids:
            self.connected = True
            self.state = 'connected'
            return True
        old = self.sessionid
        self.sessionid = str(sids[-1])   # 原会话已消失(页面刷新/断开) → 收养最新
        self.connected = True
        self.state = 'connected'
        self.error = ''
        log.info('外部会话重绑: %s → %s（原会话消失; 活跃会话 %d 个, 多个时请关掉不用的标签页）',
                 old, self.sessionid, len(sids))
        return True

    def invalidate(self, reason: str):
        self.connected = False
        self.state = 'failed'
        self.error = reason
        self.sessionid = None

    def close(self):
        self.connected = False
        self.state = 'closed'

    def status(self) -> dict:
        return {'state': self.state, 'sessionid': self.sessionid,
                'connected': self.connected, 'error': self.error,
                'frames_received': self.frames_received,
                'audio_frames_received': self.audio_frames_received,
                'mode': 'page_external'}


class LiveSessionLink:
    def __init__(self, base_url: str, avatar_id: str = None):
        self.base_url = base_url.rstrip('/')
        self.avatar_id = avatar_id
        self.sessionid = None
        self.connected = False
        self.state = 'idle'          # idle | connecting | connected | failed | closed
        self.error = ''
        self.frames_received = 0
        self.audio_frames_received = 0
        self._loop = None
        self._pc = None
        self._thread = None
        self._lock = threading.Lock()

    # ── public ────────────────────────────────────────────
    def connect(self, timeout_s: float = 30.0) -> str:
        """阻塞直到 connected；返回 sessionid。失败抛 SessionLinkError。"""
        with self._lock:
            self._close_internal()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.connected:
                return self.sessionid
            if self.state in ('failed', 'closed'):
                raise SessionLinkError(self.error or 'connection failed')
            time.sleep(0.2)
        raise SessionLinkError(f'connect timeout ({timeout_s}s), state={self.state}')

    def invalidate(self, reason: str):
        self.state = 'failed'
        self.error = reason
        self.connected = False
        self.sessionid = None

    def close(self):
        with self._lock:
            self._close_internal()
        self.state = 'closed'
        self.connected = False
        self.sessionid = None

    # ── internal ──────────────────────────────────────────
    def _close_internal(self):
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._stop_loop)

    def _stop_loop(self):
        try:
            if self._pc is not None:
                asyncio.ensure_future(self._pc.close())
        except Exception:
            pass
        if self._loop is not None:
            self._loop.stop()

    def _fail(self, msg: str):
        """标记会话失败并停掉事件循环（_loop 可能尚未创建, 此时只记状态）。"""
        self.state = 'failed'
        self.error = msg
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)

    def _run(self):
        try:
            from aiortc import RTCPeerConnection, RTCSessionDescription
        except ImportError as e:
            self._fail(f'aiortc 不可用: {e}')
            return
        self.state = 'connecting'
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop

        async def _main():
            pc = RTCPeerConnection()
            self._pc = pc
            pc.addTransceiver('audio', direction='recvonly')
            pc.addTransceiver('video', direction='recvonly')

            @pc.on('connectionstatechange')
            async def _cstate():
                st = pc.connectionState
                if st == 'connected':
                    self.connected = True
                    self.state = 'connected'
                elif st in ('failed', 'closed', 'disconnected'):
                    self.state = 'failed'
                    self.error = f'peerconnection {st}'
                    self.connected = False

            # 注意: on('track') 必须在 setRemoteDescription 之前注册——
            # track 事件在 SDP 应用时同步触发, 注册晚了会永远错过（踩过坑）。
            from aiortc.mediastreams import MediaStreamError

            def _on_track(track):
                log.info('[session_link] on_track: %s', track.kind)

                async def _consume():
                    try:
                        while True:
                            await track.recv()
                            if track.kind == 'video':
                                self.frames_received += 1
                            else:
                                self.audio_frames_received += 1
                    except MediaStreamError:
                        pass   # pc.close() 正常退出
                    except Exception as e:
                        # 连接异常断开 → 标记失效, 编排器重绑
                        self.state = 'failed'
                        self.error = f'consume({track.kind}) died: {e}'
                        self.connected = False

                asyncio.ensure_future(_consume())

            pc.on('track', _on_track)

            offer = await pc.createOffer()
            await pc.setLocalDescription(offer)
            # 等 ICE gathering 完成（本地 host candidate 足够, 短超时）
            try:
                await asyncio.wait_for(_ice_done(pc), timeout=10)
            except asyncio.TimeoutError:
                pass

            body = {'sdp': pc.localDescription.sdp, 'type': 'offer'}
            if self.avatar_id:
                body['avatar'] = self.avatar_id
            req = urllib.request.Request(
                f'{self.base_url}/offer',
                data=json.dumps(body).encode(),
                headers={'Content-Type': 'application/json'})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    if r.status != 200:
                        raise SessionLinkError(f'/offer HTTP {r.status}')
                    ans = json.loads(r.read().decode())
            except SessionLinkError:
                raise
            except Exception as e:
                raise SessionLinkError(f'/offer 请求失败: {e}')
            # vendor 约定: 出错时返回 {"code": -1, "msg": ...} 或无 sessionid
            if ans.get('code') not in (None, 0) or not ans.get('sessionid') or not ans.get('sdp'):
                raise SessionLinkError(f'/offer 业务失败: {ans}')
            self.sessionid = str(ans['sessionid'])
            await pc.setRemoteDescription(
                RTCSessionDescription(sdp=ans['sdp'], type=ans['type']))

        async def _ice_done(pc):
            ev = asyncio.Event()
            @pc.on('icegatheringstatechange')
            async def _():
                if pc.iceGatheringState == 'complete':
                    ev.set()
            if pc.iceGatheringState == 'complete':
                return
            await ev.wait()

        try:
            loop.run_until_complete(_main())
            loop.run_forever()
        except SessionLinkError as e:
            self._fail(str(e))
        except Exception as e:
            self._fail(f'session link 异常: {e}')
        finally:
            try:
                loop.close()
            except Exception:
                pass

    def status(self) -> dict:
        return {'state': self.state, 'sessionid': self.sessionid,
                'connected': self.connected, 'error': self.error,
                'frames_received': self.frames_received,
                'audio_frames_received': self.audio_frames_received}
