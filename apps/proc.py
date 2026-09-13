# -*- coding: utf-8 -*-
"""进程管理 — 用统一方式拉起/停止 LiveTalking 与 tts_gateway 子进程。

设计要点:
- 平台分支: Windows 用 CREATE_NEW_PROCESS_GROUP + CTRL_BREAK/taskkill;
  Linux（云端 GPU）用 start_new_session + killpg(SIGTERM→SIGKILL), 孙进程同组退出。
- 每个 child 记录 name/popen/logfile/state, stop 时先 terminate 再超时 kill。
- PID 文件写到 logs/, stop / healthcheck 脚本据此跨进程操作。
"""
import os
import sys
import time
import signal
import subprocess
import logging

log = logging.getLogger('proc')

IS_WINDOWS = os.name == 'nt'


def _spawn_kwargs() -> dict:
    """让子进程独占进程组, stop 时可整组终止（含孙进程）。"""
    if IS_WINDOWS:
        return {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
    return {'start_new_session': True}


def _signal_group(pid: int, sig) -> None:
    """向 pid 所在进程组发信号（Windows 只支持 CTRL_BREAK 语义）。"""
    if IS_WINDOWS:
        os.kill(pid, sig)
    else:
        os.killpg(os.getpgid(pid), sig)

STATE_STOPPED = 'stopped'
STATE_STARTING = 'starting'
STATE_RUNNING = 'running'
STATE_EXITED = 'exited'


class ManagedProcess:
    def __init__(self, name: str, cmd: list, workdir: str, env: dict, logfile: str):
        self.name = name
        self.cmd = cmd
        self.workdir = workdir
        self.env = env
        self.logfile = logfile
        self.popen = None
        self.state = STATE_STOPPED
        self.started_at = None
        self.exit_code = None

    def start(self):
        os.makedirs(os.path.dirname(self.logfile), exist_ok=True)
        # 平台差异: Windows 用 CREATE_NEW_PROCESS_GROUP, Linux 用 start_new_session
        spawn = ({'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
                 if os.name == 'nt' else {'start_new_session': True})
        with open(self.logfile, 'ab') as lf:
            self.popen = subprocess.Popen(
                self.cmd,
                cwd=self.workdir,
                env=self.env,
                stdout=lf,
                stderr=subprocess.STDOUT,
                **spawn,
            )
        self.state = STATE_STARTING
        self.started_at = time.time()
        self.exit_code = None
        log.info('[%s] started pid=%s cmd=%s', self.name, self.popen.pid, ' '.join(self.cmd))
        return self.popen.pid

    def poll(self):
        if self.popen is None:
            return None
        rc = self.popen.poll()
        if rc is not None and self.state == STATE_RUNNING:
            self.state = STATE_EXITED
            self.exit_code = rc
            log.warning('[%s] exited rc=%s', self.name, rc)
        elif rc is not None and self.state == STATE_STARTING:
            self.state = STATE_EXITED
            self.exit_code = rc
        return rc

    def is_running(self) -> bool:
        return self.popen is not None and self.popen.poll() is None

    def stop(self, timeout_s: float = 10):
        if self.popen is None or self.popen.poll() is not None:
            self.state = STATE_STOPPED
            return
        try:
            # 发到进程组, 让孙进程也收到
            if os.name == 'nt':
                os.kill(self.popen.pid, signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(os.getpgid(self.popen.pid), signal.SIGTERM)
        except (OSError, ValueError):
            try:
                self.popen.terminate()
            except OSError:
                pass
        try:
            self.popen.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            log.warning('[%s] 未在 %ss 内退出, 强杀', self.name, timeout_s)
            self.popen.kill()
            self.popen.wait(timeout=5)
        self.state = STATE_STOPPED
        log.info('[%s] stopped', self.name)


def pid_file(logs_dir: str) -> str:
    return os.path.join(logs_dir, 'processes.pid')


def write_pid_file(logs_dir: str, procs: dict, meta: dict = None):
    """PID 文件带本轮归属信息（run_id/编排器 PID/创建时间）, 供 stop 侧校验, 防 PID 复用误杀。"""
    import json
    os.makedirs(logs_dir, exist_ok=True)
    data = {n: {'pid': p.popen.pid if p.popen else None, 'state': p.state,
                'cmd': ' '.join(p.cmd)} for n, p in procs.items()}
    data['_meta'] = dict(meta or {})
    with open(pid_file(logs_dir), 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    write_owned_pids(logs_dir, procs, (meta or {}).get('orch_pid') or os.getpid())


def write_owned_pids(logs_dir: str, procs: dict, orch_pid: int):
    """写 logs/owned_pids.txt: 编排器自身 + 存活子进程 PID（每行一个）。

    供 scripts/gpu_owned_sampler.ps1 做进程级显存采样。任何重启子进程的路径
    都必须重写本文件, 否则采样器会继续跟踪已死 PID → 本链显存恒为 0。
    """
    pids = [int(orch_pid)]
    for p in procs.values():
        if p.popen is not None and p.state != STATE_EXITED:
            pids.append(int(p.popen.pid))
    path = os.path.join(logs_dir, 'owned_pids.txt')
    try:
        os.makedirs(logs_dir, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(str(x) for x in pids) + '\n')
    except OSError:
        log.warning('写 %s 失败（进程级显存采样将退回整卡口径）', path)


def clear_pid_file(logs_dir: str):
    try:
        os.remove(pid_file(logs_dir))
    except OSError:
        pass


def kill_by_pid(pid: int, timeout_s: float = 10):
    """跨进程停止: 先温和信号再强杀（Win: CTRL_BREAK+taskkill /T; Linux: killpg）。"""
    if os.name != 'nt':
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(os.getpgid(pid), sig)
            except OSError:
                pass
            time.sleep(0.5)
        return
    try:
        os.kill(pid, signal.CTRL_BREAK_EVENT)
        time.sleep(0.5)
    except OSError:
        pass
    subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'],
                   capture_output=True, timeout=timeout_s)


def python_exe(profile_cfg: dict, which: str) -> str:
    """取档位里配置的 venv python 绝对路径; 不存在时回退系统 python 并告警。"""
    from apps import common_config
    p = (profile_cfg.get('paths', {}) or {}).get(f'python_{which}', '')
    exe = common_config.resolve_path('', p) if p else sys.executable
    if not os.path.exists(exe):
        log.warning('venv python 不存在: %s, 回退当前 python', exe)
        exe = sys.executable
    return exe
