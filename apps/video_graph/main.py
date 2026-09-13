# -*- coding: utf-8 -*-
"""video_graph — 视频生产流水线的 LangGraph 状态图移植。

把 ComfyUIVideoAgent 的五角色提示词架构(manager/prompt/generate/review/deliver)
固化为可执行的状态图: review 不达标条件回环 prompt_agent 重写,
迭代超限强制交付并标记。生成/评审节点可注入后端(mock | comfyui),
生成后端换真实 ComfyUI API、评审换 Qwen-VL 时图结构零改动。
"""
import json
import os
import time
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph


def _cfg() -> dict:
    import yaml
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with open(os.path.join(here, 'configs', 'video_graph.yaml'),
              encoding='utf-8') as f:
        return yaml.safe_load(f)['video_graph']


class GState(TypedDict):
    brief: str                    # 原始需求(一句话)
    plan: dict                    # manager 分解: 镜头/时长/风格
    prompt: str                   # prompt_agent 产出的生成提示词
    video: str                    # generate 产出路径
    score: float                  # review 评分
    verdict: str                  # pass | reject
    notes: str                    # review 修改意见(回环注入)
    iteration: int                # 已重写次数
    delivered: bool
    abort_reason: str
    log: Annotated[list[str], lambda a, b: (a or []) + (b or [])]  # 追加合并


def _log(s: dict, node: str, msg: str) -> dict:
    return {'log': [f'[{node}] {msg}']}


def make_graph(cfg: dict | None = None):
    cfg = cfg or _cfg()
    gc, rc = cfg['generate'], cfg['review']

    # ── 角色节点 ──────────────────────────────────────────
    def manager(state: GState) -> dict:
        """任务分解: 把一句话需求拆成镜头计划(真实系统接 LLM, 此处规则式保确定性)。"""
        brief = state['brief']
        shots = ['开场钩子', '主体演示', '行动号召'][: 3 if len(brief) > 20 else 2]
        if '特写' in brief:
            shots.append('产品特写')       # 需求关键词直通镜头计划
        plan = {
            'shots': shots,
            'duration_s': 30,
            'style': '真实感' if '真实' in brief else '创意',
        }
        return {**_log(state, 'manager', f'需求拆解: {plan["shots"]} {plan["duration_s"]}s'),
                'plan': plan, 'iteration': 0}

    def prompt_agent(state: GState) -> dict:
        """提示词生成: 首轮按计划写; 回环轮吸收 review notes 修正。"""
        it = state['iteration'] + 1
        p = state['plan']
        parts = [f"{s}镜头" for s in p['shots']]
        prompt = f"{p['style']}风格, {', '.join(parts)}, {p['duration_s']}秒"
        if state.get('notes'):
            prompt += f", 修正: {state['notes']}"
        return {**_log(state, 'prompt', f'第{it}轮提示词: {prompt[:60]}'),
                'prompt': prompt, 'iteration': it, 'verdict': ''}

    def generate(state: GState) -> dict:
        """生成节点: backend 可注入。mock 秒出假片; comfyui 走真实 API 闭环。"""
        if gc['backend'] == 'comfyui':
            return _generate_comfyui(state, gc)
        os.makedirs(gc['output_dir'], exist_ok=True)
        path = os.path.join(gc['output_dir'],
                            f"mock_it{state['iteration']}.mp4")
        with open(path, 'wb') as f:
            f.write(b'FAKE_MP4')           # 占位产物, 验证图结构用
        time.sleep(0.2)
        return {**_log(state, 'generate', f'mock 出片 {os.path.basename(path)}'),
                'video': path}

    def review(state: GState) -> dict:
        """评审节点: mock 用确定性评分验证图回环(含'特写'或第2轮即过);
        comfyui 后端换 Qwen-VL 打分, 图结构不变。"""
        if gc['backend'] == 'comfyui':
            # 真实后端: 提取首帧 → Qwen-VL 打分(见 docs/video_graph.md)
            raise NotImplementedError('comfyui 评审桩: 接 Qwen-VL 后实现')
        good = ('特写' in state['prompt']) or state['iteration'] >= 2
        score = 0.85 if good else 0.55
        verdict = 'pass' if score >= rc['min_score'] else 'reject'
        notes = '' if good else '缺少特写镜头, 主体细节不足'
        return {**_log(state, 'review', f'score={score} → {verdict}'),
                'score': score, 'verdict': verdict, 'notes': notes}

    def deliver(state: GState) -> dict:
        if state['verdict'] == 'pass':
            msg = f"交付 {state['video']} (第{state['iteration']}轮通过)"
            out = {}
        else:
            out = {'abort_reason': 'max_iterations_reached'}
            msg = f"迭代超限({state['iteration']}轮), 带问题交付"
        return {**_log(state, 'deliver', msg), 'delivered': True, **out}

    # ── 组图: review 条件路由 ────────────────────────────
    def after_review(state: GState) -> str:
        if state['verdict'] == 'pass':
            return 'deliver'
        if state['iteration'] >= cfg['max_iterations']:
            return 'deliver'               # 超限强制交付, abort_reason 记录
        return 'prompt'                    # 条件回环: 重写提示词

    g = StateGraph(GState)
    g.add_node('manager', manager)
    g.add_node('prompt', prompt_agent)
    g.add_node('generate', generate)
    g.add_node('review', review)
    g.add_node('deliver', deliver)
    g.add_edge(START, 'manager')
    g.add_edge('manager', 'prompt')
    g.add_edge('prompt', 'generate')
    g.add_edge('generate', 'review')
    g.add_conditional_edges('review', after_review,
                            {'prompt': 'prompt', 'deliver': 'deliver'})
    g.add_edge('deliver', END)
    return g.compile()

    # _generate_comfyui 在下方定义, 供 generate 闭包调用


def _generate_comfyui(state: GState, gc: dict) -> dict:
    """真实 ComfyUI API 闭环: 提交 workflow → 轮询 history → 取输出。
    服务离线时抛出可读错误(评测环境无 ComfyUI 常驻, 故默认 mock)。"""
    import requests
    try:
        wf = json.load(open(gc['comfyui_workflow'], encoding='utf-8'))
        r = requests.post(f"{gc['comfyui_api']}/prompt",
                          json={'prompt': wf}, timeout=10)
        pid = r.json()['prompt_id']
        deadline = time.time() + 3600
        while time.time() < deadline:
            h = requests.get(f"{gc['comfyui_api']}/history/{pid}", timeout=10).json()
            if pid in h and h[pid].get('outputs'):
                return {'video': json.dumps(h[pid]['outputs'])[:200]}
            time.sleep(gc['poll_interval_sec'])
        raise TimeoutError('ComfyUI 生成超时')
    except Exception as exc:
        raise RuntimeError(f'ComfyUI 后端不可用({exc}); 切回 backend=mock 验证图结构') from exc
