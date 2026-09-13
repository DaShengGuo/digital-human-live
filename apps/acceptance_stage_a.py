# -*- coding: utf-8 -*-
"""阶段 A 端到端验收 — 真实文本→编排器→WebRTC 会话→TTS→口型→录制证据。

流程（全部真实链路, 无 mock）:
 1. GET 8020/healthz           确认 phase=live + 会话已绑定, 取真实 sessionid
 2. POST 8010/record start     开始录制该会话
 3. POST 8020/say {text}       经编排器队列派发（真实 sessionid）
 4. 轮询 /is_speaking          等待播报完成
 5. POST /record end + 下载    data/record/<sessionid>.mp4
 6. 证据分析: 时长/分辨率/音频流/口部区域帧间差（说话段 vs 静音段）
 7. 汇总 markdown 验收记录 → docs/acceptance_stage_a.md

用法: python -m apps.acceptance_stage_a  （需先 start.ps1 -Profile fallback）
退出码: 0 功能链路通过（性能另报）; 非 0 失败并给原因。
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.error

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

ORCH = 'http://127.0.0.1:8020'
LT = 'http://127.0.0.1:8010'
TEXT = '各位直播间的朋友大家好，我是李老师，欢迎来到今天的数字人直播测试。'
SPEAK_TIMEOUT_S = 180.0


def _get(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _post(url, payload, timeout=10, raw=False):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        b = r.read()
        return b if raw else json.loads(b.decode())


def download(url, path, timeout=30):
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as r, open(path, 'wb') as f:
        f.write(r.read())
    return os.path.getsize(path)


def analyze_video(mp4: str, out_md: list):
    """ffprobe + 口部区域帧差证据。"""
    import cv2
    # 优先 PATH（云端 Linux 装 ffmpeg 即可）, 回退本地固定路径
    FFPROBE = shutil.which('ffprobe') or r'C:\tools\ffmpeg\bin\ffprobe.exe'
    probe = subprocess.run(
        [FFPROBE, '-v', 'quiet', '-print_format', 'json',
         '-show_format', '-show_streams', mp4],
        capture_output=True, text=True)
    info = json.loads(probe.stdout)
    dur = float(info.get('format', {}).get('duration', 0))
    vstreams = [s for s in info['streams'] if s['codec_type'] == 'video']
    astreams = [s for s in info['streams'] if s['codec_type'] == 'audio']
    ok = True
    out_md.append(f'- 录制时长: {dur:.1f}s（音频应 ≈ 文本朗读时长, 目标 >3s）')
    out_md.append(f'- 视频流: {vstreams[0]["width"]}x{vstreams[0]["height"]} {vstreams[0]["codec_name"]}' if vstreams else '- 视频流: **缺失**')
    out_md.append(f'- 音频流: {astreams[0]["codec_name"]} {astreams[0].get("sample_rate")}Hz' if astreams else '- 音频流: **缺失**')
    if dur < 3 or not vstreams or not astreams:
        ok = False

    # 口部区域帧差: 前半(有语音)vs 尾部(静默收尾) —— 用采样帧比较下巴区域像素差
    cap = cv2.VideoCapture(mp4)
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    idxs = [int(n * f) for f in (0.05, 0.15, 0.3, 0.5, 0.7, 0.9)]
    diffs = []
    prev = None
    for i in idxs:
        cap.set(cv2.CAP_PROP_POS_FRAMES, i)
        ret, fr = cap.read()
        if not ret:
            continue
        h, w = fr.shape[:2]
        mouth = cv2.resize(fr[int(h*0.45):int(h*0.75), int(w*0.2):int(w*0.8)], (64, 32))
        if prev is not None:
            diffs.append(float(cv2.absdiff(mouth, prev).mean()))
        prev = mouth
    cap.release()
    if diffs:
        mx = max(diffs)
        out_md.append(f'- 口部区域帧间差(采样): {["%.2f" % d for d in diffs]}')
        out_md.append(f'- 口部运动证据: 最大帧差 {mx:.2f}（>1.5 判有可见口型变化）')
        if mx < 1.5:
            ok = False
    return ok, dur


def main():
    steps, evidence = [], []
    def step(name, ok, detail=''):
        steps.append((name, ok, detail))
        print(('  OK  ' if ok else '  FAIL') + f' {name}  {detail}')

    # 1. 健康 + 会话
    h = _get(f'{ORCH}/healthz')
    step('编排器 phase=live', h.get('phase') == 'live', f"phase={h.get('phase')} run={h.get('run_id')}")
    sess = (h.get('session') or {})
    sid = sess.get('sessionid')
    step('真实会话已绑定', bool(sid) and sess.get('connected'), f'sessionid={sid} state={sess.get("state")}')
    if not sid:
        _finish(steps, evidence, None)
        return 1
    step('会话非硬编码0', sid != '0', f'（sessionid 应来自 /offer 返回值）')

    # 2. 录制开始
    _post(f'{LT}/record', {'sessionid': sid, 'type': 'start_record'})
    time.sleep(0.5)

    # 3. 派发文本
    say = _post(f'{ORCH}/say', {'text': TEXT})
    step('/say 已受理', say.get('ok') is True, str(say.get('code', '')))
    if not say.get('ok'):
        _post(f'{LT}/record', {'sessionid': sid, 'type': 'end_record'})
        _finish(steps, evidence, None)
        return 1

    # 4. 等播报: is_speaking 先变 true 再变 false
    t0 = time.time()
    spoke = False
    while time.time() - t0 < SPEAK_TIMEOUT_S:
        try:
            sp = _post(f'{LT}/is_speaking', {'sessionid': sid}).get('data')
        except Exception:
            sp = None
        if sp is True:
            spoke = True
        elif sp is False and spoke:
            break
        time.sleep(1)
    step('播报开始（is_speaking=true）', spoke)
    step('播报完成（is_speaking 回 false）', spoke and _post(f'{LT}/is_speaking', {'sessionid': sid}).get('data') is False)

    # 5. 收录制
    time.sleep(1)
    _post(f'{LT}/record', {'sessionid': sid, 'type': 'end_record'})
    time.sleep(6)   # vendor 合成 mp4
    mp4 = os.path.join(_REPO, 'data', 'acceptance', 'stage_a_record.mp4')
    os.makedirs(os.path.dirname(mp4), exist_ok=True)
    try:
        size = download(f'{LT}/record/{sid}', mp4)
        step('录制文件下载', size > 10000, f'{size} bytes')
    except Exception as e:
        step('录制文件下载', False, str(e))
        mp4 = None

    # 6. 接收端帧计数
    h2 = _get(f'{ORCH}/healthz')
    fr = (h2.get('session') or {}).get('frames_received', 0)
    step('WebRTC 视频帧已送达编排器会话', fr > 50, f'frames_received={fr}')

    ok_video = True
    if mp4:
        ok_video, dur = analyze_video(mp4, evidence)
        step('录制含音视频流且口部有运动', ok_video)

    # 7. 打断语义检查
    r = _post(f'{ORCH}/interrupt')
    step('/interrupt 明确返回', r.get('ok') in (True, False) and 'interrupted' in r, str(r))

    _finish(steps, evidence, mp4)
    all_ok = all(o for _, o, _ in steps)
    return 0 if all_ok else 1


def _finish(steps, evidence, mp4):
    lines = ['# 阶段 A 验收记录 — 真实 WebRTC 闭环', '',
             f'- 时间: {time.strftime("%Y-%m-%d %H:%M:%S")}',
             f'- 档位: fallback_8g（wav2lip256 + my_avatar_live + CosyVoice-300M-SFT CPU）',
             f'- 文本: {TEXT}',
             f'- 录制文件: {mp4}', '', '## 结果', '']
    for name, ok, detail in steps:
        lines.append(f'- [{"x" if ok else " "}] {name}' + (f' — {detail}' if detail else ''))
    if evidence:
        lines += ['', '## 证据', ''] + evidence
    lines += ['', '## 性能说明（阶段 A 只证功能）', '',
              '- 功能结果见上表; **性能判定以阶段 C bench 为准**。',
              '- 参考: CPU TTS 历史整句 ~12s（RTF>1）, TTFF 1.5s 目标当前预期**不通过**。', '']
    out = os.path.join(_REPO, 'docs', 'acceptance_stage_a.md')
    with open(out, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print(f'\n验收记录: {out}')


if __name__ == '__main__':
    sys.exit(main())
