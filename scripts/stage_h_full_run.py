# -*- coding: utf-8 -*-
"""阶段H 整场 60 分钟业务流程验收（后台自动跑, 结果写 logs/stage_h_60min.done）。

覆盖: 连续讲解(10min) → 弹幕FAQ(3条) → 人工接管+解除补答 → 一次可控故障恢复
(杀 TTS 子进程, 看门狗自动拉起) → 收尾, 汇总指标。
"""
import os
import sys
import json
import time
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DONE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    'logs', 'stage_h_60min.done')
ORCH, CON = 'http://127.0.0.1:8020', 'http://127.0.0.1:8030'


def _post(base, path, obj, t=12):
    req = urllib.request.Request(base + path, data=json.dumps(obj).encode('utf-8'),
                                 headers={'Content-Type': 'application/json'})
    return json.loads(urllib.request.urlopen(req, timeout=t).read().decode())


def _get(url, t=8):
    return json.loads(urllib.request.urlopen(url, timeout=t).read().decode())


def fps_delta():
    f1 = _get(ORCH + '/healthz')['session']['frames_received']
    time.sleep(5)
    f2 = _get(ORCH + '/healthz')['session']['frames_received']
    return (f2 - f1) / 5.0


def main():
    t_start = time.time()
    texts = ['你好，欢迎来到AI启杭实训基地，我是你们的老师，叫我李老师就好。',
             '今天我们讲一讲人工智能的基础知识。',
             '我们的课程包含从零基础到进阶的完整路线，感兴趣的同学可以了解一下。']
    report = []

    # ── 阶段1: 连续讲解 10min ─────────────────────────────
    n = 0
    t0 = time.time()
    while time.time() - t0 < 600:
        _post(CON, '/control/say', {'text': texts[n % 3]})
        n += 1
        time.sleep(16)
    fps = fps_delta()
    report.append(f'[1] 连续讲解10min: 派发{n}次, 接收端{fps:.1f}fps')

    # ── 阶段2: 弹幕 FAQ 3 条 ──────────────────────────────
    qs = ['课程价格是多少？', '零基础可以学会吗？', '上课需要准备什么设备？']
    ok = 0
    for q in qs:
        _post(CON, '/danmaku/ingest', {'source': 'manual', 'user': '验收员', 'text': q})
        time.sleep(12)
    s = _get(CON + '/api/status')
    answered = [x for x in s['danmaku']['recent'] if x['user'] == '验收员'
                and x['decision'] == 'answered']
    ok = len(answered)
    report.append(f'[2] 弹幕FAQ: {ok}/3 条 answered')

    # ── 阶段3: 人工接管+解除补答 ──────────────────────────
    _post(CON, '/control/takeover', {})
    _post(CON, '/danmaku/ingest', {'source': 'manual', 'user': '验收TA', 'text': '这个课怎么报名？'})
    time.sleep(4)
    s = _get(CON + '/api/status')
    dec = [x['decision'] for x in s['danmaku']['recent'] if x['user'] == '验收TA']
    held = dec and dec[0] == 'pending'
    _post(CON, '/control/release', {})
    time.sleep(10)
    s = _get(CON + '/api/status')
    dec2 = [x['decision'] for x in s['danmaku']['recent'] if x['user'] == '验收TA']
    resumed = dec2 and dec2[0] == 'answered'
    report.append(f'[3] 接管保留pending={held}, 解除补答answered={resumed}')

    # ── 阶段4: 可控故障恢复（杀 TTS, 看门狗自动拉起）──────
    import subprocess
    # 阶段D已废弃 wmic（Win11 移除）, 用 PID 文件定位 TTS 子进程
    import json as _json
    killed = False
    tts_pid = None
    try:
        pidf = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'logs', 'processes.pid')
        tts_pid = _json.load(open(pidf, encoding='utf-8')).get('tts', {}).get('pid')
    except Exception:
        pass
    if tts_pid:
        subprocess.run(['taskkill', '/PID', str(tts_pid), '/T', '/F'],
                       capture_output=True)
        killed = True
    time.sleep(70)   # 看门狗 5s 采样 + 拉起 + 模型加载 ~60s
    try:
        h = _get(ORCH + '/healthz')
        recovered = h['ok'] or h['procs'].get('tts', {}).get('state') == 'running'
    except Exception:
        recovered = False
    report.append(f'[4] 杀TTS进程(killed={killed}), 看门狗恢复={recovered}')

    # ── 阶段5: 收尾（再播一条确认全链路仍通）──────────────
    r = _post(CON, '/control/say', {'text': '整场验收收尾，系统运行正常。'})
    time.sleep(12)
    d = _get(ORCH + '/status')
    report.append(f"[5] 收尾播报 dispatched={d['jobs'].get(r['job_id'], {}).get('state') == 'dispatched'}")

    total_min = (time.time() - t_start) / 60
    summary = (f'整场验收完成: 实际{total_min:.0f}min\n' + '\n'.join(report) +
               f'\n@{time.strftime("%Y-%m-%d %H:%M:%S")}')
    print(summary)
    with open(DONE, 'w', encoding='utf-8') as f:
        f.write(summary)


if __name__ == '__main__':
    main()
