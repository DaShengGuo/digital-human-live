# -*- coding: utf-8 -*-
"""编排器主进程 — 拉起 tts_gateway 与 LiveTalking，真实会话绑定，文本队列与看门狗。

生命周期（A3）:
  preflight → 启动 tts（等待 model_loaded+warmup_done）→ 启动 LiveTalking
  （等待端口+业务可用）→ 会话绑定（aiortc /offer, 真实 sessionid）→ live
停止: 先拒接单 → 停派发 → 断会话 → 只终止本轮创建的子进程。

HTTP API (orchestrator.listen_port):
  POST /say {text}   排队播报（合并/限流/可打断）
  POST /chat {text}  LLM 智能问答（透传 vendor /human type=chat, 不排队）
  POST /interrupt    打断当前播报并清队列
  GET /status | GET /healthz
用法: python -m apps.orchestrator.main --profile fallback_8g
"""
import os
import sys
import json
import time
import queue
import socket
import logging
import threading
import argparse
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO)

from apps import common_config, proc                     # noqa: E402
from apps.watchdog import Watchdog                       # noqa: E402
from apps.session_link import LiveSessionLink, SessionLinkError, PageSessionLink   # noqa: E402

log = logging.getLogger('orchestrator')

RUN_ID = time.strftime('%Y%m%d-%H%M%S-') + str(os.getpid())


class PreflightError(RuntimeError):
    pass


class Orchestrator:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        orch = cfg.get('orchestrator', {})
        self.host = orch.get('listen_host', '127.0.0.1')
        self.port = orch.get('listen_port', 8020)
        tq = orch.get('text_queue', {})
        self.max_pending = tq.get('max_pending', 16)
        self.min_interval = tq.get('min_interval_ms', 300) / 1000.0
        self.merge_window = tq.get('merge_window_ms', 800) / 1000.0

        self.textq = queue.Queue(maxsize=self.max_pending)
        self.procs = {}
        self.watchdog = None
        self.stop_flag = threading.Event()
        self.accepting = threading.Event()      # A3: 停止先行关门
        self.started_at = time.time()
        self._last_send = 0.0
        self._degraded = False
        self.phase = 'init'                     # init|preflight|tts|livetalking|session|live|failed
        self.phase_error = ''
        self.link = None                        # LiveSessionLink
        # 阶段B: job 状态机
        self._job_seq = 0
        self._job_lock = threading.Lock()
        self._jobs = {}                         # job_id → job dict（含终态, 供查询）
        self._interrupt_epoch = 0
        self.job_ttl_s = float(tq.get('job_ttl_s', 60))   # 待播有效期

    # ── A3 预检 ────────────────────────────────────────────
    def preflight(self):
        name = self.cfg.get('_profile_name')
        allowed, reason = common_config.is_profile_enabled(name)
        if not allowed:
            raise PreflightError(f'档位 {name} 未启用: {reason}')

        lt_cfg, tts_cfg = self.cfg.get('livetalking', {}), self.cfg.get('tts', {})
        paths = self.cfg.get('paths', {})

        # python 路径必须真实存在（禁止静默回退）
        for key in ('livetalking', 'tts'):
            cfg_sec = self.cfg.get(key, {})
            if not cfg_sec.get('enabled'):
                continue
            py = common_config.resolve_path('', (paths or {}).get(f'python_{key}', ''))
            if not py or not os.path.exists(py):
                raise PreflightError(f'python_{key} 不存在: {py}（先跑 bootstrap 或修正 configs）')

        # TTS 模型目录
        if tts_cfg.get('enabled'):
            md = common_config.resolve_path('', tts_cfg.get('model_dir', ''))
            if not any(os.path.exists(os.path.join(md, y))
                       for y in ('cosyvoice.yaml', 'cosyvoice2.yaml', 'cosyvoice3.yaml')):
                raise PreflightError(f'TTS 模型目录缺 cosyvoice*.yaml: {md}')

        # LiveTalking 资产
        if lt_cfg.get('enabled'):
            workdir = common_config.resolve_path('', paths.get('vendor_livetalking', ''))
            avatar = os.path.join(workdir, 'data', 'avatars', str(lt_cfg.get('avatar_id', '')))
            if not os.path.isdir(avatar):
                raise PreflightError(f'avatar 资产不存在: {avatar}（stable 档需 ultralight 资产, '
                                     f'当前请用 -Profile fallback）')
            if lt_cfg.get('model') == 'wav2lip' and \
               not os.path.exists(os.path.join(workdir, 'models', 'wav2lip.pth')):
                raise PreflightError(f'缺 models/wav2lip.pth: {workdir}')

        # 端口归属: 被占用时拒绝启动（占用者不可能是本轮进程, 本轮还没启动）
        self._check_ports_free()
        self.phase = 'preflight'

    def _managed_ports(self):
        ports = []
        if self.cfg.get('tts', {}).get('enabled'):
            ports.append(int(self.cfg['tts'].get('listen_port', 8011)))
        if self.cfg.get('livetalking', {}).get('enabled'):
            ports.append(int(self.cfg['livetalking'].get('listenport', 8010)))
        ports.append(int(self.port))
        return ports

    def _check_ports_free(self):
        for p in self._managed_ports():
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind(('127.0.0.1', p))
                except OSError:
                    raise PreflightError(
                        f'端口 {p} 已被占用（非本编排器进程）, 先运行 stop.ps1 或排查占用')

    # ── 进程拉起 ────────────────────────────────────────────
    def _build_procs(self):
        paths = self.cfg.get('paths', {})
        logs_dir = common_config.resolve_path('', paths.get('logs_dir', 'logs'))
        lt_cfg = self.cfg.get('livetalking', {})
        tts_cfg = self.cfg.get('tts', {})

        procs = {}
        order = self.cfg.get('orchestrator', {}).get('startup', {}).get(
            'order', ['tts', 'livetalking'])

        if tts_cfg.get('enabled'):
            cmd = [self._require_py('tts'), '-m', 'apps.tts_gateway.server',
                   '--profile', self.cfg['_profile_name']]
            procs['tts'] = proc.ManagedProcess(
                'tts', cmd, _REPO, common_config.build_env(self.cfg, 'tts'),
                os.path.join(logs_dir, 'tts.log'))

        if lt_cfg.get('enabled'):
            lt_args = common_config.livetalking_cli_args(self.cfg)
            workdir = common_config.resolve_path('', paths.get('vendor_livetalking', 'vendor/LiveTalking'))
            cmd = [self._require_py('livetalking'), lt_cfg.get('entry', 'app.py')] + lt_args
            procs['livetalking'] = proc.ManagedProcess(
                'livetalking', cmd, workdir, common_config.build_env(self.cfg, 'livetalking'),
                os.path.join(logs_dir, 'livetalking.log'))

        self.procs = procs
        return order

    def _require_py(self, which: str) -> str:
        p = (self.cfg.get('paths', {}) or {}).get(f'python_{which}', '')
        exe = common_config.resolve_path('', p)
        if not exe or not os.path.exists(exe):
            raise PreflightError(f'python_{which} 不存在: {exe}')
        return exe

    def start_all(self):
        order = self._build_procs()
        su = self.cfg.get('orchestrator', {}).get('startup', {})
        timeout = su.get('ready_timeout_s', 180)
        probe = su.get('port_probe_interval_s', 2)
        logs_dir = common_config.resolve_path('', self.cfg.get('paths', {}).get('logs_dir', 'logs'))

        for name in order:
            p = self.procs[name]
            p.start()
            p.state = proc.STATE_RUNNING
            log.info('[%s] 启动中 pid=%s 日志: %s', name, p.popen.pid, p.logfile)
        proc.write_pid_file(logs_dir, self.procs,
                            meta={'run_id': RUN_ID, 'orch_pid': os.getpid(),
                                  'profile': self.cfg.get('_profile_name')})

        try:
            # TTS 就绪: /healthz model_loaded && warmup_done（加载失败带 load_error, 不静默）
            if 'tts' in self.procs:
                self.phase = 'tts'
                ok, why = self._wait_tts_ready(timeout, probe)
                if not ok:
                    raise PreflightError(f'TTS 未就绪: {why}')

            # LiveTalking 就绪: 端口 + /api/admin/config 业务 code==0
            if 'livetalking' in self.procs:
                self.phase = 'livetalking'
                lt_port = self.cfg['livetalking'].get('listenport', 8010)
                if not self._wait_port(lt_port, timeout):
                    raise PreflightError(f'LiveTalking 端口 {lt_port} 未就绪（查 logs/livetalking.log）')
                ok, why = self._lt_business_ok(timeout_s=30)
                if not ok:
                    raise PreflightError(f'LiveTalking 业务未就绪: {why}')
        except Exception:
            # A3: 本轮启动失败 → 清理本轮创建的进程, 不带病继续
            log.error('启动期失败, 清理本轮子进程')
            for p in self.procs.values():
                try:
                    p.stop()
                except Exception:
                    pass
            proc.clear_pid_file(logs_dir)
            raise

    def _wait_tts_ready(self, timeout_s, probe_s):
        tts_port = self.cfg.get('tts', {}).get('listen_port', 8011)
        url = f'http://127.0.0.1:{tts_port}/healthz'
        deadline = time.time() + timeout_s
        last = ''
        fails = 0
        while time.time() < deadline:
            self._poll_procs()
            if any(p.state == proc.STATE_EXITED for p in self.procs.values()):
                return False, 'tts/livetalking 子进程启动期退出'
            try:
                with urllib.request.urlopen(url, timeout=3) as r:
                    d = json.loads(r.read().decode())
                if d.get('model_loaded') and d.get('warmup_done'):
                    log.info('TTS 就绪 (model+warmup)')
                    return True, ''
                if d.get('load_error'):
                    return False, f"load_error={d['load_error']}"
                last = f"model_loaded={d.get('model_loaded')} warmup={d.get('warmup_done')}"
            except Exception as e:
                last = str(e)
                # 每次失败都落一条节流告警(约每 15s 一条), 预检超时后能还原完整失败史
                fails += 1
                if fails == 1 or fails % 8 == 0:
                    log.warning('TTS 探活失败(%d): %s', fails, e)
            time.sleep(probe_s)
        return False, last or 'timeout'

    def _lt_business_ok(self, timeout_s=30):
        lt_port = self.cfg['livetalking'].get('listenport', 8010)
        url = f'http://127.0.0.1:{lt_port}/api/admin/config'
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=5) as r:
                    if r.status == 200:
                        d = json.loads(r.read().decode())
                        if d.get('code') == 0:
                            return True, ''
                        return False, f'code={d.get("code")} msg={d.get("msg")}'
            except Exception:
                time.sleep(2)
        return False, 'admin/config 探测超时'

    def _poll_procs(self):
        for p in self.procs.values():
            p.poll()

    def _wait_port(self, port: int, timeout_s: float) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            self._poll_procs()
            if any(p.state == proc.STATE_EXITED for p in self.procs.values()):
                return False
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=1):
                    return True
            except OSError:
                time.sleep(self.cfg.get('orchestrator', {}).get('startup', {})
                           .get('port_probe_interval_s', 2))
        return False

    # ── A2 会话绑定 ─────────────────────────────────────────
    def ensure_session(self, rebind=False, timeout_s: float = None):
        """建立/重绑页面会话。timeout_s 缺省按模式取（page=180s 等人工点连接）。"""
        if self.link and self.link.connected and not rebind:
            return self.link.sessionid
        lt_port = self.cfg.get('livetalking', {}).get('listenport', 8010)
        avatar_id = self.cfg.get('livetalking', {}).get('avatar_id', '')
        if self.link:
            try:
                self.link.close()
            except Exception:
                pass
        self.phase = 'session'
        # 会话绑定模式（配置驱动, 生产用 page）:
        #   page   = 绑定浏览器/OBS页面建立的 WebRTC 会话（观众看到的就是它）
        #   aiortc = 编排器自建会话（开发自检, 画面不对外）
        mode = self.cfg.get('orchestrator', {}).get('session_bind', 'aiortc')
        if mode == 'page':
            link = PageSessionLink(f'http://127.0.0.1:{lt_port}', avatar_id=avatar_id)
            sid = link.connect(timeout_s=timeout_s or 180)   # 等人工在页面点「开始连接」
        else:
            link = LiveSessionLink(f'http://127.0.0.1:{lt_port}', avatar_id=avatar_id)
            sid = link.connect(timeout_s=timeout_s or 60)
        self.link = link
        self.phase = 'live'
        log.info('WebRTC 会话已绑定 sessionid=%s (mode=%s)', sid, mode)
        return sid

    def _session_or_fail(self):
        if self.link and getattr(self.link, 'refresh', None):
            self.link.refresh()   # page 模式: 页面刷新后 sessionid 会变
        if self.link and self.link.connected:
            return self.link.sessionid
        raise RuntimeError('无已绑定的直播会话（phase=%s）' % self.phase)

    def _vendor_sessions(self):
        """vendor 当前活跃会话列表（供控制台提示多标签页抢声音; 不可达返回 None）。"""
        lt_port = self.cfg.get('livetalking', {}).get('listenport', 8010)
        try:
            with urllib.request.urlopen(
                    f'http://127.0.0.1:{lt_port}/api/admin/sessions', timeout=3) as r:
                d = json.loads(r.read().decode())
            if isinstance(d, dict) and d.get('code') == 0:
                return [str(s.get('sessionid'))
                        for s in (d.get('data', {}).get('sessions') or [])
                        if s.get('sessionid')]
        except Exception:
            pass
        return None

    def _session_speaking(self) -> bool:
        """LiveTalking 实时说话状态（导演循环据此决定何时补播）。"""
        if not (self.link and self.link.connected):
            return False
        try:
            ok, data = self._lt_post('/is_speaking', {'sessionid': self.link.sessionid},
                                     timeout_s=3.0)
            return bool(ok and isinstance(data, dict) and data.get('data'))
        except Exception:
            return False

    # ── 停止 ────────────────────────────────────────────────
    def stop_all(self, close_session=True):
        self.accepting.clear()                # 1. 关门: 拒新单
        self.stop_flag.set()                  # 2. 停派发
        time.sleep(0.3)
        if close_session and self.link:       # 3. 断会话
            try:
                self.link.close()
            except Exception:
                pass
            self.link = None
        for p in self.procs.values():         # 4. 终止本轮子进程
            p.stop()
        proc.clear_pid_file(common_config.resolve_path(
            '', self.cfg.get('paths', {}).get('logs_dir', 'logs')))

    def restart_proc(self, name: str):
        p = self.procs.get(name)
        if not p:
            return
        log.warning('重启子进程 %s', name)
        self.accepting.clear()                # 重启期间拒新单
        try:
            p.stop()
            p.state = proc.STATE_STOPPED
            p.exit_code = None
            p.start()
            p.state = proc.STATE_RUNNING
            if name == 'livetalking':
                # _wait_port 返回 bool（单值）, 不能解包（原 TypeError 事故根因）
                ok = self._wait_port(
                    self.cfg['livetalking'].get('listenport', 8010),
                    self.cfg['orchestrator']['startup'].get('ready_timeout_s', 180))
                if ok:
                    try:
                        self.ensure_session(rebind=True)   # 会话随进程失效, 强制重绑
                    except Exception as e:
                        log.error('重绑会话失败: %s', e)
        finally:
            # 重启会换 PID → 必须刷新 owned_pids.txt, 否则进程级显存采样跟踪死 PID
            logs_dir = common_config.resolve_path(
                '', self.cfg.get('paths', {}).get('logs_dir', 'logs'))
            proc.write_owned_pids(logs_dir, self.procs, os.getpid())
            self.accepting.set()              # 无论成败都要恢复接单, 不能永久关门

    def restart_proc_lip(self):
        self.restart_proc('livetalking')

    def restart_proc_tts(self):
        self.restart_proc('tts')

    def degrade(self, target: str):
        """降级 = 以 target 档位重启整组进程; 全程校验, 失败转 needs_attention。"""
        if not target or self._degraded:
            return
        allowed, reason = common_config.is_profile_enabled(target)
        if not allowed:
            log.error('降级目标 %s 不可用: %s — 保持当前档位, 转人工', target, reason)
            return
        self._degraded = True
        log.warning('执行降级 → %s', target)
        try:
            new_cfg = common_config.load_profile(target)
            self.stop_all()
            self.cfg = new_cfg
            self.stop_flag.clear()
            self.accepting.set()
            self.preflight()                  # 目标档位资产/环境预检
            self.start_all()
            self.ensure_session(rebind=True)  # 进程引用与会话随新档位更新
            # 原位刷新 watchdog: run_forever 线程继续用同一对象, 阈值/目标/进程引用换新
            if self.watchdog:
                self.watchdog.reload(self.cfg, self.procs)
            # stop_all 置位 stop_flag 会终结旧 _pump_text 线程, 必须重新拉起派发
            threading.Thread(target=self._pump_text, daemon=True).start()
            log.info('降级完成 → %s (phase=%s)', target, self.phase)
        except Exception as e:
            log.error('降级失败: %s — 进入 needs_attention, 不自动重试', e)
            self.phase = 'failed'
            self.phase_error = f'degrade: {e}'
        finally:
            self._degraded = False

    # ── 文本队列（阶段B: job 状态机）─────────────────────────
    def _new_job(self, text: str) -> dict:
        """生成播报任务: job_id 唯一, 带运行轮次与有效期。"""
        self._job_seq += 1
        return {'job_id': f'{RUN_ID}-{self._job_seq}', 'run_id': RUN_ID,
                'text': text, 'created_at': time.time(),
                'expires_at': time.time() + self.job_ttl_s,
                'state': 'queued',       # queued|dispatched|playing|done|cancelled|expired|failed
                'error': '',
                # 出生即记录当前打断轮次: interrupt() 推进 epoch 后,
                # 旧 epoch 的任务在 pump/合并缓冲中被取消, 不会迟播出
                'interrupt_epoch': self._interrupt_epoch}

    def say(self, text: str) -> dict:
        if not self.accepting.is_set():
            return {'ok': False, 'code': 'not_accepting',
                    'msg': '编排器未接单（启动中/停止中/重启中）'}
        text = (text or '').strip()
        if not text:
            return {'ok': False, 'code': 'bad_input', 'msg': 'empty text'}
        job = self._new_job(text)
        with self._job_lock:
            if self.textq.full():
                # 队列满: 明确拒绝（阶段B: 不再静默丢最旧——丢的是别人的已受理任务）
                job['state'] = 'failed'
                job['error'] = 'queue_full'
                self._jobs[job['job_id']] = job
                return {'ok': False, 'code': 'queue_full', 'job_id': job['job_id'],
                        'msg': f'待播队列已满({self.max_pending}), 拒绝接单',
                        'pending': self.textq.qsize()}
            self.textq.put(job)
            self._jobs[job['job_id']] = job
        return {'ok': True, 'job_id': job['job_id'], 'state': job['state'],
                'pending': self.textq.qsize()}

    def chat(self, text: str) -> dict:
        """LLM 智能问答: 透传 vendor /human type=chat。

        与 /say 的区别: 不进编排器队列（无合并/限流/过期）, 由 vendor 侧 LLM
        流式生成并逐句交给口型; 打断仍走 /interrupt。需在 profile 中启用
        llm_provider/llm_model 且密钥在环境变量（见 configs/profile_*.yaml）。
        """
        text = (text or '').strip()
        if not text:
            return {'ok': False, 'code': 'bad_input', 'msg': 'empty text'}
        try:
            sid = self._session_or_fail()
        except RuntimeError as e:
            return {'ok': False, 'code': 'no_session', 'msg': str(e)}
        ok, detail = self._lt_post('/human',
                                   {'sessionid': sid, 'type': 'chat', 'text': text},
                                   timeout_s=15.0)
        if ok:
            return {'ok': True, 'sessionid': sid, 'mode': 'chat'}
        return {'ok': False, 'code': 'upstream', 'msg': str(detail)[:200]}

    def _lt_post(self, path: str, payload: dict, timeout_s: float = 5.0):
        """LiveTalking 下游调用: HTTP 状态与 JSON 业务码双重校验。"""
        lt_port = self.cfg.get('livetalking', {}).get('listenport', 8010)
        url = f'http://127.0.0.1:{lt_port}{path}'
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as r:
                if r.status != 200:
                    return False, f'HTTP {r.status}'
                d = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return False, f'HTTP {e.code}'
        except Exception as e:
            return False, str(e)
        if not isinstance(d, dict) or d.get('code') != 0:
            return False, f'业务失败: {d}'
        return True, d

    def interrupt(self) -> dict:
        cancelled = []
        # 1) 打断当前播报（真实会话, 先断 downstream 再清队列, 防止 pump 在清空后又派发）
        interrupted = False
        interrupt_error = ''
        if self.link and self.link.connected:
            ok, detail = self._lt_post('/interrupt_talk', {'sessionid': self.link.sessionid})
            interrupted = ok
            if not ok:
                interrupt_error = str(detail)
        # 2) 记录打断轮次: 过滤迟到的旧派发回调
        with self._job_lock:
            self._interrupt_epoch += 1
        # 3) 队列中未派发的任务 → cancelled（pump 与 interrupt 并发时,
        #    刚被 pump 取走的任务无法撤回——这是底层队列的固有时序, 状态如实标记）
        while True:
            try:
                job = self.textq.get_nowait()
            except queue.Empty:
                break
            job['state'] = 'cancelled'
            job['interrupt_epoch'] = self._interrupt_epoch
            cancelled.append(job['job_id'])
        r = {'ok': True, 'cancelled': cancelled, 'interrupted': interrupted}
        if interrupt_error:
            r['error'] = interrupt_error   # 下游拒绝必须暴露, 不能静默
        return r

    def _pump_text(self):
        """派发循环（阶段B）: job 感知 + 保序合并 + 过期过滤 + 可中断合并缓冲。

        取消语义关键: 合并缓冲（buf_jobs）也参与取消。合并窗口内任务尚未
        派发, 若此时 interrupt() 打进来说明这些内容不该再播出——通过
        _interrupt_epoch 对比, 缓冲中的旧任务直接置 cancelled, 不派发。
        """
        buf_jobs = []
        buf_since = None
        while not self.stop_flag.is_set():
            try:
                job = self.textq.get(timeout=0.5)
            except queue.Empty:
                continue
            if not self.accepting.is_set():
                self._requeue_jobs(buf_jobs + [job])
                buf_jobs = []
                break
            # 过期任务直接淘汰（过期 = 用户等太久, 内容可能已不合时宜）
            if time.time() > job.get('expires_at', 0):
                job['state'] = 'expired'
                log.warning('任务过期淘汰: %s (%d chars)', job['job_id'], len(job['text']))
                buf_jobs, buf_since = [], None
                continue
            # 打断后进入缓冲的迟到任务: 直接取消（epoch 已推进）
            if job.get('interrupt_epoch', 0) < self._interrupt_epoch:
                job['state'] = 'cancelled'
                job['error'] = 'superseded_by_interrupt'
                log.info('打断后迟到任务取消: %s', job['job_id'])
                buf_jobs, buf_since = [], None
                continue
            buf_jobs.append(job)
            buf_since = buf_since or time.time()
            texts = [j['text'] for j in buf_jobs]
            merged = ' '.join(texts) if all(len(t) < 20 for t in texts) else buf_jobs[-1]['text']
            ready = (time.time() - buf_since >= self.merge_window) or self.textq.empty() or len(merged) > 60
            if not ready:
                # 合并窗口内收到新打断 → 缓冲任务全部取消
                if buf_jobs and self._interrupt_epoch > max(
                        j.get('interrupt_epoch', 0) for j in buf_jobs):
                    for j in buf_jobs:
                        j['state'] = 'cancelled'
                        j['error'] = 'cancelled_in_merge_window'
                    log.info('合并窗口内取消 %d 任务', len(buf_jobs))
                    buf_jobs, buf_since = [], None
                continue
            wait = self.min_interval - (time.time() - self._last_send)
            if wait > 0:
                time.sleep(wait)
            # 睡醒后(限流/合并窗口)再查一次: 若等待期间发生了打断, 缓冲任务必须取消
            if self._interrupt_epoch > max(j.get('interrupt_epoch', 0) for j in buf_jobs):
                for j in buf_jobs:
                    j['state'] = 'cancelled'
                    j['error'] = 'cancelled_during_dispatch_wait'
                log.info('派发等待期内打断, 取消 %d 任务', len(buf_jobs))
                buf_jobs, buf_since = [], None
                continue
            try:
                sid = self._session_or_fail()
            except RuntimeError as e:
                log.error('派发失败(无会话): %s — %d 任务退回队列', e, len(buf_jobs))
                self._requeue_jobs(buf_jobs)
                buf_jobs, buf_since = [], None
                time.sleep(1)
                continue
            ok, detail = self._lt_post('/human',
                                       {'sessionid': sid, 'type': 'echo', 'text': merged})
            if ok:
                self._last_send = time.time()
                for j in buf_jobs:
                    j['state'] = 'dispatched'
                    j['dispatched_at'] = time.time()
                    j['interrupt_epoch'] = self._interrupt_epoch
                log.info('已派发 %d chars (%d jobs → %s) → session=%s: %s',
                         len(merged), len(buf_jobs),
                         ','.join(j['job_id'].split('-')[-1] for j in buf_jobs),
                         sid, merged[:40])
            else:
                for j in buf_jobs:
                    j['state'] = 'failed'
                    j['error'] = str(detail)[:120]
                log.error('派发失败: %s', detail)
                if self.link and 'session not found' in str(detail):
                    self.link.invalidate('session-gone')
            buf_jobs, buf_since = [], None

    def _requeue_jobs(self, jobs):
        for job in reversed(jobs):
            try:
                self.textq.put_nowait(job)
            except queue.Full:
                job['state'] = 'failed'
                job['error'] = 'requeue_full'
                log.error('退回队列已满, 任务置失败: %s', job['job_id'][:40])

    # ── HTTP API ────────────────────────────────────────────
    def _make_handler(self):
        orch = self

        class Handler(BaseHTTPRequestHandler):
            def _json(self, obj, code=200):
                body = json.dumps(obj, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                n = int(self.headers.get('Content-Length') or 0)
                try:
                    return json.loads(self.rfile.read(n) or b'{}')
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return {}

            def do_GET(self):
                if self.path == '/healthz':
                    gpu = __import__('apps.gpu_sampler', fromlist=['x']).sample()
                    self._json({
                        'ok': (orch.phase == 'live'
                               and bool(orch.procs)
                               and all(p.is_running() for p in orch.procs.values())),
                        'phase': orch.phase, 'phase_error': orch.phase_error,
                        'run_id': RUN_ID,
                        'profile': orch.cfg.get('_profile_name'),
                        'uptime_s': int(time.time() - orch.started_at),
                        'procs': {n: {'pid': p.popen.pid if p.popen else None,
                                      'state': p.state} for n, p in orch.procs.items()},
                        'session': orch.link.status() if orch.link else None,
                        'gpu': {'mem_used': gpu.mem_used, 'mem_total': gpu.mem_total,
                                'temp': gpu.temp, 'util': gpu.util} if gpu else None,
                        'queue': orch.textq.qsize(),
                        'events': orch.watchdog.events[-10:] if orch.watchdog else [],
                    })
                elif self.path == '/status':
                    with orch._job_lock:
                        # 任务快照（最近 20 个, 含终态; 过滤当前轮次之外的历史轮）
                        recent = [j for j in orch._jobs.values()
                                  if j.get('run_id') == RUN_ID][-20:]
                    now_ts = time.time()
                    grace = float(orch.cfg.get('orchestrator', {})
                                  .get('dispatch_grace_s', 90))
                    # active = 宽限期内派发的任务。
                    # 任务没有"播完"回调（LiveTalking 不回传完成事件）, 派发后到出声前
                    # 还有很长的 CPU 合成空窗, 故用"宽限期内派发"近似"可能仍在合成/播放"。
                    # 修复历史 bug: 旧实现按 state=='dispatched' 累计, 而状态永不变回,
                    # 导致首轮之后 active 恒>0 → 导演永久 busy → 循环规则失效/异常。
                    active = sum(1 for j in recent
                                 if j['state'] == 'dispatched'
                                 and now_ts - j.get('dispatched_at', 0) < grace)
                    self._json({
                        'profile': orch.cfg.get('_profile_name'),
                        'phase': orch.phase,
                        'pending': orch.textq.qsize(),
                        'active': active,
                        'last_dispatch_age': (round(now_ts - orch._last_send, 1)
                                              if orch._last_send else None),
                        'speaking': orch._session_speaking(),   # LiveTalking 实时说话状态
                        'vendor_sessions': orch._vendor_sessions(),
                        'degraded': orch._degraded,
                        'session': orch.link.status() if orch.link else None,
                        'interrupt_epoch': orch._interrupt_epoch,
                        'jobs': {j['job_id']: {'state': j['state'],
                                               'error': j.get('error', ''),
                                               'len': len(j['text'])}
                                 for j in recent},
                    })
                elif self.path.startswith('/job/'):
                    # 查询单个任务状态: GET /job/<job_id>
                    jid = self.path[len('/job/'):]
                    with orch._job_lock:
                        job = orch._jobs.get(jid)
                    if job:
                        self._json({'ok': True, 'job_id': jid, 'state': job['state'],
                                    'error': job.get('error', ''),
                                    'created_at': job.get('created_at')})
                    else:
                        self._json({'ok': False, 'msg': 'job not found'}, 404)
                elif self.path == '/isspeaking':
                    # 代理 vendor /is_speaking（带真实会话）, 供导演/控制台判断播完
                    try:
                        sid = orch._session_or_fail()
                    except RuntimeError as e:
                        return self._json({'ok': False, 'msg': str(e)}, 503)
                    ok, detail = orch._lt_post('/is_speaking', {'sessionid': sid})
                    if ok and isinstance(detail, dict):
                        self._json({'ok': True, 'speaking': bool(detail.get('data'))})
                    else:
                        self._json({'ok': False, 'msg': str(detail)}, 502)
                else:
                    self._json({'msg': 'not found'}, 404)

            def do_POST(self):
                if self.path == '/say':
                    body = self._body()
                    text = body.get('text')
                    if not isinstance(text, str) or not text.strip():
                        self._json({'ok': False, 'code': 'bad_input',
                                    'msg': 'text required'}, 400)
                    else:
                        self._json(orch.say(text))
                elif self.path == '/chat':
                    body = self._body()
                    text = body.get('text')
                    if not isinstance(text, str) or not text.strip():
                        self._json({'ok': False, 'code': 'bad_input',
                                    'msg': 'text required'}, 400)
                    else:
                        self._json(orch.chat(text))
                elif self.path == '/interrupt':
                    self._json(orch.interrupt())
                else:
                    self._json({'msg': 'not found'}, 404)

            def log_message(self, fmt, *args):
                log.debug('http %s', fmt % args)

        return Handler

    # ── 主循环 ──────────────────────────────────────────────
    def run(self):
        self.preflight()
        self.start_all()
        self.accepting.set()
        try:
            self.ensure_session()
        except SessionLinkError as e:
            # 无会话也可服务（浏览器手动开预览后自动重绑）; 但播报会明确失败
            log.warning('初始会话未建立: %s（会话建立前 /say 返回失败, 不静默）', e)
            self.phase = 'livetalking'
            threading.Thread(target=self._session_retry_loop, daemon=True).start()

        self.watchdog = Watchdog(self.cfg, self.procs,
                                 on_degrade=self.degrade,
                                 on_restart_lip=self.restart_proc_lip,
                                 on_restart_tts=self.restart_proc_tts,
                                 repo_root=common_config.resolve_path('', '.'))
        threading.Thread(target=self.watchdog.run_forever,
                         args=(self.stop_flag,), daemon=True).start()
        threading.Thread(target=self._pump_text, daemon=True).start()

        httpd = ThreadingHTTPServer((self.host, self.port), self._make_handler())
        log.info('orchestrator 监听 %s:%s  phase=%s run_id=%s',
                 self.host, self.port, self.phase, RUN_ID)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self.stop_all()
            log.info('orchestrator 退出')

    def _session_retry_loop(self):
        """常驻会话守护（原来绑上就 return, 页面一断就永久失联 — 2026-09-11 实测修复）。

        页面刷新 / 浏览器重连 / LiveTalking 重启都会换 sessionid, 所以:
          - 已绑定: 定期 refresh 校验, 会话没了就回到未绑定状态;
          - 未绑定: 每 10s 试一次（短超时, 不卡住守护线程）, 直到绑上。
        """
        fails = 0
        while not self.stop_flag.is_set():
            link = self.link
            if link and link.connected:
                try:
                    link.refresh()
                except Exception:
                    pass
                if not link.connected:
                    log.warning('外部会话已消失, 等待页面重建后自动重绑')
                time.sleep(3)
                continue
            try:
                self.ensure_session(rebind=True, timeout_s=10)
                log.info('会话已重绑 sessionid=%s', self.link.sessionid)
                fails = 0
            except Exception as e:
                fails += 1
                if fails == 1 or fails % 12 == 0:      # 约每 2 分钟提示一次, 不刷屏
                    log.info('尚无外部会话可绑（%s）— 请在页面点「开始连接」', e)
                time.sleep(10)


def main():
    ap = argparse.ArgumentParser(description='digital-human-live 编排器')
    ap.add_argument('--profile', default=None, help='档位名, 缺省取 configs/default.yaml')
    ap.add_argument('--log-level', default='INFO')
    args = ap.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format='%(asctime)s %(name)s %(levelname)s %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)])

    cfg = common_config.load_profile(args.profile)
    log.info('profile=%s (%s) run_id=%s', cfg['_profile_name'], cfg['_profile_file'], RUN_ID)
    try:
        Orchestrator(cfg).run()
    except PreflightError as e:
        log.error('预检失败: %s', e)
        sys.exit(2)


if __name__ == '__main__':
    main()
