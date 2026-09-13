# -*- coding: utf-8 -*-
"""rag_seed — 为 RAG 评测补种知识条目（幂等, 按 title 去重）。

条目为「AI 实训课程直播」业务场景的种子数据（与库中已有两条同场景）,
source 标记 seed:rag_uplift, 上线前请业务负责人逐条复核口径。
用法: .venv-rag/Scripts/python.exe scripts/rag_seed.py
"""
import sqlite3
import time

DB = 'data/dhlive.db'

ENTRIES = [
    # title, body, category, keywords
    ('退款政策', '开课7天内不满意可以申请全额退款，需要提供学习记录，1-3个工作日原路退回。', '售后', '退款,退钱,退费,不想要了'),
    ('适合人群', '零基础上班族、在校大学生、想转行做AI的从业者都可以学，不需要任何编程基础。', 'FAQ', '适合,谁可以学,人群,要求'),
    ('零基础能学吗', '完全可以，课程从软件安装开始手把手教，前两节就是零基础入门。', 'FAQ', '零基础,小白,没基础,新手'),
    ('上课形式', '线上直播授课，手机电脑都能看，支持实时互动和连麦答疑。', 'FAQ', '怎么上课,形式,直播吗,线上'),
    ('课程回放', '直播结束后24小时内上传回放，报名学员可以反复看，有效期一年。', 'FAQ', '回放,错过,录播,看不了'),
    ('讲师背景', '主讲讲师团队均有多年AI项目实战经验，课程案例全部来自真实项目。', '师资', '老师,讲师,师资,谁讲'),
    ('课后答疑', '直播间实时答疑，课后有作业批改和学习群专属答疑。', '服务', '答疑,问题,提问,辅导'),
    ('电脑配置要求', 'Windows 10以上系统，内存8G起步，有独立显卡体验更好，普通办公本也能学。', 'FAQ', '配置,电脑,显卡,要求高吗'),
    ('学完能做什么', '可以做AI短视频、搭建数字人直播间、用AI做自动化办公，课程结尾有实战项目。', '价值', '学会,能做什么,有什么用,用处'),
    ('课时安排', '一共42节课，每周固定更新，每天学习1.5小时，8周可以学完主线内容。', 'FAQ', '多少节课,课时,多久,多长时间'),
    ('开班时间', '每月1号开新班，报名后班主任会拉群通知具体安排。', 'FAQ', '开班,什么时候开,班期'),
    ('免费试听', '第一节课免费试听，在直播间扣1就可以领取试听链接。', '活动', '试听,免费,体验,先看看'),
    ('结业证书', '完成全部课程并通过结业作业，颁发课程结业证书。', 'FAQ', '证书,结业,证明'),
    ('直播间优惠', '直播间下单立减300元，还赠送AI提示词模板库和工具清单资料包。', '活动', '优惠,便宜,减,活动,赠品'),
    ('购买方式', '点击直播间小黄车选择课程下单，或者添加助教微信咨询报名。', '购买', '怎么买,下单,购买,报名'),
    ('学习周期', '每天投入1.5小时，主线内容8周学完，之后可以按自己的节奏复习进阶。', 'FAQ', '多久学会,周期,进度'),
    ('资料包内容', '报名赠送AI提示词模板库、常用工具清单和课后实操素材包。', '活动', '资料,送什么,赠送,模板'),
    ('上课迟到怎么办', '迟到不用怕，都有回放，群里也可以问当天的问题。', 'FAQ', '迟到,来不及,没赶上'),
]

OUT_OF_SCOPE_NOTE = '以上为种子条目, 口径需业务负责人复核'


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    added, skipped = 0, 0
    for title, body, category, keywords in ENTRIES:
        row = conn.execute('SELECT id FROM knowledge WHERE title=?',
                           (title,)).fetchone()
        if row:
            skipped += 1
            continue
        conn.execute(
            'INSERT INTO knowledge(title,body,category,keywords,source,'
            'version,valid_until,allow_speak,enabled,updated_at) '
            'VALUES(?,?,?,?,?,1,0,1,1,?)',
            (title, body, category, keywords, 'seed:rag_uplift', time.time()))
        added += 1
    conn.commit()
    total = conn.execute('SELECT COUNT(*) FROM knowledge').fetchone()[0]
    conn.close()
    print(f'新增 {added} 条, 跳过 {skipped} 条, 当前总数 {total} ({OUT_OF_SCOPE_NOTE})')


if __name__ == '__main__':
    main()
