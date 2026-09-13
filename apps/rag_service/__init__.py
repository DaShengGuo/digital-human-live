# -*- coding: utf-8 -*-
"""rag_service — 知识库向量检索升级（阶段RAG）。

替代 knowledge.py 的关键词检索: bge 向量召回 + 可选 cross-encoder 精排,
未命中走兜底话术, 沿用「不编造」原则与 valid_until/allow_speak 语义。
独立进程 (默认 :8021), 编排器 knowledge.answer_for 经 HTTP 调用,
服务不可达时自动回退关键词检索, 不影响直播主链路。
所有参数读 configs/rag.yaml。
"""
