# -*- coding: utf-8 -*-
"""rag_eval — RAG 检索质量评测（30+ 题, 含精排开/关消融）。

用例: 口语化/换述问法映射到期望条目(按 title, 容忍同义条目),
外加 6 条题外话校验「未命中必须走兜底」(不编造)。
通过线: 题内命中率 >= 0.80 且题外兜底正确率 >= 0.90。
报告: docs/rag_eval_<时间戳>.md
用法: .venv-rag/Scripts/python.exe scripts/rag_eval.py [--rerank-only/--fast-only]
"""
import argparse
import json
import sqlite3
import statistics
import time
from datetime import datetime

import requests
import yaml

# (question, 期望条目title集合)。多条并存=同义条目, 命中其一即算对。
CASES = [
    ('课程多少钱', {'课程价格'}),
    ('现在报名什么价格', {'课程价格'}),
    ('有没有优惠价格', {'课程价格', '直播间优惠'}),
    ('几点开播', {'上课时间'}),
    ('每天晚上什么时间上课', {'上课时间'}),
    ('直播是几点开始', {'上课时间'}),
    ('不满意可以退款吗', {'退款政策'}),
    ('交了钱还能退吗', {'退款政策'}),
    ('这个课适合什么人', {'适合人群'}),
    ('上班族能学吗', {'适合人群'}),
    ('我什么都不会能学会吗', {'零基础能学吗'}),
    ('小白能上手吗', {'零基础能学吗'}),
    ('课是怎么上的', {'上课形式'}),
    ('是录播还是直播', {'上课形式'}),
    ('错过了直播怎么办', {'课程回放', '上课迟到怎么办'}),
    ('有没有回放可以看', {'课程回放'}),
    ('讲课的老师什么水平', {'讲师背景'}),
    ('师资怎么样', {'讲师背景'}),
    ('课后有问题找谁', {'课后答疑'}),
    ('有老师辅导吗', {'课后答疑'}),
    ('我的电脑很旧能带得动吗', {'电脑配置要求'}),
    ('对笔记本配置有要求吗', {'电脑配置要求'}),
    ('学完这个课能干什么', {'学完能做什么'}),
    ('学会以后能做啥项目', {'学完能做什么'}),
    ('一共有多少节课', {'课时安排'}),
    ('整个课程要学多久', {'课时安排', '学习周期'}),
    ('下一期什么时候开班', {'开班时间'}),
    ('可以先试听一下吗', {'免费试听'}),
    ('有没有免费的课先体验', {'免费试听'}),
    ('学完有证书吗', {'结业证书'}),
    ('现在下单有什么活动', {'直播间优惠'}),
    ('有没有赠品', {'直播间优惠', '资料包内容'}),
    ('这个课在哪里买', {'购买方式'}),
    ('怎么报名下单', {'购买方式'}),
    ('多久能学完所有内容', {'学习周期', '课时安排'}),
    ('报名送什么资料', {'资料包内容'}),
    ('有提示词模板吗', {'资料包内容'}),
    ('今天来晚了还能跟上吗', {'上课迟到怎么办', '课程回放'}),
    # 题外话: 期望兜底(expected=None)
    ('今天天气怎么样', None),
    ('你会唱歌吗', None),
    ('现在比特币什么行情', None),
    ('附近有什么好吃的餐厅', None),
    ('1加1等于几', None),
    ('你叫什么名字', None),
]

PASS_IN_SCOPE = 0.80
PASS_FALLBACK = 0.90


def load_service_url(config_path):
    with open(config_path, 'r', encoding='utf-8') as f:
        cfg = yaml.safe_load(f)['rag']['service']
    return f"http://{cfg['host']}:{cfg['port']}"


def title_to_id(db_path):
    conn = sqlite3.connect(db_path)
    rows = conn.execute('SELECT id, title FROM knowledge').fetchall()
    conn.close()
    return {t: i for i, t in rows}


def run_mode(base_url, rerank):
    in_scope, out_scope = [], []
    for question, expected in CASES:
        t0 = time.perf_counter()
        r = requests.post(f'{base_url}/v1/ask',
                          json={'question': question, 'rerank': rerank},
                          timeout=30)
        dt = (time.perf_counter() - t0) * 1000
        data = r.json()
        item = {'q': question, 'expected': expected, 'latency_ms': round(dt, 1),
                'hit_id': data.get('hit_id'),
                'decision': data.get('decision'),
                'score': data.get('score'),
                'rerank_score': data.get('rerank_score')}
        if expected is None:
            item['ok'] = data.get('decision') == 'fallback'
            out_scope.append(item)
        else:
            item['ok'] = (data.get('decision') == 'standard'
                          and data.get('hit_id') is not None)
            item['_ok_ids'] = None   # 填充在主流程
            in_scope.append(item)
    return in_scope, out_scope


def attach_ids(items, mapping):
    for it in items:
        if it['expected']:
            ok_ids = {mapping[t] for t in it['expected'] if t in mapping}
            it['_ok_ids'] = ok_ids
            it['ok'] = it['ok'] and it['hit_id'] in ok_ids


def stats(latencies):
    lat = sorted(l['latency_ms'] for l in latencies)
    return {'p50': statistics.median(lat),
            'p95': lat[max(0, int(len(lat) * 0.95) - 1)] if lat else 0,
            'max': lat[-1] if lat else 0}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='configs/rag.yaml')
    parser.add_argument('--rerank-only', action='store_true')
    parser.add_argument('--fast-only', action='store_true')
    args = parser.parse_args()

    with open(args.config, 'r', encoding='utf-8') as f:
        cfg = yaml.safe_load(f)['rag']
    base_url = (f"http://{cfg['service']['host']}:{cfg['service']['port']}")
    mapping = title_to_id(cfg['knowledge_db'])

    modes = []
    if not args.fast_only:
        modes.append(('精排开启', True))
    if not args.rerank_only:
        modes.append(('纯向量(关闭精排)', False))

    report_time = datetime.now().strftime('%Y%m%d_%H%M%S')
    lines = [f'# RAG 评测报告 {report_time}', '',
             f'- 用例: 题内 {sum(1 for _, e in CASES if e)} 条 / '
             f'题外 {sum(1 for _, e in CASES if e is None)} 条',
             f'- 服务: {base_url}', '']

    all_pass = True
    for name, rerank in modes:
        in_scope, out_scope = run_mode(base_url, rerank)
        attach_ids(in_scope, mapping)
        hit = sum(1 for i in in_scope if i['ok']) / len(in_scope)
        fb = sum(1 for i in out_scope if i['ok']) / len(out_scope)
        s_in, s_out = stats(in_scope), stats(out_scope)
        passed = hit >= PASS_IN_SCOPE and fb >= PASS_FALLBACK
        all_pass = all_pass and passed
        lines += [f'## {name}（rerank={rerank}）', '',
                  f'- 题内命中率: **{hit:.1%}** ({sum(1 for i in in_scope if i["ok"])}/{len(in_scope)})'
                  f'  通过线 {PASS_IN_SCOPE:.0%}',
                  f'- 题外兜底正确率: **{fb:.1%}** ({sum(1 for i in out_scope if i["ok"])}/{len(out_scope)})'
                  f'  通过线 {PASS_FALLBACK:.0%}',
                  f'- 题内延迟 p50={s_in["p50"]:.0f}ms p95={s_in["p95"]:.0f}ms '
                  f'max={s_in["max"]:.0f}ms',
                  f'- 结论: {"✅ 通过" if passed else "❌ 未通过"}', '',
                  '| 问题 | 期望 | 命中ID | 决策 | rerank分 | 延迟ms | 结果 |',
                  '|---|---|---|---|---|---|---|']
        for it in in_scope + out_scope:
            exp = ','.join(it['expected']) if it['expected'] else '(兜底)'
            ok = '✅' if it['ok'] else '❌'
            lines.append(
                f"| {it['q']} | {exp} | {it['hit_id']} | {it['decision']} "
                f"| {it['rerank_score']} | {it['latency_ms']} | {ok} |")
        misses = [it for it in in_scope if not it['ok']]
        if misses:
            lines += ['', '### 题内未命中明细', '']
            for it in misses:
                lines.append(f"- {it['q']} → 期望 {it['expected']}, "
                             f"实际 hit_id={it['hit_id']} "
                             f"score={it['score']} rerank={it['rerank_score']}")
        lines.append('')

    out_path = f'docs/rag_eval_{report_time}.md'
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print('\n'.join(lines[:14]))
    print(f'\n完整报告: {out_path}')
    print('总体结论:', 'PASS' if all_pass else 'FAIL')
    raise SystemExit(0 if all_pass else 1)


if __name__ == '__main__':
    main()
