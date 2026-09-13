# -*- coding: utf-8 -*-
"""video_graph_test — LangGraph 五角色状态图真实执行验证。

场景A: brief 含"特写" → 首轮评审通过, 1 轮交付
场景B: brief 不含 → 首轮拒绝 → 条件回环重写(注入 notes) → 第 2 轮通过
场景C: max_iterations=1 且首轮拒绝 → 超限强制交付并标记
通过线: 三场景断言全部成立。
用法: .venv-lg/Scripts/python.exe scripts/video_graph_test.py
"""
import sys

sys.path.insert(0, '.')

from apps.video_graph.main import make_graph  # noqa: E402

BASE = {'generate': {'backend': 'mock', 'output_dir': 'data/video_graph',
                     'comfyui_api': '', 'comfyui_workflow': '', 'poll_interval_sec': 5},
        'review': {'min_score': 0.7}}


def run(name, brief, expect_it, expect_abort=False):
    cfg = {**BASE, 'max_iterations': 1 if expect_abort else 3}
    final = make_graph(cfg).invoke({'brief': brief, 'log': []})
    ok = (final['delivered'] and final['iteration'] == expect_it
          and final['verdict'] == ('reject' if expect_abort else 'pass')
          and (final.get('abort_reason') == 'max_iterations_reached') == expect_abort)
    print(f'--- {name}')
    for line in final['log']:
        print('   ', line)
    print(f'    断言: delivered={final["delivered"]} '
          f'iteration={final["iteration"]}(期望{expect_it}) verdict={final["verdict"]}')
    return ok


def main():
    a = run('场景A: 首轮通过', '手机开箱视频，要有产品特写', 1)
    b = run('场景B: 评审回环', '一款保温杯的介绍视频', 2)
    c = run('场景C: 超限强制交付', '一款保温杯的介绍视频', 1, expect_abort=True)
    passed = a and b and c
    print('\nVIDEO_GRAPH_TEST:', 'PASS' if passed else 'FAIL')
    sys.exit(0 if passed else 1)


if __name__ == '__main__':
    main()
