# -*- coding: utf-8 -*-
"""OBS 控制适配层（阶段F）。

原则: OBS 已推流 ≠ 平台已开播; OBS 未安装/未连接时如实报告 unknown, 不伪造成功。
通过 OBS WebSocket(obsws) 协议控制; OBS 未安装或未启用 ws 服务时, 返回能力=unavailable。
凭据(obs_ws_password)从环境变量 OBS_WS_PASSWORD 读取, 不入库不入日志。
"""
import os
import json
import socket
import logging

log = logging.getLogger('obs_control')

DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 4455      # OBS WebSocket v5 默认端口


def ws_available(host=None, port=None, timeout_s=2):
    """快速探测 OBS WebSocket 端口是否监听。"""
    h = host or DEFAULT_HOST
    p = port or int(os.environ.get('OBS_WS_PORT', DEFAULT_PORT))
    try:
        with socket.create_connection((h, p), timeout=timeout_s):
            return True
    except OSError:
        return False


class ObsController:
    """最小 OBS 控制器。连接失败 → state='unknown'（不是 False, 更不是成功）。"""

    def __init__(self, host=None, port=None):
        self.host = host or DEFAULT_HOST
        self.port = port or int(os.environ.get('OBS_WS_PORT', DEFAULT_PORT))
        self._ws = None
        self.state = 'unknown'        # unknown | connected | disconnected
        self.error = ''

    def connect(self):
        try:
            import obsws_python as obs
            self._ws = obs.ReqClient(
                host=self.host, port=self.port,
                password=os.environ.get('OBS_WS_PASSWORD', ''), timeout=3)
            ver = self._ws.get_version()
            self.state = 'connected'
            self.error = ''
            log.info('OBS 已连接: %s', ver)
            return True, 'connected'
        except ImportError:
            self.state = 'unavailable'
            self.error = 'obswebsocket 未安装（pip install obsws-python）'
            return False, self.error
        except Exception as e:
            self.state = 'disconnected'
            self.error = str(e)[:120]
            return False, self.error

    def output_status(self):
        """真实输出状态。无法确认时返回 unknown, 不猜测。"""
        if self.state != 'connected':
            return {'streaming': 'unknown', 'recording': 'unknown',
                    'note': self.state + (': ' + self.error if self.error else '')}
        try:
            a = self._ws.get_stream_status()
            return {'streaming': bool(a.output_active),
                    'congestion': getattr(a, 'congestion', None),
                    'note': 'obs ws ok'}
        except Exception as e:
            return {'streaming': 'unknown', 'note': f'查询失败: {e}'}

    def start_output(self):
        if self.state != 'connected':
            return False, f'OBS 未连接（{self.state}）, 不能开播'
        try:
            self._ws.start_stream()
            return True, 'streaming started'
        except Exception as e:
            return False, str(e)

    def stop_output(self):
        if self.state != 'connected':
            return False, f'OBS 未连接（{self.state}）, 不能停播'
        try:
            self._ws.stop_stream()
            return True, 'streaming stopped'
        except Exception as e:
            return False, str(e)

    def scene_status(self):
        if self.state != 'connected':
            return {'current': 'unknown', 'note': self.state}
        try:
            return {'current': self._ws.get_current_program_scene().current_program_scene_name}
        except Exception as e:
            return {'current': 'unknown', 'note': str(e)}
