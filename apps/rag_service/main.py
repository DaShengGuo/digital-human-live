# -*- coding: utf-8 -*-
"""main — RAG 知识库服务入口 (FastAPI)。

启动: .venv-rag/Scripts/python.exe -m apps.rag_service.main --config configs/rag.yaml
接口:
  GET  /healthz            健康与索引状态
  POST /v1/ask             {question, rerank?=true|false(临时覆盖配置)}
                            → {answer, decision, hit_id, hit_version, score,
                               rerank_score, latency_ms}
  POST /v1/index/rebuild   强制重建索引
后台线程按 sync_interval_sec 轮询 knowledge 表变更, 有变更自动重建。
模型名/阈值/端口等全部来自 configs/rag.yaml。
"""
import argparse
import logging
import threading
import time

import numpy as np
from fastapi import FastAPI
from pydantic import BaseModel

from apps.rag_service.indexer import KnowledgeIndex, load_config

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(name)s %(levelname)s %(message)s')
log = logging.getLogger('rag.main')

CFG = None
INDEX = None
RERANKER = None
LOCK = threading.RLock()


class AskBody(BaseModel):
    question: str
    rerank: bool | None = None     # 不传=按配置; 传了=本次请求临时覆盖(评测用)


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


def ask(question: str, rerank_override: bool | None = None) -> dict:
    t0 = time.perf_counter()
    rag_cfg = CFG['rag']
    ret_cfg = rag_cfg['retrieval']
    with LOCK:
        cands = INDEX.search(question, ret_cfg['top_k'])
    use_rerank = (rerank_override if rerank_override is not None
                  else rag_cfg['rerank']['enabled'])
    best, rerank_score = None, None
    if cands:
        if use_rerank and RERANKER is not None:
            top = cands[:rag_cfg['rerank']['top_n']]
            with LOCK:
                logits = RERANKER.predict(
                    [(question, c['body']) for c in top],
                    batch_size=len(top), show_progress_bar=False)
            scores = [float(_sigmoid(x)) for x in np.asarray(logits).ravel()]
            for c, s in zip(top, scores):
                c['rerank_score'] = s
            best = max(top, key=lambda c: c['rerank_score'])
            rerank_score = best['rerank_score']
            hit = rerank_score >= ret_cfg['rerank_threshold']
        else:
            best = cands[0]
            hit = best['score'] >= ret_cfg['threshold']
        if not hit:
            best = None
    latency_ms = round((time.perf_counter() - t0) * 1000, 1)
    if best is not None:
        return {'answer': best['body'][:ret_cfg['answer_max_chars']],
                'decision': 'standard', 'hit_id': best['id'],
                'hit_version': best['version'], 'score': round(best['score'], 4),
                'rerank_score': (round(rerank_score, 4)
                                 if rerank_score is not None else None),
                'latency_ms': latency_ms}
    return {'answer': rag_cfg['fallback_answer'], 'decision': 'fallback',
            'hit_id': None, 'hit_version': None, 'score': None,
            'rerank_score': None, 'latency_ms': latency_ms}


app = FastAPI(title='dhlive-rag')


@app.get('/healthz')
def healthz():
    return {'status': 'ok', 'entries': len(INDEX.entries),
            'index_ready': INDEX.index is not None,
            'rerank_enabled': RERANKER is not None,
            'fingerprint': INDEX.fingerprint}


@app.post('/v1/ask')
def v1_ask(body: AskBody):
    q = (body.question or '').strip()
    if not q:
        return {'error': 'question is empty'}
    return ask(q, body.rerank)


@app.post('/v1/index/rebuild')
def v1_rebuild():
    with LOCK:
        n = INDEX.build()
    return {'rebuilt': True, 'entries': n}


def _sync_loop():
    interval = CFG['rag']['index']['sync_interval_sec']
    while True:
        time.sleep(interval)
        try:
            if INDEX.stale():
                log.info('knowledge 表有变更, 自动重建索引')
                with LOCK:
                    INDEX.build()
        except Exception as exc:            # 后台线程不允许静默死亡
            log.warning('索引同步异常: %s', exc)


def main():
    global CFG, INDEX, RERANKER
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='configs/rag.yaml')
    args = parser.parse_args()
    CFG = load_config(args.config)
    rag_cfg = CFG['rag']

    from sentence_transformers import CrossEncoder, SentenceTransformer
    emb = rag_cfg['embedding']
    log.info('加载 embedding 模型: %s (%s)', emb['model'], emb['device'])
    embedder = SentenceTransformer(emb['model'], device=emb['device'])
    embedder.max_seq_length = emb['max_length']   # 新版ST: encode不再收max_length
    if rag_cfg['rerank']['enabled']:
        rr = rag_cfg['rerank']
        log.info('加载精排模型: %s (%s)', rr['model'], rr['device'])
        RERANKER = CrossEncoder(rr['model'], device=rr['device'],
                                max_length=rr['max_length'])
    INDEX = KnowledgeIndex(CFG, embedder)
    INDEX.load_or_build()

    threading.Thread(target=_sync_loop, daemon=True).start()

    import uvicorn
    svc = rag_cfg['service']
    uvicorn.run(app, host=svc.get('bind', svc['host']), port=svc['port'],
                log_level='warning')


if __name__ == '__main__':
    main()
