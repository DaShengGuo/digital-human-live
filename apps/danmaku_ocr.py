# -*- coding: utf-8 -*-
"""danmaku_ocr — 从「抖音直播伴侣」窗口读弹幕, 走 OCR 送进弹幕管线（阶段G）。

为什么是独立进程 + 独立解释器:
  项目 venv（.venv-lt）里没有 rapidocr, 而本机 pip 通道不可用（代理坏）。
  D:\\Python39 里已装 rapidocr 3.9.2 + onnxruntime(CPU) + Pillow + pywin32,
  所以本模块由「配置里指定的 OCR 解释器」运行, 通过 HTTP 把结果 POST 给控制台
  （source=ocr），与控制台/编排器解耦。**不占显存**（onnxruntime 仅 CPU EP）。

选型依据: docs/bench_danmaku_ocr_*.md —— OCR 0 显存 / 裁 ROI 后 0.56~1.3s / 召回≈92%;
通用视觉模型(qwen2.5vl:7b) 加载即占 5.13GB、与数字人共存时单图 179s, 8G 卡直接排除。

用法（一般由控制台拉起）:
  D:\\Python39\\python.exe -X utf8 apps\\danmaku_ocr.py --profile fallback_8g
  ... --dry-run            # 只打印识别结果, 不 POST（标定 ROI 时用）
  ... --once               # 只跑一轮就退出
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request

# ── 必须在导入任何第三方库之前做 ──────────────────────────────────────────
# 以 `python apps\danmaku_ocr.py` 方式运行时, sys.path[0] 是 apps/ 目录, 而
# apps/platform.py 会把**标准库 platform** 顶掉（同名遮蔽）→ rapidocr 内部
# `import platform` 拿到我们的模块, 连锁 ImportError:
#   "cannot import name 'RapidOCR' from 'rapidocr'"
# 所以先把这个目录从 sys.path 摘掉, 再把仓库根放进去（apps 走包名导入）。
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
while _HERE in sys.path:
    sys.path.remove(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

CONF_MIN_DEFAULT = 0.60
DEDUPE_TTL_DEFAULT = 60.0

# 直播伴侣界面上的固定文案/噪声行（实测归纳, 命中即丢）
# 注意: 这里用「包含即丢」, 所以只放**足够长、不会出现在真实评论里**的串;
#       短 UI 标签（"已关注"/"分享"…）走 UI_EXACT 精确匹配, 免得把"我关注你很久了"也丢掉。
UI_NOISE = (
    '直播伴侣', '抖音直播', '开播中', '下播', '直播设置', '帮助中心', '意见反馈',
    '管理员', '粉丝团', '在线人数', '观看人数', '排行榜', '本场音浪',
    '欢迎来到直播间', '文明直播', '健康直播',
    '已将剪贴板中的内容粘贴到抖音', '说点什么',
)
# 整行 == 这些标签时丢弃（OCR 常把按钮文字一起读进来）
UI_EXACT = {
    '已关注', '关注', '分享', '复制', '举报', '清屏', '更多', '收起', '展开',
    '搜索', '设置', '帮助', '反馈', '发送', '礼物', '榜单', '点赞', '弹幕',
    '直播间', '评论', '连麦', '上热门', '开播', '下播',
}
_RE_TIME = re.compile(r'^\s*\d{1,2}:\d{2}(:\d{2})?\s*$')
# 界面上的计数器行: "点赞 1.2万" / "在线人数 345" / "音浪 8.8万" —— 数字+标签=UI, 不是弹幕
_RE_UI_COUNTER = re.compile(
    r'^\s*(点赞|在线|观看|人数|音浪|粉丝|礼物|关注|收礼)\s*[\d.,]+\s*[万亿千百]?\+?\s*$')
_RE_DIGITS = re.compile(r'^[\d\s.,%+万千百亿:：\-—/]+$')
_RE_NICK_SPLIT = re.compile(r'^(.{1,24}?)\s*[：:]\s*(.+)$')


def log(msg: str):
    print(f'[{time.strftime("%H:%M:%S")}] {msg}', flush=True)


# ── 配置 ────────────────────────────────────────────────────────────────────
def load_cfg(profile: str):
    """复用项目配置加载（只依赖 common_config, 与 OCR 解释器无关）。"""
    from apps import common_config
    return common_config.load_profile(profile), _ROOT


# ── 取词器 ──────────────────────────────────────────────────────────────────
def _grab_window(hwnd):
    """用 PrintWindow(PW_RENDERFULLCONTENT) 抓窗口自身画面（被遮挡也能抓到）。

    直播伴侣是 CEF 窗口, 最小化时 PrintWindow 会返回黑图 → 调用方需回退屏幕截图。
    """
    import win32gui
    import win32ui
    from ctypes import windll
    from PIL import Image
    try:
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        w, h = r - l, b - t
        hdc = win32gui.GetWindowDC(hwnd)
        mfc = win32ui.CreateDCFromHandle(hdc)
        save = mfc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(mfc, w, h)
        save.SelectObject(bmp)
        windll.user32.PrintWindow(hwnd, save.GetSafeHdc(), 2)   # 2 = PW_RENDERFULLCONTENT
        info = bmp.GetInfo()
        img = Image.frombuffer('RGB', (info['bmWidth'], info['bmHeight']),
                               bmp.GetBitmapBits(True), 'raw', 'BGRX', 0, 1)
        win32gui.DeleteObject(bmp.GetHandle())
        save.DeleteDC()
        mfc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hdc)
        return img
    except Exception:
        return None


class WindowGrabber:
    """按标题找到直播伴侣窗口并截取弹幕区域。"""

    def __init__(self, title_kw: str, roi, logger=log):
        self.title_kw = title_kw
        self.roi = roi                     # [x, y, w, h] 相对客户区的比例
        self.log = logger
        self.hwnd = None

    def _find(self):
        import win32gui

        hit = []
        minimized = []

        def cb(h, _):
            if not win32gui.IsWindowVisible(h):
                return
            t = win32gui.GetWindowText(h)
            if self.title_kw and self.title_kw in t:
                if win32gui.IsIconic(h):
                    minimized.append(h)
                    return
                r = win32gui.GetWindowRect(h)
                if r[2] - r[0] > 300 and r[3] - r[1] > 300:
                    hit.append(h)

        win32gui.EnumWindows(cb, None)
        self.hwnd = hit[0] if hit else None
        self.minimized_found = bool(minimized) and not hit
        return self.hwnd

    def grab(self):
        """返回 (PIL.Image, 说明) ; 窗口不可用时返回 (None, 原因)。"""
        import win32gui
        from PIL import ImageGrab

        if not self.hwnd or not win32gui.IsWindow(self.hwnd):
            if not self._find():
                if getattr(self, 'minimized_found', False):
                    return None, f'「{self.title_kw}」窗口处于最小化（请恢复窗口后重试）'
                return None, f'未找到标题含「{self.title_kw}」的窗口'
        if win32gui.IsIconic(self.hwnd):
            return None, '直播伴侣窗口已最小化（请保持窗口可见）'
        l, t, r, b = win32gui.GetWindowRect(self.hwnd)
        w, h = r - l, b - t
        x, y, rw, rh = self.roi
        cx, cy, cx2, cy2 = (int(w * x), int(h * y),
                            int(w * (x + rw)), int(h * (y + rh)))
        if cx2 - cx < 40 or cy2 - cy < 40:
            return None, f'ROI 太小: {(cx, cy, cx2, cy2)}'

        # 先抓窗口自身（被遮挡也有效）; 抓到的是黑图/纯色(最小化或渲染未就绪)再退回屏幕截图
        img = _grab_window(self.hwnd)
        if img is not None:
            try:
                import numpy as np
                sub = np.asarray(img.crop((cx, cy, cx2, cy2)))
                if sub.std() > 1.0:
                    return img.crop((cx, cy, cx2, cy2)), f'PrintWindow 窗口{(l, t, r, b)}'
            except Exception:
                pass
        box = (l + cx, t + cy, l + cx2, t + cy2)
        return ImageGrab.grab(bbox=box), f'屏幕截图{(l, t, r, b)} ROI{box}'


class Ocr:
    """rapidocr 封装（惰性初始化, 冷启动约 0.3s）。"""

    def __init__(self):
        from rapidocr import RapidOCR
        self.engine = RapidOCR()

    def read(self, pil_img):
        """返回 [(text, score), ...]（按 y 排序, 保持阅读顺序）。"""
        import numpy as np
        res = self.engine(np.array(pil_img))
        out = []
        # rapidocr 3.x: res.boxes / res.txts / res.scores
        txts = getattr(res, 'txts', None)
        scores = getattr(res, 'scores', None)
        boxes = getattr(res, 'boxes', None)
        if txts is None:      # 兼容老版 tuple 返回
            _, items = res if isinstance(res, tuple) else (None, res)
            for it in items or []:
                out.append((str(it[1]), float(it[2])))
            return out
        rows = list(zip(txts, scores, boxes if boxes is not None else [None] * len(txts)))
        rows.sort(key=lambda r: (r[2][0][1] if r[2] is not None else 0))
        for t, s, _ in rows:
            out.append((str(t), float(s)))
        return out


# ── 过滤 ────────────────────────────────────────────────────────────────────
class LineFilter:
    def __init__(self, min_conf=CONF_MIN_DEFAULT, dedupe_ttl=DEDUPE_TTL_DEFAULT):
        self.min_conf = min_conf
        self.dedupe_ttl = dedupe_ttl
        self.seen = {}

    @staticmethod
    def _norm(s: str) -> str:
        return re.sub(r'[\s\W_]+', '', s or '').lower()

    def accept(self, text: str, score: float):
        """返回 (user, text) 或 None（被过滤）。"""
        t = (text or '').strip()
        if not t or score < self.min_conf:
            return None
        if len(self._norm(t)) <= 2:
            return None
        if _RE_TIME.match(t) or _RE_DIGITS.match(t) or _RE_UI_COUNTER.match(t):
            return None
        if any(k in t for k in UI_NOISE) or t.strip('　 .,，。!！?？') in UI_EXACT:
            return None
        key = self._norm(t)
        now = time.time()
        last = self.seen.get(key)
        if last and now - last < self.dedupe_ttl:
            return None
        self.seen[key] = now
        # 过期清理
        if len(self.seen) > 2000:
            self.seen = {k: v for k, v in self.seen.items() if now - v < self.dedupe_ttl}
        m = _RE_NICK_SPLIT.match(t)
        if m:
            return m.group(1).strip(), m.group(2).strip()
        return '', t


# ── 主循环 ──────────────────────────────────────────────────────────────────
def post_ingest(url: str, user: str, text: str, timeout=5.0):
    body = json.dumps({'source': 'ocr', 'user': user, 'text': text}).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def write_status(here: str, **kw):
    path = os.path.join(here, 'logs', 'danmaku_ocr.status.json')
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        prev = {}
        if os.path.isfile(path):
            try:
                prev = json.load(open(path, encoding='utf-8'))
            except Exception:
                prev = {}
        prev.update(kw)
        prev['ts'] = time.time()
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(prev, f, ensure_ascii=False)
    except OSError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--profile', default='fallback_8g')
    ap.add_argument('--dry-run', action='store_true', help='只打印, 不 POST')
    ap.add_argument('--once', action='store_true')
    ap.add_argument('--calibrate', action='store_true',
                    help='截一张窗口图并在图上画出当前 ROI, 存到 data/out/danmaku_roi.png 便于标定')
    args = ap.parse_args()

    cfg, here = load_cfg(args.profile)
    oc = (cfg.get('danmaku_ocr') or {})
    title_kw = oc.get('window_title', '直播伴侣')
    roi = oc.get('roi', [0.02, 0.30, 0.35, 0.60])
    interval = float(oc.get('interval_s', 1.5))
    ingest = oc.get('ingest_url', 'http://127.0.0.1:8030/danmaku/ingest')

    log(f'启动: 窗口关键字=「{title_kw}」 ROI={roi} 间隔={interval}s '
        f'{"(dry-run)" if args.dry_run else ""}')
    grabber = WindowGrabber(title_kw, roi)

    if args.calibrate:
        from PIL import ImageDraw
        import win32gui
        if not grabber._find():
            log(f'未找到标题含「{title_kw}」的窗口 —— 请先把直播伴侣恢复到可见状态')
            return 2
        l, t, r, b = win32gui.GetWindowRect(grabber.hwnd)
        shot = _grab_window(grabber.hwnd)
        if shot is None:
            log('截窗口失败')
            return 2
        x, y, rw, rh = roi
        box = (int((r - l) * x), int((b - t) * y),
               int((r - l) * (x + rw)), int((b - t) * (y + rh)))
        d = ImageDraw.Draw(shot)
        d.rectangle(box, outline=(255, 0, 0), width=3)
        outdir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              'data', 'out')
        os.makedirs(outdir, exist_ok=True)
        out = os.path.join(outdir, 'danmaku_roi.png')
        shot.save(out)
        log(f'窗口尺寸={r - l}x{b - t} 当前 ROI(窗口内像素)={box}')
        log(f'已保存 {out} —— 红框就是 OCR 取词区域; 调整配置 danmaku_ocr.roi 后重跑本命令')
        return 0

    flt = LineFilter(float(oc.get('min_confidence', CONF_MIN_DEFAULT)),
                     float(oc.get('dedupe_ttl_s', DEDUPE_TTL_DEFAULT)))
    ocr = Ocr()
    log('OCR 引擎就绪 (rapidocr, CPU)')

    total = 0
    while True:
        t0 = time.time()
        img, why = grabber.grab()
        if img is None:
            log(f'取图失败: {why}')
            write_status(here, running=True, window_ok=False, note=why,
                         lines_total=total)
            if args.once:
                return 2
            time.sleep(max(2.0, interval))
            continue
        try:
            items = ocr.read(img)
        except Exception as e:
            log(f'OCR 异常: {type(e).__name__} {e}')
            if args.once:
                return 3
            time.sleep(max(2.0, interval))
            continue
        new = 0
        for text, score in items:
            got = flt.accept(text, score)
            if not got:
                continue
            user, body = got
            if args.dry_run:
                log(f'  [识别] user={user!r} text={body!r} score={score:.2f}')
            else:
                try:
                    post_ingest(ingest, user, body)
                except Exception as e:
                    log(f'  投递失败: {type(e).__name__} {e}')
                    continue
                log(f'  → 弹幕: {user} {body}')
            new += 1
            total += 1
        cost = time.time() - t0
        write_status(here, running=True, window_ok=True, note='',
                     lines_total=total, lines_last_cycle=new,
                     last_cycle_s=round(cost, 2), last_text=(items[-1][0] if items else ''))
        if args.once:
            break
        time.sleep(max(0.2, interval - cost))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('用户中断')
