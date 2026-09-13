# -*- coding: utf-8 -*-
"""bench_danmaku_ocr.py — 「读取直播间弹幕」场景实测: 本地 OCR vs 本地通用视觉模型(VLM)

目标: 为数字人直播链路自动读取弹幕/评论文本, 在 8G 显存(数字人常驻)约束下做选型。

被测方案
  A. OCR  : rapidocr 3.9.2 + onnxruntime(CPU), 解释器 D:\\Python39\\python.exe
  B. VLM  : ollama qwen2.5vl:7b, HTTP /api/chat, base64 传图

测量项
  - 冷启动耗时 / 单图耗时(3 次取中位数) / 文本输出
  - 整图 vs 弹幕 ROI 裁剪 的耗时差
  - VLM 推理期间 nvidia-smi 采样峰值显存, 以及模型常驻显存
  - 与人眼 ground truth 的对照(漏字/错字/多余/顺序/非弹幕文字混入)

用法(必须用装了 rapidocr 的解释器):
  D:\\Python39\\python.exe D:\\AI\\digital-human-live\\scripts\\bench_danmaku_ocr.py
可选参数:
  --skip-vlm        只跑 OCR
  --only img01,img05
  --runs 3
"""

import argparse
import base64
import difflib
import io
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

BENCH_DIR = r'D:\AI\digital-human-live\data\bench_danmaku'
DOCS_DIR = r'D:\AI\digital-human-live\docs'
OLLAMA = 'http://127.0.0.1:11434'
VLM_MODEL = 'qwen2.5vl:7b'
VLM_PROMPT = '只输出图中所有可见的中文文本，每行一条，不要解释。'
VLM_PROMPT_STRUCT = ('这是抖音直播的评论区/弹幕截图。请按行输出每条观众发言，'
                     '格式为「昵称|内容」，不要输出点赞数、按钮、时间、输入框提示等界面文字，不要解释。')
VLM_NUM_PREDICT = 384

# ---------------------------------------------------------------- 素材与 ground truth
# gt_lines  : 人眼确认的「弹幕/评论」文本(不含 UI 装饰文字)。用于漏字率统计。
# ui_lines  : 同区域内属于界面装饰/按钮/视频自带标题的文字(方法若输出即算「非弹幕混入」)。
# kind      : float_danmaku=浮层弹幕 | comment_list=评论面板 | subtitle=视频自带字幕 | ui_negative=非弹幕界面
IMAGES = [
    dict(key='img01', file='img01_pipeline_1_1788921962.png', roi=(0, 140, 1080, 640),
         kind='float_danmaku',
         note='全屏视频上的浮层弹幕, 叠在明亮星云背景上, 部分被红包挂件遮挡, 右侧多行被屏幕边缘截断',
         gt_lines=['一级文明 143', '马斯克的梦想', '收钱就有了根据', '最新消息，中国太空', '星链'],
         ui_lines=['点', '关注', '南京', '团购', '商城', '推荐', '星尘译官', '点击激活']),
    dict(key='img02', file='img02_pipeline_9_1788923043.png', roi=(0, 140, 1080, 760),
         kind='float_danmaku',
         note='12 条完全相同的「接好运」弹幕网格(4列×3行), 左边缘另有被截断的碎片',
         gt_lines=['接好运'] * 12,
         ui_lines=['点', '关注', '南京', '团购', '商城', '推荐', '点击激活']),
    dict(key='img03', file='img03_pipeline_4_1788922548.png', roi=(0, 140, 1080, 640),
         kind='float_danmaku',
         note='浮层弹幕与视频自带黄色大字标题严重重叠, 深浅背景交界, 含 emoji 贴纸',
         gt_lines=['token太贵了', '难怪', '那么嗨免费吗?', '有', '免费你也用不了', '看到这些游戏大作'],
         ui_lines=['三次方科技油', '关注', '南京', '团购', '商城', '推荐', '点击激活',
                   'GPT-6 Astra全量开放', '发布1天 各种3D建模游戏案例盘点大赏']),
    dict(key='img04', file='img04_pipeline_1_1788927362.png', roi=(0, 140, 1080, 700),
         kind='subtitle',
         note='视频自带字幕(非弹幕), 浅色小字压在人物与墙面背景上, 可读性差',
         gt_lines=['谁懂这份仙侠孤勇', '一念间天荒仙灭，我仍立八荒之间'],
         ui_lines=[]),
    dict(key='img05', file='img05_pipeline_2_1788927441.png', roi=(0, 900, 1080, 2300),
         kind='comment_list',
         note='评论面板: 昵称 + 评论文本 + 图片缩略图卡片, 存在重复昵称',
         gt_lines=['讲得很清楚，收藏了慢慢看', '内容很有质量，已三连支持'],
         ui_lines=['评论 3818', 'AI 解析', '我', '刚刚', '回复', '去发布作品', '刚刚看过',
                   '发条评论，说说你的感受', '1人公司全链路赋能']),
    dict(key='img06', file='img06_pipeline_1_1788850598.png', roi=(0, 1000, 1080, 2300),
         kind='comment_list',
         note='评论面板: 评论文本 + 一整张长文卡片(DeepSeek V5 传闻) + Toast 提示遮挡评论',
         gt_lines=['太治愈了，看完心情都变好了'],
         ui_lines=['评论 3856', 'AI 解析', '大圣 2077', '发送中', '回复', '分享你此刻的想法',
                   '已将剪贴板中的内容粘贴到抖音', '07-23', '山东', '展开204条回复']),
    dict(key='img07', file='img07_pipeline_2_1788851023.png', roi=(0, 1000, 1080, 2300),
         kind='comment_list',
         note='评论面板: 评论文本 + 长文卡片 + Toast 遮挡',
         gt_lines=['氛围感拉满，喜欢这种风格'],
         ui_lines=['评论 3.5万', 'AI 解析', '大圣 2077', '发送中', '回复', '分享你此刻的想法',
                   '已将剪贴板中的内容粘贴到抖音']),
    dict(key='img08', file='img08_pipeline_4_1788851176.png', roi=(0, 1000, 1080, 2300),
         kind='comment_list',
         note='评论面板: 评论文本 + 长文卡片 + Toast 遮挡, 下方还有半截被遮挡的评论',
         gt_lines=['这条视频拍得真不错，画面很舒服', '支持一下，期待更新'],
         ui_lines=['评论 3.5万', 'AI 解析', '大圣 2077', '发送中', '回复', '分享你此刻的想法',
                   '已将剪贴板中的内容粘贴到抖音']),
    dict(key='img09', file='img09_pipeline_7_1788922827.png', roi=(0, 0, 1080, 2340),
         kind='ui_negative',
         note='反例: 抖音热榜页, 画面里没有弹幕, 只有榜单标题与账号说明',
         gt_lines=[],
         ui_lines=['抖音热点·去热点频道看更多', '24小时精选热点', '热榜TOP5', '香港首任特首董建华...',
                   '@中国新闻网: 香港首任特首', '董建华逝世，享年89岁', '社会榜TOP1',
                   '吃播网红"干饭莹莹" ...', '@国+社区: 笑着提醒大家"身', '体第一"，视频发出3天后...',
                   '汽车类高热', '青岛保时捷女销冠称其...', '@河南都市频道: 保时捷女销',
                   '冠：销量排名全球第一，屡...', '热', '不感兴趣', '订阅精选热点', '上滑继续看视频',
                   '外星文明', '别只知道AI回微信']),
    dict(key='img10', file='img10_pipeline_2_1788922220.png', roi=(0, 0, 1080, 2340),
         kind='ui_negative',
         note='反例: 互动消息列表(私信/互动通知), 不是直播间弹幕, 但含真实评论句与 7 个重复昵称',
         gt_lines=[],
         ui_lines=['互动消息', '粉丝', '99+', '杭州众创猴科技有限公司', '氛围感拉满，喜欢这种风格',
                   '3分钟前', '每天刷到你的视频都是惊喜', '8分钟前', '哪里，谢谢', '2025年12月9日',
                   '@豆包AI智能助手 帮我提取视频文字', '网址来个', '大哥来了',
                   '回复：弟弟啊弟弟，你这样讲就不对了，小心我发你...', '1人公司全链路赋能',
                   '外星文明', '别只知道AI回微信']),
]

PUNCT = re.compile(r'[\s，。！？、,.!?：:；;""\'\'（）()\[\]【】<>《》~～\-—_·|/\\+*#@%&^`]')


def norm(s):
    """归一化: 去空白/标点/emoji, 大小写统一, 用于比对。"""
    s = s or ''
    s = PUNCT.sub('', s)
    s = re.sub(r'[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]', '', s)
    return s.lower()


def similarity(a, b):
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


# ---------------------------------------------------------------- 显存采样
def gpu_used_mib():
    try:
        out = subprocess.run(
            ['nvidia-smi', '--query-gpu=memory.used', '--format=csv,noheader,nounits'],
            capture_output=True, text=True, timeout=15).stdout.strip().splitlines()
        return int(out[0].strip())
    except Exception:
        return -1


class VramSampler(threading.Thread):
    def __init__(self, interval=0.4):
        super().__init__(daemon=True)
        self.interval = interval
        self.samples = []
        self._stop = threading.Event()
        self.baseline0 = gpu_used_mib()

    def run(self):
        while not self._stop.is_set():
            self.samples.append(gpu_used_mib())
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()
        self.join(timeout=5)
        return [s for s in self.samples if s >= 0]

    @property
    def peak(self):
        v = [s for s in self.samples if s >= 0]
        return max(v) if v else -1


# ---------------------------------------------------------------- OCR 路径
def run_ocr(engine, img_path, runs=3):
    from PIL import Image
    im = Image.open(img_path).convert('RGB')
    out = {'runs': [], 'texts': [], 'scores': [], 'boxes': []}
    import numpy as np
    arr = np.array(im)
    for i in range(runs):
        t0 = time.perf_counter()
        res = engine(arr)
        dt = time.perf_counter() - t0
        out['runs'].append(dt)
        if i == 0:
            txts = list(getattr(res, 'txts', None) or [])
            scores = list(getattr(res, 'scores', None) or [])
            boxes = getattr(res, 'boxes', None)
            out['texts'] = [str(t) for t in txts]
            out['scores'] = [float(s) for s in scores] if scores is not None else []
            if boxes is not None:
                out['boxes'] = [[[float(p[0]), float(p[1])] for p in box] for box in boxes]
    out['median'] = statistics.median(out['runs'])
    out['min'] = min(out['runs'])
    return out


def ocr_result_lines(res):
    """按 y 坐标从上到下、x 从左到右排序, 得到阅读顺序。"""
    items = []
    for idx, t in enumerate(res['texts']):
        box = res['boxes'][idx] if idx < len(res['boxes']) else None
        y = min(p[1] for p in box) if box else idx * 100
        x = min(p[0] for p in box) if box else 0
        items.append((y, x, t))
    items.sort(key=lambda v: (round(v[0] / 25.0), v[1]))
    return [t for _, _, t in items]


# ---------------------------------------------------------------- VLM 路径
def ollama_chat(image_b64, prompt, keep_alive='5m', timeout=900):
    payload = {
        'model': VLM_MODEL, 'stream': False, 'keep_alive': keep_alive,
        'options': {'temperature': 0, 'num_predict': VLM_NUM_PREDICT, 'num_ctx': 8192},
        'messages': [{'role': 'user', 'content': prompt, 'images': [image_b64]}],
    }
    req = urllib.request.Request(
        OLLAMA + '/api/chat', data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode('utf-8'))
    dt = time.perf_counter() - t0
    return {
        'elapsed': dt,
        'text': (d.get('message') or {}).get('content', '') or '',
        'load_s': (d.get('load_duration') or 0) / 1e9,
        'prompt_eval_count': d.get('prompt_eval_count') or 0,
        'prompt_eval_s': (d.get('prompt_eval_duration') or 0) / 1e9,
        'eval_count': d.get('eval_count') or 0,
        'eval_s': (d.get('eval_duration') or 0) / 1e9,
    }


def ollama_unload():
    try:
        payload = {'model': VLM_MODEL, 'messages': [], 'keep_alive': 0}
        req = urllib.request.Request(
            OLLAMA + '/api/chat', data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=60).read()
    except Exception as e:
        print('  [warn] unload failed:', e)
    for _ in range(20):
        time.sleep(1.0)
        if gpu_used_mib() < 4000:
            break


def ollama_ps():
    try:
        with urllib.request.urlopen(OLLAMA + '/api/ps', timeout=20) as r:
            return json.loads(r.read().decode('utf-8')).get('models', [])
    except Exception:
        return []


def ollama_list():
    try:
        with urllib.request.urlopen(OLLAMA + '/api/tags', timeout=20) as r:
            return json.loads(r.read().decode('utf-8')).get('models', [])
    except Exception:
        return []


def b64_of(path):
    with open(path, 'rb') as f:
        return base64.b64encode(f.read()).decode('ascii')


def crop_to_bytes(img_path, roi, fmt='PNG'):
    from PIL import Image
    im = Image.open(img_path).convert('RGB')
    if roi:
        im = im.crop(tuple(roi))
    buf = io.BytesIO()
    im.save(buf, fmt)
    return buf.getvalue(), base64.b64encode(buf.getvalue()).decode('ascii'), im.size


# ---------------------------------------------------------------- 打分
def split_lines(text):
    out = []
    for ln in (text or '').splitlines():
        ln = ln.strip().lstrip('-*0123456789.、) ').strip()
        if ln:
            out.append(ln)
    return out


def score_group(gt_lines, pred_lines, ui_lines):
    """返回 (命中, 近似命中, 漏检, 多余)。多余 = 与 GT 及 UI 都不匹配的输出行。"""
    gt_u = [g for g in gt_lines if norm(g)]
    used = set()
    hit, approx = [], []
    for p in pred_lines:
        best, bi = 0.0, None
        for i, g in enumerate(gt_u):
            if i in used:
                continue
            r = similarity(p, g)
            if r > best:
                best, bi = r, i
        if bi is not None and best >= 0.97:
            hit.append(p)
            used.add(bi)
        elif bi is not None and best >= 0.60:
            approx.append((p, gt_u[bi], round(best, 2)))
            used.add(bi)
    missed = [g for i, g in enumerate(gt_u) if i not in used]
    extra = []
    for p in pred_lines:
        if p in hit or any(p == a[0] for a in approx):
            continue
        best_ui = max([similarity(p, u) for u in ui_lines], default=0.0)
        extra.append((p, round(best_ui, 2)))
    return dict(gt_n=len(gt_u), pred_n=len(pred_lines), hit=hit, approx=approx,
                missed=missed, extra=extra)


# ---------------------------------------------------------------- 报告
def fmt_lines(lines, limit=60):
    if not lines:
        return '_(空)_'
    s = '\n'.join('  %d. `%s`' % (i + 1, l) for i, l in enumerate(lines[:limit]))
    if len(lines) > limit:
        s += '\n  ...(共 %d 行)' % len(lines)
    return s


def build_report(meta, ocr_cold, results, vram, args):
    L = []
    A = L.append
    A('# 弹幕读取方案实测: 本地 OCR vs 本地通用视觉模型 (VLM)')
    A('')
    A('- 生成时间: %s' % meta['ts'])
    A('- 脚本: `scripts/bench_danmaku_ocr.py` (可用 `D:\\Python39\\python.exe` 复跑)')
    A('- 素材: 抖音手机截图副本, `data/bench_danmaku/` (原始文件未改动)')
    A('')
    A('## 0. 结论速览')
    A('')
    A(meta['conclusion'])
    A('')
    A('## 1. 环境与素材')
    A('')
    A('| 项 | 实测值 |')
    A('|---|---|')
    A('| GPU | %s |' % meta['gpu_name'])
    A('| 显存总量 | %d MiB |' % meta['gpu_total'])
    A('| 测试前基线显存(数字人/桌面常驻) | %d MiB |' % vram['baseline0'])
    A('| OCR 引擎 | rapidocr 3.9.2 (onnxruntime %s) |' % meta['ort_ver'])
    A('| OCR 执行提供者 | %s |' % ', '.join(meta['providers']))
    A('| VLM | ollama `%s` (%s GiB, ctx=8192) |' % (VLM_MODEL, meta['vlm_size_gib']))
    A('| VLM 提示词 | `%s` |' % VLM_PROMPT)
    A('| VLM 生成上限 | num_predict=%d |' % VLM_NUM_PREDICT)
    A('| 重复次数 | OCR/VLM 各 %d 次, 取中位数/分别列出 |' % args.runs)
    A('')
    A('本机实际 ollama 视觉模型清单: %s' % meta['vlm_list'])
    A('')
    A('测试图 (%d 张, 均为手机 1080 宽抖音截图):' % len(IMAGES))
    A('')
    A('| # | 文件 | 类型 | ROI | 说明 |')
    A('|---|---|---|---|---|')
    for it in IMAGES:
        full_img = (it['roi'][0] == 0 and it['roi'][1] == 0 and it['roi'][2] >= 1080
                    and it['roi'][3] >= 2340)
        A('| %s | `%s` | %s | %s | %s |' % (
            it['key'], it['file'], it['kind'],
            '整图' if full_img else '%d,%d,%d,%d' % it['roi'], it['note']))
    A('')
    A('> 素材局限(如实说明): 原始素材是**手机端抖音截图**(1080x2340/2400), 不是「抖音直播伴侣」'
      '桌面窗口截图。弹幕/评论文本形态、字体、字号级、背景复杂度与实际生产接近, '
      '但**窗口尺寸、弹幕滚动速度、行密度**与桌面端直播伴侣不完全一致; '
      '本报告的面积/耗时结论可直接外推, 绝对准确率数字需在生产窗口上复测一次。')
    A('')
    A('## 2. 速度实测')
    A('')
    A('### 2.1 OCR (rapidocr, CPU)')
    A('')
    A('- 模型冷启动(构造 RapidOCR + 首图推理): **%.2f s**' % ocr_cold['init_s'])
    A('- 首图用时 %.2f s (含模型加载)' % ocr_cold['first_img_s'])
    A('')
    A('| 图 | 整图尺寸 | 整图中位 (s) | ROI 尺寸 | ROI 中位 (s) | 加速比 | 整图 vs ROI 面积比 |')
    A('|---|---|---|---|---|---|---|')
    for r in results:
        o = r['ocr_full']
        c = r['ocr_roi']
        A('| %s | %dx%d | %.3f | %dx%d | %.3f | %.2fx | %.2fx |' % (
            r['key'], o['size'][0], o['size'][1], o['median'],
            c['size'][0], c['size'][1], c['median'],
            (o['median'] / c['median']) if c['median'] else 0,
            (o['size'][0] * o['size'][1]) / max(1, c['size'][0] * c['size'][1])))
    A('')
    med_full = statistics.median([r['ocr_full']['median'] for r in results])
    med_roi = statistics.median([r['ocr_roi']['median'] for r in results])
    A('- 整图中位耗时 **%.3f s**, ROI 中位耗时 **%.3f s**, 平均加速 **%.2fx**'
      % (med_full, med_roi, med_full / med_roi if med_roi else 0))
    A('')
    vlm_done = [r for r in results if r.get('vlm')]
    if vlm_done:
        keep = vlm_done[0].get('vlm_keepalive', '5m')
        A('### 2.2 VLM (ollama %s)' % VLM_MODEL)
        A('')
        A('> **测试范围受限(如实说明)**: 本轮 VLM 只在 **%d/%d 张图**上实测 —— 加载 %s 会把整卡显存从'
          % (len(vlm_done), len(results), VLM_MODEL))
        A('> 约 %d MiB 顶到 %d MiB, 生产数字人(LiveTalking)被看门狗反复重启; '
          % (vram['baseline0'], vram['vlm_peak']))
        A('> 因此其余图只跑了 OCR。本表 keep_alive=`%s`, 每张图跑完立即卸载。' % keep)
        A('')
        A('- 模型冷加载耗时(第一次请求内含): **%.1f s**' % vram['vlm_load_s'])
        A('- 模型显存增量(整卡, 相对加载前): **+%d MiB**' % vram['vlm_steady_delta'])
        A('')
        A('| 图 | run1 冷/首次 (s) | run2 (s) | run3 (s) | 中位(已加载) (s) | 输出 token | 视觉 token | 输出行数 |')
        A('|---|---|---|---|---|---|---|---|')
        for r in results:
            v = r['vlm']
            if not v:
                continue
            runs = v['runs']
            warm = runs[1:]
            A('| %s | %.2f | %s | %s | %s | %d | %d | %d |' % (
                r['key'], runs[0],
                ('%.2f' % runs[1]) if len(runs) > 1 else '-',
                ('%.2f' % runs[2]) if len(runs) > 2 else '-',
                ('%.2f' % statistics.median(warm)) if warm else 'n/a',
                v['meta'][-1]['eval_count'],
                v['meta'][-1]['prompt_eval_count'], len(r['vlm_lines'])))
        A('')
        allwarm = [x for r in results if r.get('vlm') for x in r['vlm']['runs'][1:]]
        if allwarm:
            A('- VLM 已加载后单图中位耗时 **%.2f s** (n=%d, 含视觉编码+生成)'
              % (statistics.median(allwarm), len(allwarm)))
    A('### 2.2b VLM 实测记录 (手工记录, 含提前终止说明)')
    A('')
    A(meta.get('vlm_block', '_(未测)_'))
    A('')
    A('## 3. 准确率对照 (逐图)')
    A('')
    A('评分口径: 归一化(去空白/标点/emoji)后完全一致=命中; 相似度 0.60~0.97=近似(算错字); '
      '<0.60 且与界面文字也不像=多余/幻觉。')
    A('')
    A('### 3.1 汇总')
    A('')
    A('| 图 | 类型 | GT 条数 | OCR 整图 命中/近似/漏/多 | OCR-ROI 命中/近似/漏/多 | VLM 命中/近似/漏/多 |')
    A('|---|---|---|---|---|---|')
    tot = {k: [0, 0, 0, 0] for k in ('ocr_full', 'ocr_roi', 'vlm')}
    for r in results:
        row = [r['key'], r['kind'], r['gt_n']]
        for k in ('ocr_full', 'ocr_roi', 'vlm'):
            s = r['score'].get(k)
            if not s:
                row.append('-')
                continue
            row.append('%d/%d/%d/%d' % (len(s['hit']), len(s['approx']), len(s['missed']), len(s['extra'])))
            tot[k][0] += len(s['hit'])
            tot[k][1] += len(s['approx'])
            tot[k][2] += len(s['missed'])
            tot[k][3] += len(s['extra'])
        A('| ' + ' | '.join(str(x) for x in row) + ' |')
    A('| **合计** | | | **%d/%d/%d/%d** | **%d/%d/%d/%d** | **%d/%d/%d/%d** |' % (
        tuple(tot['ocr_full']) + tuple(tot['ocr_roi']) + tuple(tot['vlm'])))
    A('')
    for k, name in (('ocr_full', 'OCR 整图'), ('ocr_roi', 'OCR 裁 ROI'), ('vlm', 'VLM 整图')):
        h, a, m, e = tot[k]
        denom = h + a + m
        A('- %s: 精确命中 %d, 近似 %d, 漏检 %d, 多余 %d → 行级召回 ≈ %.0f%%, 输出噪声率 ≈ %.0f%%'
          % (name, h, a, m, e, 100.0 * (h + 0.5 * a) / denom if denom else 0,
             100.0 * e / (h + a + e) if (h + a + e) else 0))
    A('')
    A('> 行级召回 = (精确命中 + 0.5×近似) / GT 条数; 输出噪声率 = 多余行 / 全部输出行。'
      '注意 img09/img10 两张反例的 GT 为空, 任何输出都会计入「多余」, '
      '这会抬高噪声率——这恰恰是「非弹幕画面是否误报」的度量。')
    A('')
    A('**按场景拆分(行级召回 / 输出噪声率):**')
    A('')
    A('| 场景 | 图数 | GT 条数 | OCR 整图 | OCR-ROI | VLM |')
    A('|---|---|---|---|---|---|')
    kinds = [('float_danmaku', '浮层弹幕'), ('comment_list', '评论面板'),
             ('subtitle', '视频自带字幕'), ('ui_negative', '非弹幕界面(反例)')]
    for kk, kname in kinds:
        sub = [r for r in results if r['kind'] == kk]
        if not sub:
            continue
        cells = []
        for k in ('ocr_full', 'ocr_roi', 'vlm'):
            h = sum(len(r['score'][k]['hit']) for r in sub if r['score'].get(k))
            a = sum(len(r['score'][k]['approx']) for r in sub if r['score'].get(k))
            m = sum(len(r['score'][k]['missed']) for r in sub if r['score'].get(k))
            e = sum(len(r['score'][k]['extra']) for r in sub if r['score'].get(k))
            rec = 100.0 * (h + 0.5 * a) / (h + a + m) if (h + a + m) else float('nan')
            noi = 100.0 * e / (h + a + e) if (h + a + e) else 0.0
            cells.append('%.0f%% / %.0f%%' % (rec, noi) if rec == rec else 'n/a / %.0f%%' % noi)
        A('| %s | %d | %d | %s |' % (kname, len(sub),
                                     sum(r['gt_n'] for r in sub), ' | '.join(cells)))
    A('')
    A('### 3.2 逐图明细 (Ground Truth vs 两者输出)')
    A('')
    for r in results:
        A('#### %s — %s' % (r['key'], r['kind']))
        A('')
        A('- 文件: `%s`' % r['file'])
        A('- 场景: %s' % r['note'])
        A('- ROI: %s' % (str(r['roi']),))
        A('')
        A('**人眼 Ground Truth — 弹幕/评论 (%d 条):**' % r['gt_n'])
        A('')
        A(fmt_lines(r['gt_lines']))
        A('')
        if r['ui_lines']:
            A('**同区域界面/非弹幕文字 (读出来算误读):** `%s`' % '`、`'.join(r['ui_lines']))
            A('')
        A('**OCR (整图) 输出 %d 行:**' % len(r['ocr_full_lines']))
        A('')
        A(fmt_lines(r['ocr_full_lines']))
        A('')
        A('**OCR (ROI) 输出 %d 行:**' % len(r['ocr_roi_lines']))
        A('')
        A(fmt_lines(r['ocr_roi_lines']))
        A('')
        if r.get('vlm'):
            A('**VLM 输出 %d 行 (第 1 次):**' % len(r['vlm_lines']))
            A('')
            A(fmt_lines(r['vlm_lines']))
            A('')
            A('**逐条判定:** 命中 `%s` / 近似 `%s` / 漏检 `%s` / 多余 `%s`' % (
                r['score']['vlm']['hit'] or '-',
                [x[0] for x in r['score']['vlm']['approx']] or '-',
                r['score']['vlm']['missed'] or '-',
                [x[0] for x in r['score']['vlm']['extra']][:8] or '-'))
            A('')
        A('---')
        A('')
    A('## 4. 显存实测')
    A('')
    A('| 时点 | 显存占用 (MiB) |')
    A('|---|---|')
    for k, v in vram['marks']:
        A('| %s | %d |' % (k, v))
    A('')
    A('- VLM 推理采样峰值: **%d MiB** (基线 %d MiB, 峰值增量 **+%d MiB**)'
      % (vram['vlm_peak'], vram['baseline0'], vram['vlm_peak'] - vram['baseline0']))
    A('- ollama 自报模型常驻: %s' % vram['ps_note'])
    A('')
    A(meta.get('vram_block', ''))
    A('')
    A('## 5. VLM 结构化输出试验 (昵称|内容)')
    A('')
    A(meta['struct_block'])
    A('')
    A('## 5b. VLM 只喂弹幕 ROI vs 喂整图')
    A('')
    A(meta.get('vlm_roi_block', '_(未测)_'))
    A('')
    A('## 6. 结论与选型建议')
    A('')
    A(meta['verdict'])
    A('')
    A('---')
    A('')
    A('## 附: 原始复现命令')
    A('')
    A('```')
    A(r'D:\Python39\python.exe D:\AI\digital-human-live\scripts\bench_danmaku_ocr.py')
    A('```')
    A('')
    A('原始结果 JSON 见: `%s`' % meta['json_path'])
    return '\n'.join(L)


def vlm_roi_mode(args):
    """补充实测: 只把弹幕 ROI 裁图喂给 VLM, 对比整图。结果写 JSON + 打印 markdown 片段。"""
    n = args.vlm_roi
    imgs = [i for i in IMAGES if i['kind'] in ('float_danmaku', 'comment_list')][:n]
    print('=== VLM ROI 模式: %d 张 ===' % len(imgs))
    ollama_unload()
    rows = []
    sampler = VramSampler()
    sampler.start()
    for idx, it in enumerate(imgs):
        p = os.path.join(BENCH_DIR, it['file'])
        roi_bytes, roi_b64, roi_size = crop_to_bytes(p, it['roi'])
        from PIL import Image
        full_size = Image.open(p).size
        keep = '5m'
        t0 = time.perf_counter()
        res = ollama_chat(roi_b64, VLM_PROMPT, keep_alive=keep)
        wall = time.perf_counter() - t0
        print('  %s ROI %dx%d: %.2fs (load %.1fs, 视觉token %d, 输出 %d tok)' % (
            it['key'], roi_size[0], roi_size[1], wall, res['load_s'],
            res['prompt_eval_count'], res['eval_count']))
        rows.append(dict(key=it['key'], full_size=full_size, roi_size=roi_size,
                         wall=wall, load_s=res['load_s'],
                         vision_tokens=res['prompt_eval_count'],
                         eval_count=res['eval_count'], text=res['text'],
                         lines=split_lines(res['text'])))
    samples = sampler.stop()
    ollama_unload()
    peak = max(samples) if samples else -1
    with open(os.path.join(BENCH_DIR, '_vlm_roi.json'), 'w', encoding='utf-8') as f:
        json.dump(dict(peak=peak, rows=rows), f, ensure_ascii=False, indent=1)
    print('峰值显存 %d MiB; 已写 _vlm_roi.json' % peak)
    print('---MARKDOWN---')
    print('- 模型在测前已加载(首张含一次冷加载 %.1fs), 之后为热态。' % rows[0]['load_s'])
    print('')
    print('| 图 | 整图尺寸 | ROI 尺寸 | 整图像素 | ROI 像素 | ROI 耗时 (s) | ROI 视觉 token | ROI 输出 token |')
    print('|---|---|---|---|---|---|---|---|')
    for r in rows:
        print('| %s | %dx%d | %dx%d | %.2f M | %.2f M | %.2f | %d | %d |' % (
            r['key'], r['full_size'][0], r['full_size'][1], r['roi_size'][0], r['roi_size'][1],
            r['full_size'][0] * r['full_size'][1] / 1e6,
            r['roi_size'][0] * r['roi_size'][1] / 1e6, r['wall'],
            r['vision_tokens'], r['eval_count']))
    print('')
    for r in rows:
        print('**%s ROI 输出 %d 行:**' % (r['key'], len(r['lines'])))
        print('')
        print(fmt_lines(r['lines']))
        print('')
    return rows


def render_from_json(json_path, args):
    """用已保存的原始 JSON 重建报告(不重跑耗时测量), 便于修正 ground truth / 补结论。"""
    with open(json_path, encoding='utf-8') as f:
        dump = json.load(f)
    meta = dict(dump['meta'])
    ocr_cold = dump['ocr_cold']
    vram = dict(dump['vram'])
    vram['marks'] = [tuple(x) for x in vram.get('marks', [])]
    by_key = {it['key']: it for it in IMAGES}
    results = []
    for r in dump['results']:
        src = by_key.get(r['key'])
        if src:
            r['gt_lines'] = src['gt_lines']
            r['ui_lines'] = src['ui_lines']
            r['note'] = src['note']
            r['roi'] = src['roi']
        r['gt_n'] = len([g for g in r['gt_lines'] if norm(g)])
        r['score'] = {'ocr_full': score_group(r['gt_lines'], r['ocr_full_lines'], r['ui_lines']),
                      'ocr_roi': score_group(r['gt_lines'], r['ocr_roi_lines'], r['ui_lines'])}
        if r.get('vlm_lines'):
            r['score']['vlm'] = score_group(r['gt_lines'], r['vlm_lines'], r['ui_lines'])
        results.append(r)
    struct_blocks = []
    if args.redo_struct:
        print('=== 重跑结构化提示词试验 ===')
        sampler_note = gpu_used_mib()
        for r in results:
            if r['kind'] != 'comment_list':
                continue
            if len(struct_blocks) >= 3:
                break
            p = os.path.join(BENCH_DIR, r['file'])
            print('  [结构化] %s' % r['key'])
            sres = ollama_chat(b64_of(p), VLM_PROMPT_STRUCT, keep_alive='5m')
            struct_blocks.append((r['key'], sres['elapsed'], split_lines(sres['text'])))
        print('  结构化试验后显存 %d MiB (前 %d)' % (gpu_used_mib(), sampler_note))
        ollama_unload()
    meta['struct_block'] = '\n'.join(
        '### %s (%.2f s)\n\n%s\n' % (k, t, fmt_lines(lines)) for k, t, lines in struct_blocks
    ) or meta.get('struct_block', '(未测)')
    qual = os.path.join(BENCH_DIR, '_qual.json')
    if os.path.exists(qual):
        with open(qual, encoding='utf-8') as f:
            meta.update(json.load(f))
    meta.setdefault('conclusion', '_(结论待填)_')
    meta.setdefault('verdict', '_(结论待填)_')
    meta['json_path'] = json_path
    md = build_report(meta, ocr_cold, results, vram, args)
    stamp = os.path.basename(json_path).replace('bench_result_', '').replace('.json', '')
    md_path = os.path.join(DOCS_DIR, 'bench_danmaku_ocr_%s.md' % stamp)
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(md)
    print('报告: %s' % md_path)
    for r in results:
        for k in ('ocr_full', 'ocr_roi', 'vlm'):
            s = r['score'].get(k)
            if s:
                print('  %-6s %-11s hit=%d approx=%d miss=%d extra=%d' % (
                    r['key'], k, len(s['hit']), len(s['approx']), len(s['missed']), len(s['extra'])))
    return md_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--runs', type=int, default=3)
    ap.add_argument('--skip-vlm', action='store_true')
    ap.add_argument('--only', type=str, default='')
    ap.add_argument('--from-json', type=str, default='', dest='from_json')
    ap.add_argument('--redo-struct', action='store_true')
    ap.add_argument('--vlm-roi', type=int, default=0, dest='vlm_roi',
                    help='补充实测: 只把弹幕 ROI 喂给 VLM, 指定张数')
    ap.add_argument('--vlm-limit', type=int, default=0, dest='vlm_limit',
                    help='只对前 N 张图跑 VLM(显存受限时用)')
    ap.add_argument('--vlm-keepalive', type=str, default='5m', dest='vlm_keepalive',
                    help='VLM keep_alive; 传 0 表示每次推理后立即卸载(生产共存时的唯一安全姿态)')
    ap.add_argument('--vlm-pause', type=float, default=0.0, dest='vlm_pause',
                    help='VLM 每张图之间的间隔秒数(留给数字人链路恢复)')
    ap.add_argument('--vlm-runs', type=int, default=0, dest='vlm_runs',
                    help='VLM 每张图重复次数(缺省与 --runs 相同)')
    ap.add_argument('--vlm-keys', type=str, default='', dest='vlm_keys',
                    help='只对这些图跑 VLM, 逗号分隔, 如 img02,img06,img10')
    args = ap.parse_args()

    if args.vlm_roi:
        vlm_roi_mode(args)
        return
    if args.from_json:
        render_from_json(args.from_json, args)
        return

    only = set(x.strip() for x in args.only.split(',') if x.strip())
    imgs = [i for i in IMAGES if not only or i['key'] in only]

    meta = {'ts': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
    gpu_name = subprocess.run(['nvidia-smi', '--query-gpu=name', '--format=csv,noheader'],
                              capture_output=True, text=True).stdout.strip()
    gpu_total = int(subprocess.run(['nvidia-smi', '--query-gpu=memory.total',
                                    '--format=csv,noheader,nounits'],
                                   capture_output=True, text=True).stdout.strip())
    meta['gpu_name'] = gpu_name
    meta['gpu_total'] = gpu_total
    import onnxruntime
    meta['ort_ver'] = onnxruntime.__version__
    meta['providers'] = onnxruntime.get_available_providers()
    vlm_tags = ollama_list()
    meta['vlm_list'] = '; '.join('%s(%s)' % (m['name'], ','.join(m.get('capabilities') or []))
                                 for m in vlm_tags) or '(取不到)'
    meta['vlm_size_gib'] = '%.2f' % next((m['size'] / 2 ** 30 for m in vlm_tags
                                          if m['name'] == VLM_MODEL), 0.0)

    vram = {'marks': [], 'baseline0': gpu_used_mib(), 'vlm_load_s': 0.0,
            'vlm_peak': -1, 'vlm_steady_delta': 0, 'ps_note': '(未测)'}
    vram['marks'].append(('基线(测前, 数字人/桌面常驻)', vram['baseline0']))

    # ---------------- OCR
    print('=== OCR: 初始化 rapidocr ===')
    from rapidocr import RapidOCR
    t0 = time.perf_counter()
    engine = RapidOCR()
    init_s = time.perf_counter() - t0
    print('  RapidOCR() 构造: %.2f s' % init_s)

    results = []
    for it in imgs:
        p = os.path.join(BENCH_DIR, it['file'])
        print('[OCR] %s' % it['key'])
        t0 = time.perf_counter()
        full = run_ocr(engine, p, args.runs)
        first_img_s = time.perf_counter() - t0
        roi_bytes, roi_b64, roi_size = crop_to_bytes(p, it['roi'])
        tmp_roi = os.path.join(BENCH_DIR, '_roi_%s.png' % it['key'])
        with open(tmp_roi, 'wb') as f:
            f.write(roi_bytes)
        roi = run_ocr(engine, tmp_roi, args.runs)
        from PIL import Image
        full_size = Image.open(p).size
        full['size'] = full_size
        roi['size'] = roi_size
        print('  整图 %dx%d 中位 %.3fs | ROI %dx%d 中位 %.3fs' % (
            full_size[0], full_size[1], full['median'], roi_size[0], roi_size[1], roi['median']))
        results.append(dict(
            key=it['key'], file=it['file'], kind=it['kind'], note=it['note'],
            roi=it['roi'], gt_lines=it['gt_lines'], ui_lines=it['ui_lines'],
            gt_n=len([g for g in it['gt_lines'] if norm(g)]),
            ocr_full=full, ocr_roi=roi,
            ocr_full_lines=ocr_result_lines(full), ocr_roi_lines=ocr_result_lines(roi),
            roi_b64=roi_b64, roi_size=roi_size, full_size=full_size))
    ocr_cold = dict(init_s=init_s, first_img_s=first_img_s)
    vram['marks'].append(('OCR 全程(CPU, 显存无变化)', gpu_used_mib()))
    print('OCR 冷启动: 构造 %.2fs, 含首图推理 %.2fs' % (init_s, first_img_s))

    # ---------------- VLM
    if not args.skip_vlm:
        print('=== VLM: %s ===' % VLM_MODEL)
        ollama_unload()
        vram['marks'].append(('VLM 前置: 模型卸载后', gpu_used_mib()))
        sampler = VramSampler()
        base_before_load = gpu_used_mib()
        sampler.start()
        struct_blocks = []
        if args.vlm_keys:
            ks = set(x.strip() for x in args.vlm_keys.split(',') if x.strip())
            vlm_imgs = [i for i in imgs if i['key'] in ks]
        elif args.vlm_limit:
            vlm_imgs = imgs[:args.vlm_limit]
        else:
            vlm_imgs = imgs
        by_key = {r['key']: r for r in results}
        vlm_runs = args.vlm_runs or args.runs
        keep = args.vlm_keepalive
        print('[VLM] 计划测试 %d 张: %s (keep_alive=%s, runs=%d)'
              % (len(vlm_imgs), [i['key'] for i in vlm_imgs], keep, vlm_runs))
        for n, it in enumerate(vlm_imgs, 1):
            r = by_key[it['key']]
            p = os.path.join(BENCH_DIR, it['file'])
            b64 = b64_of(p)
            print('[VLM] %s (%d/%d) keep_alive=%s' % (it['key'], n, len(vlm_imgs), keep))
            runs, metas, texts = [], [], []
            for k in range(vlm_runs):
                t0 = time.perf_counter()
                res = ollama_chat(b64, VLM_PROMPT, keep_alive=keep)
                wall = time.perf_counter() - t0
                runs.append(wall)
                metas.append(res)
                texts.append(res['text'])
                if k == 0:
                    print('  run1 %.2fs (load %.1fs, prompt %.1fs, eval %d tok/%.1fs)' % (
                        wall, res['load_s'], res['prompt_eval_s'],
                        res['eval_count'], res['eval_s']))
                    if res['load_s'] > vram['vlm_load_s']:
                        vram['vlm_load_s'] = res['load_s']
                else:
                    print('  run%d %.2fs' % (k + 1, wall))
            r['vlm'] = dict(runs=runs, meta=metas)
            r['vlm_lines'] = split_lines(texts[0])
            r['vlm_raw'] = texts
            r['vlm_keepalive'] = keep
            # 结构化输出试验: 仅评论类前 3 张跑 1 次
            if it['kind'] == 'comment_list' and len(struct_blocks) < 3 and str(keep) != '0':
                print('  [结构化] %s' % it['key'])
                sres = ollama_chat(b64, VLM_PROMPT_STRUCT, keep_alive=keep)
                struct_blocks.append((it['key'], sres['elapsed'], split_lines(sres['text'])))
            if str(keep) == '0':
                # 每张图跑完立即卸载, 避免长时间占用显存挤压数字人链路
                ollama_unload()
                print('    已卸载, 整卡显存 %d MiB' % gpu_used_mib())
            if args.vlm_pause:
                time.sleep(args.vlm_pause)
        samples = sampler.stop()
        vram['vlm_peak'] = max(samples) if samples else -1
        vram['samples'] = samples
        ps = ollama_ps()
        if ps:
            m = ps[0]
            vram['ps_note'] = '%s: total %.2f GiB, vram %.2f GiB, ctx %s' % (
                m['name'], m['size'] / 2 ** 30, m.get('size_vram', 0) / 2 ** 30,
                m.get('context_length'))
        else:
            vram['ps_note'] = '(已卸载/取不到)'
        vram['marks'].append(('VLM 模型常驻(首次加载后)', max(samples) if samples else -1))
        vram['vlm_steady_delta'] = max(samples) - base_before_load if samples else 0
        meta['struct_block'] = '\n'.join(
            '### %s (%.2f s)\n\n%s\n' % (k, t, fmt_lines(lines)) for k, t, lines in struct_blocks
        ) or '(未测)'

    # ---------------- 打分
    for r in results:
        r['score'] = {}
        r['score']['ocr_full'] = score_group(r['gt_lines'], r['ocr_full_lines'], r['ui_lines'])
        r['score']['ocr_roi'] = score_group(r['gt_lines'], r['ocr_roi_lines'], r['ui_lines'])
        if r.get('vlm'):
            r['score']['vlm'] = score_group(r['gt_lines'], r['vlm_lines'], r['ui_lines'])

    # ---------------- 写产物
    os.makedirs(DOCS_DIR, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M')
    json_path = os.path.join(BENCH_DIR, 'bench_result_%s.json' % stamp)
    meta['json_path'] = json_path
    with open(os.path.join(BENCH_DIR, '_report_meta.json'), 'w', encoding='utf-8') as f:
        json.dump({'meta_keys': list(meta.keys())}, f)

    dump = dict(
        meta={k: v for k, v in meta.items() if k not in ('conclusion', 'verdict')},
        ocr_cold=ocr_cold,
        vram={k: v for k, v in vram.items() if k != 'samples'},
        results=[{k: v for k, v in r.items() if k not in ('roi_b64',)} for r in results])
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(dump, f, ensure_ascii=False, indent=1)
    print('原始结果: %s' % json_path)

    # 报告正文中的定性段落由 --fill 指定的 JSON 提供, 缺省使用脚本内模板
    qual = os.path.join(BENCH_DIR, '_qual.json')
    if os.path.exists(qual):
        with open(qual, encoding='utf-8') as f:
            meta.update(json.load(f))
    else:
        meta.setdefault('conclusion', '_(结论待填)_')
        meta.setdefault('verdict', '_(结论待填)_')
        meta.setdefault('struct_block', '(未测)')

    md = build_report(meta, ocr_cold, results, vram, args)
    md_path = os.path.join(DOCS_DIR, 'bench_danmaku_ocr_%s.md' % stamp)
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(md)
    print('报告: %s' % md_path)
    print('=== 汇总 ===')
    for r in results:
        for k in ('ocr_full', 'ocr_roi', 'vlm'):
            s = r['score'].get(k)
            if s:
                print('  %-6s %-11s hit=%d approx=%d miss=%d extra=%d' % (
                    r['key'], k, len(s['hit']), len(s['approx']), len(s['missed']), len(s['extra'])))

    # 测完让模型自然卸载
    if not args.skip_vlm:
        ollama_unload()
        print('VLM 已卸载, 显存回落至 %d MiB' % gpu_used_mib())


if __name__ == '__main__':
    main()
