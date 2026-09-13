# -*- coding: utf-8 -*-
"""danmaku — 弹幕接入、筛选与调度管线（阶段G）。

事件源(三类, 状态严格区分):
- manual : 人工在控制台粘贴直播间看到的弹幕（真实内容, 人工通道）
- mock   : 开发模拟器（apps/danmaku_simulator.py, 永远标注 MOCK）
- real   : 自动接入（直播伴侣 UIA 实测: Chromium 渲染树不可读 → 不可用, 见 platform.py）

处理链: 接收→场次校验→去重→过滤→分类→是否回复→排队→知识库回答→口播派发→讲解自动恢复
频控: 单用户窗口限次 + 全场限次 + 相似问题合并 + 问题 TTL 过期不答
持久化: danmaku 表（重启后去重仍生效）。
"""
import time
import threading
import logging
import hashlib
import re

from apps import storage

log = logging.getLogger('danmaku')

SCHEMA = '''
CREATE TABLE IF NOT EXISTS danmaku (
  event_id TEXT PRIMARY KEY,
  ts REAL, source TEXT, room_id TEXT, run_id TEXT,
  user TEXT, text TEXT,
  decision TEXT DEFAULT 'pending',   -- pending|answered|ignored|expired|merged|failed|cooldown
  reason TEXT DEFAULT '',
  answer TEXT DEFAULT '',
  answered_at REAL DEFAULT 0
);
'''


def ensure_schema():
    conn = storage._conn()
    with storage._LOCK:
        conn.executescript(SCHEMA)


def _norm(s: str) -> str:
    s = (s or '').lower()
    return ''.join(ch for ch in s if ch.isalnum() or '一' <= ch <= '鿿')


def event_id_of(source: str, user: str, text: str, ts_bucket: int) -> str:
    return hashlib.sha256(f'{source}|{_norm(user)}|{_norm(text)}|{ts_bucket}'.encode()).hexdigest()[:32]


class DanmakuPipeline:
    """弹幕处理管线（单工作线程消费, 状态清晰）。"""

    def __init__(self, say_fn, room_id='', run_id='',
                 user_rate_n=2, user_rate_win_s=120,
                 global_rate_n=6, global_rate_win_s=180,
                 question_ttl_s=120, similar_win_s=60,
                 ignore_patterns=('加微信', 'QQ群', '转账', '骗子', '举报'),
                 poll_s=2.0):
        ensure_schema()
        self.say_fn = say_fn
        self.room_id = room_id
        self.run_id = run_id
        self.user_rate_n = user_rate_n
        self.user_rate_win = user_rate_win_s
        self.global_rate_n = global_rate_n
        self.global_rate_win = global_rate_win_s
        self.question_ttl = question_ttl_s
        self.similar_win = similar_win_s
        self.ignore_pat = [re.compile(re.escape(p)) for p in ignore_patterns]
        self.poll_s = poll_s
        self.stop_flag = threading.Event()
        self.stats = {'received': 0, 'answered': 0, 'ignored': 0,
                      'expired': 0, 'merged': 0, 'dup': 0, 'failed': 0}
        self._lock = threading.Lock()
        self._answer_times = []          # 全场回答时间窗
        self.takeover = threading.Event()   # 人工接管时不自动生成回答(入队保留)

    # ── 接收 ──────────────────────────────────────────────
    def ingest(self, source: str, user: str, text: str,
               event_ts: float = None) -> dict:
        """入口。返回 {ok, event_id, state}。

        source ∈ manual(人工粘贴) | ocr(自动读直播伴侣窗口, 阶段G) | mock(模拟) | real(平台接口, 未接入)。
        溯源要求: 库里的 source 字段就是给验收/复盘分辨"这条问答是不是真实观众发的"。
        """
        if source not in ('manual', 'ocr', 'mock', 'real'):
            return {'ok': False, 'msg': 'bad source'}
        text = (text or '').strip()
        if not text:
            return {'ok': False, 'msg': 'empty'}
        now = event_ts or time.time()
        eid = event_id_of(source, user, text, int(now // 5))   # 5s 桶去重
        with self._lock:
            self.stats['received'] += 1
        exists = storage.query('SELECT event_id FROM danmaku WHERE event_id=?', (eid,))
        if exists:
            with self._lock:
                self.stats['dup'] += 1
            return {'ok': True, 'event_id': eid, 'state': 'duplicate_ignored'}
        storage.execute(
            'INSERT INTO danmaku(event_id,ts,source,room_id,run_id,user,text) '
            'VALUES(?,?,?,?,?,?,?)',
            (eid, now, source, self.room_id, self.run_id,
             (user or '匿名')[:30], text[:300]))
        return {'ok': True, 'event_id': eid, 'state': 'pending'}

    def set_session(self, room_id, run_id):
        self.room_id = room_id
        self.run_id = run_id

    # ── 处理 ──────────────────────────────────────────────
    def _decide(self, row: dict):
        """单条弹幕决策 → (decision, reason, answer)。"""
        now = time.time()
        text, user, ts = row['text'], row['user'], row['ts']
        # 过期
        if now - ts > self.question_ttl:
            return 'expired', '超过问题有效期', ''
        # 非提问/敏感
        if any(p.search(text) for p in self.ignore_pat):
            return 'ignored', '不适合口播内容', ''
        looks_question = ('?' in text or '？' in text or
                          any(k in text for k in ('吗', '怎么', '如何', '什么', '多少', '能不能', '可否')))
        if not looks_question:
            return 'ignored', '非提问（问候/闲聊）', ''
        # 历史消息过滤: 早于管线启动的 mock 回放可标注
        # 相似合并: 窗口内相同归一化文本已有 answered → merged
        nq = _norm(text)
        sim = storage.query(
            'SELECT event_id,decision FROM danmaku '
            'WHERE decision=? AND ts>? ORDER BY rowid DESC LIMIT 5',
            ('answered', now - self.similar_win))
        for s in sim:
            sim_q = storage.query('SELECT text FROM danmaku WHERE event_id=?',
                                  (s['event_id'],))
            if sim_q and _norm(sim_q[0]['text']) == nq:
                return 'merged', '窗口内相同问题已回答', ''
        # 频控
        with self._lock:
            self._answer_times = [t for t in self._answer_times
                                  if now - t < self.global_rate_win]
            if len(self._answer_times) >= self.global_rate_n:
                return 'cooldown', '全场回答频率限制', ''
            ucnt = storage.query(
                'SELECT COUNT(*) c FROM danmaku WHERE user=? AND decision=? AND ts>?',
                (user, 'answered', now - self.user_rate_win))
            if ucnt and ucnt[0]['c'] >= self.user_rate_n:
                return 'cooldown', '单用户回答频率限制', ''
        # 知识库回答
        from apps import knowledge
        a = knowledge.answer_for(text, self.run_id)
        # 禁止编造: 兜底回答也要过一遍是否值得口播（fallback 是配置好的自然回应）
        return 'answered', a['decision'], a['answer']

    def _worker(self):
        while not self.stop_flag.is_set():
            try:
                self._tick()
            except Exception:
                log.exception('弹幕管线轮次异常')
            time.sleep(self.poll_s)

    def _tick(self):
        rows = storage.query(
            "SELECT * FROM danmaku WHERE decision='pending' ORDER BY rowid LIMIT 5")
        if not rows:
            return
        if self.takeover.is_set():
            return   # 人工接管: 不自动生成/口播回答, 但保留 pending
        for row in rows:
            decision, reason, answer = self._decide(row)
            if decision == 'answered' and answer:
                r = self.say_fn(answer)
                if not r.get('ok'):
                    decision, reason = 'failed', str(r.get('msg', 'say rejected'))[:80]
                else:
                    with self._lock:
                        self._answer_times.append(time.time())
            storage.execute(
                'UPDATE danmaku SET decision=?, reason=?, answer=?, answered_at=? '
                'WHERE event_id=?',
                (decision, reason[:100], answer[:300], time.time(), row['event_id']))
            with self._lock:
                bucket = {'answered': 'answered', 'ignored': 'ignored',
                          'expired': 'expired', 'merged': 'merged',
                          'cooldown': 'ignored', 'failed': 'failed'}.get(decision, 'ignored')
                self.stats[bucket] = self.stats.get(bucket, 0) + 1

    def start(self):
        threading.Thread(target=self._worker, daemon=True, name='DanmakuPipeline').start()

    def snapshot(self):
        ensure_schema()
        rows = storage.query(
            'SELECT event_id,ts,source,user,text,decision,reason FROM danmaku '
            'ORDER BY rowid DESC LIMIT 30')
        return {'stats': dict(self.stats), 'recent': rows}


def build_default_pipeline(say_fn):
    return DanmakuPipeline(say_fn)
