# -*- coding: utf-8 -*-
"""knowledge — 直播知识库（阶段G）。

轻量 SQLite 方案: 结构化 FAQ + 关键词检索, 无常驻模型/向量库。
条目: 标识/标题/正文/分类/来源/版本/有效期/是否允许口播。
规则:
- 检索顺序: 有效且允许口播的标准回答 → 未命中用配置兜底回应, 不编造
- 时效: valid_until 过期条目不参与回答
- 正文中的价格/规格等关键数值以条目为准（来源可追溯, 记录条目+版本）
"""
import os
import time
import logging

from apps import storage

log = logging.getLogger('knowledge')

_schema = '''
CREATE TABLE IF NOT EXISTS knowledge (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  category TEXT DEFAULT '',
  keywords TEXT DEFAULT '',          -- 逗号分隔, 检索用
  source TEXT DEFAULT '',            -- 来源说明
  version INTEGER DEFAULT 1,
  valid_until REAL DEFAULT 0,        -- 0=长期有效; 过期不答
  allow_speak INTEGER DEFAULT 1,     -- 0=仅内部参考不得口播
  enabled INTEGER DEFAULT 1,
  updated_at REAL
);
CREATE TABLE IF NOT EXISTS qa_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL, question TEXT, hit_id INTEGER, hit_version INTEGER,
  answer TEXT, decision TEXT         -- standard|fallback|ignored
);
'''

storage_schema_addon = _schema


def ensure_schema():
    conn = storage._conn()
    with storage._LOCK:
        conn.executescript(_schema)


def add_entry(title, body, category='', keywords='', source='',
              valid_until=0.0, allow_speak=True):
    ensure_schema()
    return storage.execute(
        'INSERT INTO knowledge(title,body,category,keywords,source,version,'
        'valid_until,allow_speak,enabled,updated_at) VALUES(?,?,?,?,?,'
        '1,?,?,1,?)',
        (title, body, category, keywords, source, valid_until,
         1 if allow_speak else 0, time.time()))


def update_entry(entry_id, **fields):
    """内容变化 → version+1（使旧缓存/旧引用失效可追溯）。"""
    ensure_schema()
    row = storage.query('SELECT version FROM knowledge WHERE id=?', (entry_id,))
    if not row:
        return False
    sets, vals = [], []
    for k in ('title', 'body', 'category', 'keywords', 'source',
              'valid_until', 'enabled'):
        if k in fields:
            sets.append(f'{k}=?'); vals.append(fields[k])
    if 'allow_speak' in fields:
        sets.append('allow_speak=?'); vals.append(1 if fields['allow_speak'] else 0)
    sets.append('version=?'); vals.append(row[0]['version'] + 1)
    sets.append('updated_at=?'); vals.append(time.time())
    storage.execute(f'UPDATE knowledge SET {",".join(sets)} WHERE id=?',
                    tuple(vals) + (entry_id,))
    return True


def list_entries(enabled_only=True):
    ensure_schema()
    sql = 'SELECT * FROM knowledge' + (' WHERE enabled=1' if enabled_only else '')
    return storage.query(sql)


def _norm(s: str) -> str:
    return ''.join(ch for ch in (s or '').lower() if ch.isalnum() or '一' <= ch <= '鿿')


def search(question: str):
    """返回 (entry|None, score)。规则: 关键词命中优先, 其次标题分词包含。"""
    ensure_schema()
    now = time.time()
    q = _norm(question)
    best, best_score = None, 0
    for e in storage.query('SELECT * FROM knowledge WHERE enabled=1'):
        if e['valid_until'] and e['valid_until'] < now:
            continue                       # 时效过期不答
        if not e['allow_speak']:
            continue                       # 不允许口播的不作回答
        score = 0
        for kw in str(e['keywords'] or '').split(','):
            kw = _norm(kw)
            if kw and kw in q:
                score += 3
        t = _norm(e['title'])
        if t and (t in q or q in t):
            score += 2
        if score > best_score:
            best, best_score = e, score
    return (best if best_score >= 3 else None), best_score


FALLBACK_ANSWER = '这个问题我记下了，稍后详细给大家讲，先继续今天的内容。'

_RAG_CFG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'configs', 'rag.yaml')


def _rag_answer(question: str):
    """RAG 向量检索(apps/rag_service)。返回 dict 或 None。

    规则: RAG 标准命中 → 直接使用; RAG 兜底/服务不可达/未启用 → None
    (调用方回退关键词检索, 主链路永不被 RAG 故障阻塞)。
    """
    try:
        import yaml
        with open(_RAG_CFG_PATH, 'r', encoding='utf-8') as f:
            cfg = yaml.safe_load(f).get('rag', {})
        if not cfg.get('enabled'):
            return None
        import requests
        svc = cfg['service']
        url = f"http://{svc['host']}:{svc['port']}/v1/ask"
        r = requests.post(url, json={'question': question},
                          timeout=cfg.get('client_timeout_sec', 2.5))
        data = r.json()
    except FileNotFoundError:
        return None                      # 未部署 RAG 时静默走老链路
    except Exception as exc:
        log.warning('RAG 服务不可达, 回退关键词检索: %s', exc)
        return None
    if data.get('decision') == 'standard':
        return data
    return None                          # 向量检索亦未命中 → 不编造


def answer_for(question: str, session_run_id=''):
    """生成口播回答。返回 dict(answer, decision, hit_id, hit_version)。
    检索顺序: RAG 向量召回+精排 → 关键词检索 → 固定兜底, 不编造。
    decision 取值保持 standard|fallback|ignored 不变(控制台兼容)。"""
    rag = _rag_answer(question)
    if rag is not None:
        ans, decision = rag['answer'], 'standard'
        hit_id, hit_version = rag['hit_id'], rag['hit_version']
    else:
        entry, score = search(question)
        if entry:
            ans = entry['body'][:200]
            decision = 'standard'
            hit_id, hit_version = entry['id'], entry['version']
        else:
            ans = FALLBACK_ANSWER
            decision = 'fallback'
            hit_id = hit_version = None
    ensure_schema()
    storage.execute(
        'INSERT INTO qa_log(ts,question,hit_id,hit_version,answer,decision) '
        'VALUES(?,?,?,?,?,?)',
        (time.time(), question[:200], hit_id,
         hit_version, ans[:200], decision))
    storage.log_event('qa', f'回答[{decision}] q={question[:30]} '
                            f'hit={hit_id if hit_id else "-"}', session_run_id)
    return {'answer': ans, 'decision': decision,
            'hit_id': hit_id, 'hit_version': hit_version}
