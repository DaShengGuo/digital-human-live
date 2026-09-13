# -*- coding: utf-8 -*-
"""platform — 平台适配层（阶段F）。

能力声明式设计: 每项能力(连接/状态/弹幕/开播/下播)独立声明, 不可用时
返回明确状态, 控制台如实展示, 不虚构 API、不把模拟当真实。

douyin_companion: 抖音直播伴侣集成路径。
- 直播伴侣无公开远程控制 API; 开播/下播必须由人在伴侣界面操作
  （账号安全优先, 不做 UI 自动化点击, 避免风控）。
- 画面接入: 数字人 WebRTC 预览窗口 / 虚拟摄像头（阶段C virtualcam 路线）
  由直播伴侣「添加摄像头/窗口」源采集。
- 弹幕: 真实获取暂未验证 → capabilities 如实标 unknown, 阶段G 再核查。
"""
import subprocess
import logging

log = logging.getLogger('platform')


class Capability:
    UNKNOWN = 'unknown'
    UNAVAILABLE = 'unavailable'
    MANUAL = 'manual'        # 能力存在但必须人工执行
    OK = 'ok'                # 已实测可用


class PlatformAdapter:
    """平台能力基类。子类按实际支持情况覆写。"""
    name = 'base'

    def __init__(self):
        self.room_id = None
        self.connected = False

    def capabilities(self) -> dict:
        return {
            'connect': Capability.UNKNOWN,
            'status_query': Capability.UNKNOWN,
            'danmaku_subscribe': Capability.UNKNOWN,
            'start_live': Capability.UNKNOWN,
            'stop_live': Capability.UNKNOWN,
        }

    def connect(self, room_id: str):
        raise NotImplementedError

    def disconnect(self):
        raise NotImplementedError

    def live_status(self) -> str:
        """'live'|'offline'|'unknown' — 无法确认时必须返回 unknown。"""
        return 'unknown'

    def subscribe_danmaku(self, callback):
        raise NotImplementedError


class DouyinCompanionAdapter(PlatformAdapter):
    """抖音直播伴侣: 进程存在性可查, 开播状态/弹幕本期不自动获取。

    实测(2026-09-10): 安装于 D:\\webcast_mate, 主进程「直播伴侣.exe」
    （launcher 为「直播伴侣 Launcher.exe」）。tasklist 对中文映像名匹配
    受控制台代码页影响, 用 PowerShell Get-Process 检测。"""
    name = 'douyin_companion'

    PROC_NAMES = ('直播伴侣', 'douyin_live_streamer', 'webcast_mate')

    def capabilities(self) -> dict:
        return {
            'connect': Capability.MANUAL,           # 人工登录+绑定直播间
            'status_query': Capability.UNKNOWN,     # 伴侣无查询接口, 需人工/画面确认
            'danmaku_subscribe': Capability.UNKNOWN,
            'start_live': Capability.MANUAL,        # 开播按钮由人点击(风控考量)
            'stop_live': Capability.MANUAL,
            'companion_process_detect': Capability.OK,   # 进程存在性已可检测
        }

    def companion_running(self) -> bool:
        """用 tasklist CSV 输出（GBK 解码, 规避控制台代码页与 ctypes 结构体长度问题）。"""
        try:
            out = subprocess.run(
                ['tasklist', '/FO', 'CSV', '/NH'],
                capture_output=True, timeout=10)
            text = out.stdout.decode('gbk', errors='ignore')
        except Exception:
            log.exception('tasklist 检测失败')
            return False
        for line in text.splitlines():
            parts = line.split('","')
            if not parts:
                continue
            name = parts[0].strip('"').lower()
            if any(p.lower() in name for p in self.PROC_NAMES):
                return True
        return False

    def connect(self, room_id: str):
        if not self.companion_running():
            return False, '直播伴侣进程未检测到 — 请打开并登录抖音直播伴侣'
        self.room_id = room_id or 'manual-bound'
        self.connected = True
        return True, ('伴侣在运行; 直播间绑定为人工步骤: 请在伴侣中选好直播标题/分类,'
                      '「摄像头」源选择 OBS虚拟摄像头 或「窗口」源选择数字人预览页')

    def disconnect(self):
        self.connected = False

    def live_status(self) -> str:
        # 无可靠自动检测手段 → 如实 unknown, 控制台提示人工确认
        return 'unknown'

    def subscribe_danmaku(self, callback):
        raise NotImplementedError('弹幕自动获取未实现(阶段G核查接入方式)')


class MockPlatformAdapter(PlatformAdapter):
    """开发用模拟平台（阶段G弹幕测试用）; 与真实接入严格区分, 状态永远标注 MOCK。"""
    name = 'mock'

    def capabilities(self) -> dict:
        return {k: Capability.OK for k in
                ('connect', 'status_query', 'danmaku_subscribe',
                 'start_live', 'stop_live')}

    def connect(self, room_id: str):
        self.room_id = room_id
        self.connected = True
        return True, 'MOCK 平台已连接（非真实接入）'

    def disconnect(self):
        self.connected = False

    def live_status(self) -> str:
        return 'live' if self.connected else 'offline'

    def subscribe_danmaku(self, callback):
        raise NotImplementedError('MOCK 弹幕源在阶段G的 danmaku_simulator 中提供')


REGISTRY = {
    'douyin_companion': DouyinCompanionAdapter,
    'mock': MockPlatformAdapter,
}


def get_adapter(name: str = 'douyin_companion') -> PlatformAdapter:
    cls = REGISTRY.get(name)
    if not cls:
        raise ValueError(f'未知平台 {name}, 可选: {list(REGISTRY)}')
    return cls()
