# -*- coding: utf-8 -*-
"""indexer — 知识条目向量化与 FAISS 索引管理。

数据源: configs/rag.yaml 指定的 SQLite knowledge 表
       (与 apps/knowledge.py 同一张表, 只取 enabled=1 且 allow_speak=1)。
持久化: index.dir 下 entries.json(条目元数据+向量文本) + index.faiss。
增量:   条目 count/max(version)/max(updated_at) 变化即整体重建
       (直播知识库条目量级为几十条, 重建毫秒级, 无需向量级增量)。
"""
import json
import logging
import os
import sqlite3
import time

import numpy as np

log = logging.getLogger('rag.indexer')


def load_config(path: str) -> dict:
    import yaml
    with open(path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


class KnowledgeIndex:
    """knowledge 表的向量索引。线程安全: rebuild/search 加锁。"""

    def __init__(self, cfg: dict, embedder):
        self.cfg = cfg
        self.embedder = embedder            # SentenceTransformer 实例
        self._lock_ready = False
        self.index = None
        self.entries = []                   # [{id,title,body,version,valid_until,text}]
        self.fingerprint = None
        os.makedirs(cfg['rag']['index']['dir'], exist_ok=True)

    # ---------- 数据源 ----------
    def _fetch_entries(self):
        db = self.cfg['rag']['knowledge_db']
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                'SELECT id,title,body,category,keywords,version,valid_until,'
                'updated_at FROM knowledge WHERE enabled=1 AND allow_speak=1 '
                'ORDER BY id'
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    @staticmethod
    def _entry_text(e: dict) -> str:
        """向量化文本: 标题+关键词+正文(关键词进文本以接住口语化问法)。"""
        kws = str(e.get('keywords') or '').replace(',', '、')
        parts = [e['title']]
        if kws:
            parts.append(f'关键词: {kws}')
        parts.append(e['body'])
        return '。'.join(p for p in parts if p)

    def _fingerprint_of(self, rows) -> str:
        if not rows:
            return 'empty'
        return f"{len(rows)}|{max(r['version'] for r in rows)}|" \
               f"{max(r['updated_at'] or 0 for r in rows)}"

    # ---------- 构建/加载 ----------
    def build(self) -> int:
        rows = self._fetch_entries()
        fp = self._fingerprint_of(rows)
        if fp == self.fingerprint and self.index is not None:
            return len(self.entries)
        if not rows:
            log.warning('knowledge 表无可用条目, 索引置空')
            self.index, self.entries, self.fingerprint = None, [], fp
            return 0
        texts = [self._entry_text(e) for e in rows]
        emb_cfg = self.cfg['rag']['embedding']
        vecs = self.embedder.encode(
            texts, batch_size=emb_cfg['batch_size'],
            normalize_embeddings=True, show_progress_bar=False)
        import faiss
        index = faiss.IndexFlatIP(vecs.shape[1])
        index.add(np.asarray(vecs, dtype='float32'))
        self.index, self.entries, self.fingerprint = index, rows, fp
        self._persist(index, rows)
        log.info('索引重建完成: %d 条', len(rows))
        return len(rows)

    def _persist(self, index, rows):
        d = self.cfg['rag']['index']['dir']
        try:
            import faiss
            faiss.write_index(index, os.path.join(d, 'index.faiss'))
            meta = [{'id': e['id'], 'title': e['title'], 'version': e['version'],
                     'valid_until': e['valid_until']} for e in rows]
            with open(os.path.join(d, 'entries.json'), 'w', encoding='utf-8') as f:
                json.dump(meta, f, ensure_ascii=False, indent=1)
        except OSError as exc:
            log.warning('索引持久化失败(仅影响冷启动): %s', exc)

    def load_or_build(self):
        """启动时优先加载落盘索引, 缺失/损坏/条目不符则重建。"""
        d = self.cfg['rag']['index']['dir']
        idx_path = os.path.join(d, 'index.faiss')
        meta_path = os.path.join(d, 'entries.json')
        if os.path.exists(idx_path) and os.path.exists(meta_path):
            try:
                import faiss
                index = faiss.read_index(idx_path)
                with open(meta_path, 'r', encoding='utf-8') as f:
                    meta = json.load(f)
                rows = self._fetch_entries()
                fp = self._fingerprint_of(rows)
                same = (len(meta) == len(rows) and all(
                    m['id'] == r['id'] and m['version'] == r['version']
                    for m, r in zip(meta, rows)))
                if same and fp != 'empty':
                    self.index = index
                    self.entries = [{**m, 'body': r['body']}
                                    for m, r in zip(meta, rows)]
                    self.fingerprint = fp
                    log.info('已加载落盘索引: %d 条', len(rows))
                    return
            except (RuntimeError, OSError, ValueError,
                    json.JSONDecodeError) as exc:
                log.warning('落盘索引不可用, 转重建: %s', exc)
        self.build()

    # ---------- 查询 ----------
    def search(self, question: str, top_k: int):
        """返回 [{id,title,body,version,valid_until,score}], 余弦降序。"""
        if self.index is None or not self.entries:
            return []
        emb_cfg = self.cfg['rag']['embedding']
        q = self.embedder.encode(
            [question], batch_size=emb_cfg['batch_size'],
            normalize_embeddings=True, show_progress_bar=False)
        scores, idxs = self.index.search(np.asarray(q, dtype='float32'), top_k)
        now = time.time()
        out = []
        for s, i in zip(scores[0], idxs[0]):
            if i < 0 or i >= len(self.entries):
                continue
            e = self.entries[i]
            vu = e.get('valid_until') or e.get('_valid_until') or 0
            if vu and vu < now:        # 时效过期不答(与knowledge.py一致)
                continue
            out.append({'id': e['id'], 'title': e['title'], 'body': e['body'],
                        'version': e['version'], 'valid_until': vu,
                        'score': float(s)})
        return out

    def stale(self) -> bool:
        """knowledge 表是否有变更(供同步线程轮询)。"""
        try:
            rows = self._fetch_entries()
        except sqlite3.Error as exc:
            log.warning('读取 knowledge 失败: %s', exc)
            return False
        return self._fingerprint_of(rows) != self.fingerprint
