# -*- coding: utf-8 -*-
"""mcp_server — 把直播间服务封装为 MCP 工具（stdio 传输）。

让 Claude Desktop / Cursor 等 AI 客户端可直接操控数字人直播间:
  ask_kb          查询 RAG 知识库(弹幕问答同源)
  get_live_status 直播间状态(阶段/队列/说话中/GPU/降级)
  say_to_live     让数字人口播一段文本
  interrupt_live  打断当前口播
服务不可达时返回可读错误字符串, 不抛异常(不吓到客户端模型)。
所有服务地址/超时读 configs/mcp.yaml(DHLIVE_MCP_CONFIG 可覆盖)。
"""
import json
import os

import requests
from mcp.server.mcpserver import MCPServer   # mcp SDK 2.x (1.x 时为 FastMCP)

from apps.rag_service.indexer import load_config

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CFG = load_config(os.environ.get(
    'DHLIVE_MCP_CONFIG', os.path.join(_ROOT, 'configs', 'mcp.yaml')))['mcp']
SVC = CFG['services']

mcp = MCPServer(name=CFG['name'])


def _fmt(data) -> str:
    return json.dumps(data, ensure_ascii=False)


@mcp.tool()
def ask_kb(question: str) -> str:
    """查询直播间知识库(RAG向量检索), 返回标准答案或兜底话术。
    适合回答商品价格、上课时间、优惠活动等常见观众问题。"""
    try:
        r = requests.post(f"{SVC['rag']}/v1/ask",
                          json={'question': question},
                          timeout=CFG['timeout_sec'])
        d = r.json()
    except Exception as exc:
        return f'RAG服务不可达: {exc}'
    tag = '命中' if d.get('decision') == 'standard' else '未命中-走兜底'
    return f'[{tag}] {d.get("answer", "")}'


@mcp.tool()
def get_live_status() -> str:
    """获取数字人直播间当前状态: 阶段/待播队列/是否在说话/GPU显存温度/降级状态/会话。"""
    out = {}
    for key in ('healthz', 'status'):
        try:
            r = requests.get(f"{SVC['orchestrator']}/{key}",
                             timeout=CFG['timeout_sec'])
            out[key] = r.json()
        except Exception as exc:
            out[key] = f'编排器不可达(直播间未开机?): {exc}'
    return _fmt(out)


@mcp.tool()
def say_to_live(text: str) -> str:
    """让数字人直播间口播一段文本(进入编排器文本队列, 自动限流/合并/过期丢弃)。"""
    try:
        r = requests.post(f"{SVC['orchestrator']}/say", json={'text': text},
                          timeout=CFG['say_timeout_sec'])
        return _fmt(r.json())
    except Exception as exc:
        return f'编排器不可达(直播间未开机?): {exc}'


@mcp.tool()
def interrupt_live() -> str:
    """打断当前口播: 清空待播队列并立即停止数字人说话。"""
    try:
        r = requests.post(f"{SVC['orchestrator']}/interrupt",
                          timeout=CFG['timeout_sec'])
        return _fmt(r.json())
    except Exception as exc:
        return f'编排器不可达(直播间未开机?): {exc}'


if __name__ == '__main__':
    mcp.run(transport='stdio')
