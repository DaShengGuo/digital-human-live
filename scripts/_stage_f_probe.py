# -*- coding: utf-8 -*-
"""阶段F调度实测驱动: 排一个 ~90 秒后触发的 once 计划, 等调度线程到点执行预检。
预期(伴侣未运行): 开播预检失败 → 场次 needs_attention, 事件留痕。
用法: python scripts/_stage_f_probe.py  （先确保控制台+编排器在跑）
"""
import sys
import os
import time
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from apps import storage   # noqa: E402


def main():
    fire = (datetime.datetime.now() + datetime.timedelta(seconds=90)).strftime('%H:%M')
    sid = storage.add_schedule('调度实测场', 'once', fire, 5, 'fallback_8g')
    print('排计划 id=%s 触发=%s  等待约100s…' % (sid, fire))
    deadline = time.time() + 130
    while time.time() < deadline:
        s = storage.query('SELECT id,state,reason FROM sessions ORDER BY rowid DESC LIMIT 1')
        evs = [e['detail'] for e in storage.recent_events(4)
               if e['detail'] and ('调度' in e['detail'] or '开播' in e['detail']
                                   or '预检' in e['detail'] or 'needs' in str(s))]
        if s and 'sch' in s[0]['id']:
            print('SESSION:', s[0])
        if evs:
            for e in evs:
                print('EVENT:', e[:90])
        if s and s[0]['state'] in ('needs_attention', 'live'):
            print('>>> 调度已触发, 结果:', s[0]['state'], '-', s[0]['reason'])
            return 0
        time.sleep(15)
        print('…still waiting, last:', (s[0] if s else None))
    print('TIMEOUT: 调度未在该窗口内触发')
    return 1


if __name__ == '__main__':
    sys.exit(main())
