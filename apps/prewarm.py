# -*- coding: utf-8 -*-
"""阶段C: 话术预热脚本 — 对给定文本预先合成并写入 TTS 缓存。
用法: python -m apps.prewarm --profile fallback_8g --file data/scripts/opening.txt
"""
import os
import sys
import json
import time
import urllib.request
import argparse

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)


def main():
    ap = argparse.ArgumentParser(description='TTS 缓存预热')
    ap.add_argument('--profile', default=None)
    ap.add_argument('--file', help='文本文件（每行一段话术）')
    ap.add_argument('--text', help='直接传单条文本')
    ap.add_argument('--from-db', action='store_true',
                    help='把库里启用的所有话术按导演的分句规则切好逐句预热（推荐）')
    args = ap.parse_args()

    profile = args.profile or json.load(open(
        os.path.join(_REPO, 'configs', 'default.yaml'), encoding='utf-8'))
    # 读取 tts_gateway 地址
    import yaml
    name = profile.get('default_profile', 'fallback_8g') if isinstance(profile, dict) else args.profile
    cfg = yaml.safe_load(open(os.path.join(_REPO, 'configs', f'profile_{name}.yaml'), encoding='utf-8'))
    host = cfg['tts'].get('listen_host', '127.0.0.1')
    port = cfg['tts'].get('listen_port', 8011)
    url = f'http://{host}:{port}/v1/audio/speech'

    texts = []
    if args.text:
        texts.append(args.text)
    if args.file:
        with open(args.file, encoding='utf-8') as f:
            texts += [line.strip() for line in f if line.strip() and not line.startswith('#')]
    if args.from_db:
        sys.path.insert(0, _REPO)
        from apps import storage
        from apps.director import split_lines
        for sc in storage.list_scripts(enabled_only=True):
            lines = split_lines(sc['content'])
            print(f'话术「{sc["name"]}」切出 {len(lines)} 句')
            texts += lines
    if not texts:
        print('无话术（--from-db / --file / --text）')
        return

    # 去重（重复句子只合成一次）
    seen, uniq = set(), []
    for t in texts:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    texts = uniq
    print(f'共 {len(texts)} 句待预热（CPU 合成 RTF≈10, 请耐心; 已缓存的会秒回）')

    t_all = time.time()
    slow = 0
    for i, t in enumerate(texts):
        t0 = time.time()
        body = json.dumps({'input': t, 'voice': 'default', 'speed': 1.0}).encode()
        req = urllib.request.Request(url, data=body,
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                data = r.read()
        except Exception as e:
            print(f'[{i+1}/{len(texts)}] 失败: {type(e).__name__} {e} — {t[:30]}')
            continue
        cost = time.time() - t0
        if cost > 1.0:
            slow += 1
        print(f'[{i+1}/{len(texts)}] {len(data)} bytes in {cost:.1f}s{" (缓存命中)" if cost < 1.0 else ""}: {t[:30]}')
    print(f'预热完成: {len(texts)} 句, 实际合成 {slow} 句, 总耗时 {(time.time()-t_all)/60:.1f} 分钟')


if __name__ == '__main__':
    main()
