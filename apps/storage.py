# -*- coding: utf-8 -*-
"""storage — SQLite 持久化（阶段E）。

单机单库: data/dhlive.db。写入并发由 sqlite3 连接级锁+WAL 承担。
不每次启动清库; 版本迁移用 schema_version 表。
"""
import os
import sqlite3
import threading
import time

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(_REPO, 'data', 'dhlive.db')

_CONN = None
_LOCK = threading.Lock()

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    repeat_rule TEXT NOT NULL,          -- once | daily | weekly:1,3,5
    start_time TEXT NOT NULL,           -- HH:MM
    duration_min INTEGER NOT NULL,
    profile TEXT NOT NULL,
    script_file TEXT,
    knowledge_version TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,                -- run_id
    schedule_id INTEGER,
    state TEXT NOT NULL,                -- scheduled|live|finished|cancelled|failed|needs_attention
    started_at REAL,
    ended_at REAL,
    reason TEXT
);
CREATE TABLE IF NOT EXISTS scripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    position INTEGER NOT NULL DEFAULT 0,
    content TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS play_progress (
    session_id TEXT NOT NULL,
    script_id INTEGER NOT NULL,
    line_index INTEGER NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (session_id, script_id)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    session_id TEXT,
    kind TEXT NOT NULL,                 -- fault|recover|degrade|manual|info
    detail TEXT
);
"""


def _conn():
    global _CONN
    if _CONN is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _CONN = sqlite3.connect(DB_PATH, check_same_thread=False)
        _CONN.row_factory = sqlite3.Row
        _CONN.execute('PRAGMA journal_mode=WAL')
        _migrate()
    return _CONN


def _migrate():
    c = _CONN
    c.executescript(SCHEMA_V1)
    row = c.execute('SELECT version FROM schema_version').fetchone()
    if row is None:
        c.execute('INSERT INTO schema_version (version) VALUES (1)')
    c.commit()


def execute(sql: str, params: tuple = ()) -> int:
    """写操作, 返回 lastrowid。"""
    with _LOCK:
        cur = _conn().execute(sql, params)
        _conn().commit()
        return cur.lastrowid


def query(sql: str, params: tuple = ()) -> list:
    with _LOCK:
        return [dict(r) for r in _conn().execute(sql, params).fetchall()]


# ── 业务封装 ────────────────────────────────────────────────
def add_schedule(name, repeat_rule, start_time, duration_min, profile,
                 script_file=None, knowledge_version=None, enabled=1) -> int:
    now = time.time()
    return execute(
        'INSERT INTO schedules (name,enabled,repeat_rule,start_time,duration_min,'
        'profile,script_file,knowledge_version,created_at,updated_at) '
        'VALUES (?,?,?,?,?,?,?,?,?,?)',
        (name, enabled, repeat_rule, start_time, duration_min, profile,
         script_file, knowledge_version, now, now))


def list_schedules(enabled_only=False) -> list:
    sql = 'SELECT * FROM schedules'
    if enabled_only:
        sql += ' WHERE enabled=1'
    return query(sql + ' ORDER BY start_time')


def set_schedule_enabled(sid: int, enabled: bool):
    execute('UPDATE schedules SET enabled=?, updated_at=? WHERE id=?',
            (1 if enabled else 0, time.time(), sid))


def new_session(run_id: str, schedule_id=None) -> str:
    execute('INSERT INTO sessions (id, schedule_id, state, started_at) VALUES (?,?,?,?)',
            (run_id, schedule_id, 'live', time.time()))
    return run_id


def session_state(run_id: str, state: str, reason: str = ''):
    execute('UPDATE sessions SET state=?, ended_at=?, reason=? WHERE id=?',
            (state, time.time(), reason, run_id))


def get_session(run_id: str):
    rows = query('SELECT * FROM sessions WHERE id=?', (run_id,))
    return rows[0] if rows else None


def save_progress(run_id: str, script_id: int, line_index: int):
    execute('INSERT OR REPLACE INTO play_progress (session_id,script_id,line_index,updated_at) '
            'VALUES (?,?,?,?)', (run_id, script_id, line_index, time.time()))


def get_progress(run_id: str, script_id: int):
    rows = query('SELECT line_index FROM play_progress WHERE session_id=? AND script_id=?',
                 (run_id, script_id))
    return rows[0]['line_index'] if rows else 0


def add_script(name: str, content: str, position: int = 0) -> int:
    return execute('INSERT INTO scripts (name,enabled,position,content,version,updated_at) '
                   'VALUES (?,?,?,?,1,?)', (name, 1, position, content, time.time()))


def list_scripts(enabled_only=True) -> list:
    sql = 'SELECT * FROM scripts'
    if enabled_only:
        sql += ' WHERE enabled=1'
    return query(sql + ' ORDER BY position, id')


def log_event(kind: str, detail: str, session_id=None):
    execute('INSERT INTO events (ts, session_id, kind, detail) VALUES (?,?,?,?)',
            (time.time(), session_id, kind, detail))


def recent_events(limit=50, session_id=None) -> list:
    if session_id:
        return query('SELECT * FROM events WHERE session_id=? ORDER BY id DESC LIMIT ?',
                     (session_id, limit))
    return query('SELECT * FROM events ORDER BY id DESC LIMIT ?', (limit,))
