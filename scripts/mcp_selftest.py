# -*- coding: utf-8 -*-
"""mcp_selftest — 用真实 stdio 客户端自测 MCP server。

验证链路: initialize → list_tools → ask_kb(真实 RAG 检索)
        → get_live_status(编排器宕机时优雅报错)。
通过线: 4 工具可见 且 ask_kb 返回命中 且 status 返回可读字符串。
用法: .venv-mcp/Scripts/python.exe scripts/mcp_selftest.py
"""
import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


async def main():
    params = StdioServerParameters(
        command=os.path.join(ROOT, '.venv-mcp', 'Scripts', 'python.exe'),
        args=['-m', 'apps.mcp_server.main'],
        cwd=ROOT,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            info = await s.initialize()
            print('连接成功:', info.server_info.name)
            tools = await s.list_tools()
            print('工具列表:', sorted(t.name for t in tools.tools))

            r1 = await s.call_tool('ask_kb', {'question': '课程多少钱'})
            ans = r1.content[0].text
            print('ask_kb →', ans)

            r2 = await s.call_tool('get_live_status', {})
            st = r2.content[0].text
            print('get_live_status →', st[:100] + ('...' if len(st) > 100 else ''))

            r3 = await s.call_tool('say_to_live', {'text': '自测文本'})
            print('say_to_live →', r3.content[0].text[:80])

            kb_ok = ans.startswith('[命中]')
            tools_ok = len(tools.tools) == 4
            st_ok = len(st) > 0
            ok = kb_ok and tools_ok and st_ok
            print('SELFTEST:', 'PASS' if ok else 'FAIL')
    sys.exit(0 if ok else 1)


asyncio.run(main())
