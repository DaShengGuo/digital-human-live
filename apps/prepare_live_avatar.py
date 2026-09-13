# -*- coding: utf-8 -*-
"""prepare_live_avatar — 从已有 avatar 生成直播用精简资产（A4 内存预算）。

背景: LiveTalking 在线加载会把 full_imgs 全量读进 RAM。
my_avatar 3721 帧 × 720×1280×3 ≈ 10.3GB — 不可接受。
本脚本取前 N 帧（连续, 保持真实运动速度）生成 <avatar_id>_live 资产,
帧编号与坐标严格一致; 原始资产与素材完全保留。

用法: python -m apps.prepare_live_avatar --profile fallback_8g
配置（livetalking 段）:
  avatar_id: my_avatar          # 源资产
  avatar_live_frames: 375       # 直播资产帧数上限 (15s@25fps)
  avatar_live_id: my_avatar_live
mirror_index 乒乓循环: 第 0 帧首尾相接, 循环处无跳变。
"""
import os
import sys
import glob
import shutil
import pickle
import argparse

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from apps import common_config   # noqa: E402


def _sorted_files(d):
    return sorted(glob.glob(os.path.join(d, '*.[jpJP][pnPN]*[gG]')),
                  key=lambda x: int(os.path.splitext(os.path.basename(x))[0]))


def inspect_avatar(avatar_dir: str) -> dict:
    """对齐校验: 帧数、坐标数、编号连续性、图像尺寸。返回报告 dict。"""
    rep = {'dir': avatar_dir, 'ok': False, 'issues': []}
    full = _sorted_files(os.path.join(avatar_dir, 'full_imgs'))
    faces = _sorted_files(os.path.join(avatar_dir, 'face_imgs'))
    coords_p = os.path.join(avatar_dir, 'coords.pkl')
    if not (full and faces and os.path.exists(coords_p)):
        rep['issues'].append('缺 full_imgs/face_imgs/coords.pkl 之一')
        return rep
    with open(coords_p, 'rb') as f:
        coords = pickle.load(f)
    rep.update(n_full=len(full), n_face=len(faces), n_coords=len(coords))
    idx_full = [int(os.path.splitext(os.path.basename(x))[0]) for x in full]
    if idx_full != list(range(len(full))):
        rep['issues'].append('full_imgs 编号不连续')
    if not (len(full) == len(faces) == len(coords)):
        rep['issues'].append('帧/脸/坐标数量不一致')
    import cv2
    img = cv2.imread(full[0])
    face = cv2.imread(faces[0])
    rep['full_size'] = list(img.shape[:2]) if img is not None else None
    rep['face_size'] = list(face.shape[:2]) if face is not None else None
    y1, y2, x1, x2 = coords[0]
    rep['coord0'] = [y1, y2, x1, x2]
    h, w = img.shape[:2]
    if not (0 <= y1 < y2 <= h and 0 <= x1 < x2 <= w):
        rep['issues'].append(f'坐标越界: {rep["coord0"]} vs 帧 {h}x{w}')
    # 信息项: crop 尺寸 vs face 尺寸（genavatar 会 resize, 不等属正常）
    if face is not None:
        rep['coord_face_match'] = (abs((y2 - y1) - face.shape[0]) <= 2
                                   and abs((x2 - x1) - face.shape[1]) <= 2)
    rep['ram_gb_full'] = round(len(full) * rep['full_size'][0] * rep['full_size'][1] * 3 / 1e9, 2)
    rep['ok'] = not rep['issues']
    return rep


def prepare(cfg: dict) -> dict:
    lt = cfg.get('livetalking', {})
    src_id = lt.get('avatar_source_id', 'my_avatar')      # 源资产（全量帧, 保留不动）
    n_keep = int(lt.get('avatar_live_frames', 375))
    dst_id = lt.get('avatar_live_id', f'{src_id}_live')   # 直播资产（在线 avatar_id 用它）
    av_root = os.path.join(common_config.resolve_path(
        '', cfg['paths'].get('vendor_livetalking', '')), 'data', 'avatars')
    src = os.path.join(av_root, src_id)
    dst = os.path.join(av_root, dst_id)

    rep = inspect_avatar(src)
    if not rep['ok']:
        raise SystemExit(f'源资产检查失败: {rep["issues"]}')
    if n_keep < 25 * 8:
        raise SystemExit(f'avatar_live_frames={n_keep} 太短(<8s 循环), 观感差')

    full = _sorted_files(os.path.join(src, 'full_imgs'))[:n_keep]
    faces = _sorted_files(os.path.join(src, 'face_imgs'))[:n_keep]
    with open(os.path.join(src, 'coords.pkl'), 'rb') as f:
        coords = pickle.load(f)[:n_keep]

    os.makedirs(os.path.join(dst, 'full_imgs'), exist_ok=True)
    os.makedirs(os.path.join(dst, 'face_imgs'), exist_ok=True)
    for x in full:
        shutil.copy2(x, os.path.join(dst, 'full_imgs', os.path.basename(x)))
    for x in faces:
        shutil.copy2(x, os.path.join(dst, 'face_imgs', os.path.basename(x)))
    with open(os.path.join(dst, 'coords.pkl'), 'wb') as f:
        pickle.dump(coords, f)

    out = inspect_avatar(dst)
    out['src'] = rep
    out['dst_id'] = dst_id
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--profile', default='fallback_8g')
    args = ap.parse_args()
    cfg = common_config.load_profile(args.profile)
    out = prepare(cfg)
    print('生成直播资产:', out['dst_id'])
    for k in ('n_full', 'n_face', 'n_coords', 'full_size', 'face_size',
              'coord0', 'ram_gb_full', 'ok', 'issues'):
        print(f'  {k}: {out.get(k)}')


if __name__ == '__main__':
    main()
