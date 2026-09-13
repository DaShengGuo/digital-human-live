# -*- coding: utf-8 -*-
"""scheduler — 直播计划定时执行（阶段F）。

时区: Asia/Shanghai（固定 UTC+8, 本机即东八区, 用本地时间即可, 不引入 zoneinfo 依赖）。
规则: once | daily | weekly:d, d 0=周一..6=周日
原则:
- 每计划每次触发 → 唯一 session_id（run_id 复用）, 防重复开播
- 用户手动结束的场次不被定时器重启
- 程序重启后: 先查 sessions 是否有 state=live 的未完成场次, 不自动重开
- 到点先预检(平台/资产/模型), 失败转 needs_attention, 不盲目开播
"""
import threading
import time
import datetime
import logging

from apps import storage

log = logging.getLogger('scheduler')


def _parse_repeat(rule: str):
    if rule.startswith('weekly:'):
        days = [int(x) for x in rule.split(':', 1)[1].split(',') if x.strip()]
        return 'weekly', days
    return rule, None


def next_fire(schedule: dict, now: datetime.datetime):
    """返回下一个触发时刻（datetime）, 已过窗口返回 None。"""
    kind, days = _parse_repeat(schedule['repeat_rule'])
    st = schedule['start_time']
    # once 可带完整日期 'YYYY-MM-DD HH:MM'; 其余仅 'HH:MM'
    if len(st) > 5:
        hh, mm = [int(x) for x in st.split()[1].split(':')]
    else:
        hh, mm = [int(x) for x in st.split(':')]
    if kind == 'once':
        # start_time 里可带日期(yyyy-mm-dd HH:MM), 否则今天
        d = now.date()
        if len(st) > 5:
            d = datetime.date.fromisoformat(st.split()[0])
        fire = datetime.datetime(d.year, d.month, d.day, hh, mm)
        return fire if now < fire + datetime.timedelta(minutes=schedule['duration_min']) else None
    if kind == 'daily':
        fire = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    elif kind == 'weekly':
        for off in range(8):
            cand = now + datetime.timedelta(days=off)
            if cand.weekday() in days:
                fire = cand.replace(hour=hh, minute=mm, second=0, microsecond=0)
                break
        else:
            return None
    else:
        return None
    # 窗口: fire ~ fire+duration; 已过今日 fire 则顺延明天
    if fire < now - datetime.timedelta(minutes=schedule['duration_min']):
        fire = fire + datetime.timedelta(days=7 if kind == 'weekly' else 1)
    return fire


class LiveScheduler(threading.Thread):
    """轻量调度线程: 到点触发一次开播流程。首版: 单并发。

    start_fn(schedule) → 执行开播（预检+启动+置 live）;
    end_fn(session_id) → 收尾下播。由控制台注入编排器/平台实现。
    """

    def __init__(self, start_fn, end_fn, poll_s=15, tz_offset_h=8):
        super().__init__(daemon=True, name='LiveScheduler')
        self.start_fn = start_fn
        self.end_fn = end_fn
        self.poll_s = poll_s
        self.tz_offset_h = tz_offset_h     # 必须保存（曾漏赋值致调度线程每轮 AttributeError）
        self.stop_flag = threading.Event()
        self._fired = {}     # schedule_id → 已触发日期字符串（防同日重复）
        self.active_session = None

    def now(self):
        return datetime.datetime.utcnow() + datetime.timedelta(hours=self.tz_offset_h)

    # ── 阶段F: 保存前冲突检测（首版单直播间, 窗口重叠即冲突）──
    def expand_windows(self, sc, now=None, days=8):
        """把计划展开为未来 N 天的 [(start_dt, end_dt), ...] 触发窗口。"""
        now = now or self.now()
        kind, wdays = _parse_repeat(sc['repeat_rule'])
        hh, mm = [int(x) for x in sc['start_time'][-5:].split(':')]
        dur = int(sc['duration_min'])
        out = []
        if kind == 'once':
            d = now.date()
            if len(sc['start_time']) > 5:
                d = datetime.date.fromisoformat(sc['start_time'].split()[0])
            fire = datetime.datetime(d.year, d.month, d.day, hh, mm)
            out.append((fire, fire + datetime.timedelta(minutes=dur)))
        elif kind == 'daily':
            for off in range(days):
                day = (now + datetime.timedelta(days=off)).date()
                fire = datetime.datetime(day.year, day.month, day.day, hh, mm)
                out.append((fire, fire + datetime.timedelta(minutes=dur)))
        elif kind == 'weekly' and wdays:
            for off in range(days):
                day = (now + datetime.timedelta(days=off)).date()
                if day.weekday() in wdays:
                    fire = datetime.datetime(day.year, day.month, day.day, hh, mm)
                    out.append((fire, fire + datetime.timedelta(minutes=dur)))
        return out

    def find_conflict(self, new_sc, now=None):
        """新计划与任一已启用计划（排除自身 id）窗口重叠 → 返回 (id, name)。"""
        now = now or self.now()
        for other in storage.list_schedules(enabled_only=True):
            if other['id'] == new_sc.get('id'):
                continue
            for a0, a1 in self.expand_windows(new_sc, now):
                for b0, b1 in self.expand_windows(other, now):
                    if a0 < b1 and b0 < a1:
                        return other['id'], other['name']
        return None

    def manual_end_active(self, reason='人工结束'):
        """人工结束当前场次: 置 manual_end, 后续自动恢复/定时器不得重启该场。"""
        if self.active_session:
            sid = self.active_session
            storage.session_state(sid, 'manual_end', reason)
            storage.log_event('info', f'人工结束场次 run={sid}', sid)
            self.active_session = None
            try:
                self.end_fn(sid)
            except Exception as e:
                storage.log_event('fault', f'人工结束收尾异常: {e}', sid)
            return sid
        # 无活动场次: 结束可能处于 needs_attention 的最近场次
        rows = storage.query("SELECT id FROM sessions WHERE state='needs_attention' "
                             'ORDER BY rowid DESC LIMIT 1')
        if rows:
            storage.session_state(rows[0]['id'], 'manual_end', reason)
            return rows[0]['id']
        return None

    def run(self):
        log.info('调度线程启动 (Asia/Shanghai)')
        self._recover_on_start()
        while not self.stop_flag.is_set():
            try:
                self._tick()
            except Exception:
                log.exception('调度轮次异常')
            time.sleep(self.poll_s)

    def _recover_on_start(self):
        """程序重启恢复: 已有未结束场次不自动重开; once 计划已过触发点标记已发。"""
        now = self.now()
        open_rows = storage.query(
            "SELECT id FROM sessions WHERE state IN "
            "('scheduled','preflight','ready','starting','live','stopping') "
            'ORDER BY rowid DESC LIMIT 1')
        if open_rows:
            self.active_session = open_rows[0]['id']
            storage.log_event('info', f'重启恢复: 沿用未完成场次 {open_rows[0]["id"]}, 不自动重开')
        for sc in storage.list_schedules(enabled_only=True):
            # once 计划已过触发点（无论是否还在时长窗口内）→ 标记已发, 重启不重开
            if sc['repeat_rule'] == 'once':
                st = sc['start_time']
                try:
                    d = datetime.date.fromisoformat(st.split()[0]) if len(st) > 5 else now.date()
                    hh, mm = [int(x) for x in st.split()[1].split(':')] if len(st) > 5 \
                        else [int(x) for x in st.split(':')]
                    fire = datetime.datetime(d.year, d.month, d.day, hh, mm)
                    if fire <= now:
                        self._fired[str(sc['id'])] = str(fire.date())
                except (ValueError, IndexError):
                    pass
                continue
            fire = next_fire(sc, now)
            if fire is not None and fire < now:
                self._fired[str(sc['id'])] = str(fire.date())

    def _tick(self):
        now = self.now()
        if self.active_session:
            s = storage.get_session(self.active_session)
            if s and s['state'] in ('manual_end', 'ended', 'finished'):
                self.active_session = None
                return
            # 到点结束
            row = storage.query('SELECT sc.duration_min FROM sessions ss '
                                'JOIN schedules sc ON ss.schedule_id=sc.id WHERE ss.id=?',
                                (self.active_session,))
            start = s.get('started_at') or 0
            if row and now.timestamp() > start + row[0]['duration_min'] * 60:
                self._stop_current('到达结束时间')
            return
        for sc in storage.list_schedules(enabled_only=True):
            fire = next_fire(sc, now)
            if fire is None:
                continue
            key = str(sc['id'])
            fired_date = self._fired.get(key)
            in_window = fire <= now < fire + datetime.timedelta(minutes=sc['duration_min'])
            # 错过窗口默认跳过: fire 已早于 now 超过 5 分钟(迟到窗口) → 丢弃本场
            if now > fire + datetime.timedelta(minutes=5):
                if fired_date != str(fire.date()) + str(fire.time()):
                    # 从未在该计划触发过且已迟到: 记录跳过一次
                    storage.log_event('info', f'计划{sc["id"]} 错过触发(迟到), 跳过本场')
                    self._fired[key] = str(fire.date()) + str(fire.time())
                continue
            if in_window and fired_date != str(fire.date()):
                self._launch(sc, fire)
                self._fired[key] = str(fire.date())

    def _launch(self, sc, fire):
        run_id = f"{fire:%Y%m%d-%H%M%S}-sch{sc['id']}"
        storage.log_event('info', f'计划[{sc["name"]}] 触发开播 run={run_id}', run_id)
        try:
            ok, msg = self.start_fn(sc, run_id)
        except Exception as e:
            ok, msg = False, str(e)
        if ok:
            storage.new_session(run_id, sc['id'])
            self.active_session = run_id
            storage.log_event('info', f'开播成功: {msg}', run_id)
        else:
            storage.new_session(run_id, sc['id'])
            storage.session_state(run_id, 'needs_attention', f'开播预检/启动失败: {msg}')
            storage.log_event('fault', f'开播失败: {msg}', run_id)

    def _stop_current(self, reason):
        if self.active_session:
            try:
                self.end_fn(self.active_session)
            except Exception as e:
                storage.log_event('fault', f'下播异常: {e}', self.active_session)
            storage.session_state(self.active_session, 'finished', reason)
            storage.log_event('info', f'下播: {reason}', self.active_session)
            self.active_session = None

    def stop(self):
        self.stop_flag.set()
