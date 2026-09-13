# 视频生产多 Agent 状态图（LangGraph 移植）验收记录（2026-09-13）

## 概述

把 ComfyUIVideoAgent 的五角色提示词架构固化为 LangGraph 状态图
（apps/video_graph），review 条件回环 prompt_agent 重写，迭代超限强制交付。
生成/评审节点可注入后端：`mock`（确定性，验证图结构）| `comfyui`（真实 API 闭环桩）。
图结构零改动即可切换后端。

## 图结构

```
START → manager(需求拆解) → prompt(提示词生成) → generate(出片) → review(评审)
                                        ↑                  │ pass → deliver → END
                                        └── reject+未超限 ←──┘ (条件回环, 注入 notes)
超限: 强制 deliver, abort_reason=max_iterations_reached
```

## 真实执行验证（scripts/video_graph_test.py，三场景全 PASS）

| 场景 | brief | 路径 | 断言 |
|---|---|---|---|
| A 首轮通过 | "手机开箱视频，要有产品特写" | manager→prompt→generate→review(pass)→deliver | delivered, iteration=1, verdict=pass |
| B 评审回环 | "一款保温杯的介绍视频" | 首轮 reject(0.55) → 回环重写(注入"缺少特写镜头"notes) → 二轮 pass(0.85) | delivered, iteration=2, verdict=pass |
| C 超限强制交付 | 同B + max_iterations=1 | 首轮 reject → 超限 → deliver | delivered, iteration=1, verdict=reject, abort_reason=max_iterations_reached |

场景 B 的日志可见完整的回环链路（第 2 轮提示词确实吸收了评审意见）。

## 与岗位要求的对应

- 多 Agent 协作/工作流编排: 五角色状态图 + 条件边路由
- 算法效果评估/实验设计: mock 评审的确定性评分设计使回环路径可复现验证
- 工程化: 参数全在 configs/video_graph.yaml; 生成后端可注入（mock 秒级验证图结构, 
  comfyui 后端实现提交/轮询/history 取片闭环, 需 ComfyUI 服务在线）

## 已知限制

- mock generate 产出占位 mp4（验证图结构用）; 真实出片走 backend=comfyui
- comfyui 评审桩（Qwen-VL 打分）留待真实服务在线时实现, 当前 raise NotImplementedError
- 评测脚本（ragas）需要 LLM API key（DASHSCOPE_API_KEY 未设置, 本地 27B GGUF CPU
  offload 不适合批量评测）, scripts/ragas_eval.py 留作 key 配置后的接入点
