# RAG 知识库升级验收记录（2026-09-13）

## 概述

把 knowledge.py 的关键词检索升级为向量检索 RAG 服务（apps/rag_service），
真实接入弹幕问答链路（knowledge.answer_for），保留「未命中不编造」原则、
valid_until/allow_speak 语义与 qa_log 记录，控制台 decision 取值不变。

## 架构

```
弹幕/问答 → knowledge.answer_for
              ├─ RAG 服务 :8021 (POST /v1/ask, 超时2.5s)   ← bge-small-zh 向量召回
              │    └─ 不可达/兜底 → 自动回退 ↓              ← 可选 cross-encoder 精排(默认关)
              └─ 关键词检索(原逻辑) → 兜底话术
后台: 60s 轮询 knowledge 表变更自动重建 FAISS 索引; 索引落盘 data/rag_index/
```

- 独立进程、独立 venv（.venv-rag）、纯 CPU（不占直播 CUDA 预算）
- 全部参数在 configs/rag.yaml；模型缓存加载离线启动（HF_HUB_OFFLINE=1）
- 数据源与 knowledge.py 同一张 SQLite knowledge 表，零数据迁移

## 验收结果（scripts/rag_eval.py，报告 docs/rag_eval_20260913_151456.md）

| 指标 | 结果 | 通过线 |
|---|---|---|
| 题内命中率（38 条口语化/换述问法） | **92.1%** (35/38) | ≥80% |
| 题外兜底正确率（6 条题外话不编造） | **100%** (6/6) | ≥90% |
| 检索延迟 p50 / p95 / max | 17ms / 31ms / 33ms | — |

**精排消融实验**（docs/rag_eval_20260913_151224.md）：bge-reranker-base 在
20 条短 FAQ 场景 top1 命中率 86.8% < 纯向量 92.1%，且题外话 6/6 全部误命中
（sigmoid 分 0.50~0.72 与正确命中区间重叠，无分离度）→ 默认关闭精排，
保留开关与代码，供条目规模上千后复测。

## 集成验证（亲眼实测）

1. `knowledge.answer_for('课程多少钱')` → standard hit=1（RAG 路径，qa_log 落库正确）
2. `knowledge.answer_for('今天比特币什么行情')` → fallback（不编造）
3. **宕机演练**：停止 RAG 服务后 answer_for 打告警日志并回退关键词检索，
   '上课时间' 仍正确命中（hit=2），主链路零阻塞

## 已知限制

- 种子条目 18 条（source=seed:rag_uplift）为演示口径，上线前需业务复核
- 剩余 3 条未命中：2 条保守兜底（口语距离过远，方向安全）、
  1 条误命中（"现在报名什么价格"→购买方式条目，语义相近答非所问）
- 精排在条目数上百、长文本场景可能有增益，需重跑评测后再开

## 运行命令

```powershell
# 一次性: 下载模型（HF_HUB_DISABLE_XET=1 走 hf-mirror 普通HTTP, Xet后端401）
powershell -ExecutionPolicy Bypass -File scripts\rag_download_models.ps1
# 种子条目（幂等, 可跳过）
.venv-rag\Scripts\python.exe scripts\rag_seed.py
# 启动服务（:8021）
powershell -ExecutionPolicy Bypass -File scripts\rag_start.ps1
# 评测（--fast-only 关精排模式; --rerank-only 精排模式）
.venv-rag\Scripts\python.exe scripts\rag_eval.py
```
