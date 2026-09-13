# -*- coding: utf-8 -*-
"""配置加载与路径解析（orchestrator / tts_gateway / bench 共用）。

原则：所有模型名、端口、batch、路径只存在于 configs/*.yaml，
业务代码一律通过 load_profile() 读取，禁止硬编码。
"""
import os
import yaml


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIGS_DIR = os.path.join(REPO_ROOT, 'configs')


def resolve_path(base: str, p: str) -> str:
    """把 profile 里的相对路径解析为绝对路径（相对 repo_root）。"""
    if not p:
        return ''
    if os.path.isabs(p):
        return os.path.normpath(p)
    return os.path.normpath(os.path.join(REPO_ROOT, base, p)) if base else os.path.normpath(os.path.join(REPO_ROOT, p))


def load_yaml(path: str) -> dict:
    with open(path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f) or {}


def load_default() -> dict:
    """读 configs/default.yaml（默认档位 + 降级链）。"""
    return load_yaml(os.path.join(CONFIGS_DIR, 'default.yaml'))


def load_profile(name: str = None) -> dict:
    """读档位配置。name 为空时取 default.yaml 的 default_profile。

    返回的 dict 额外带:
      _profile_file: 配置文件绝对路径
      _profile_name: 档位名
    """
    if not name:
        name = load_default().get('default_profile', 'stable_8g')
    fname = name if name.endswith('.yaml') else f'profile_{name}.yaml'
    path = os.path.join(CONFIGS_DIR, fname)
    if not os.path.exists(path):
        raise FileNotFoundError(f'profile 配置不存在: {path}')
    cfg = load_yaml(path)
    cfg['_profile_file'] = path
    cfg['_profile_name'] = name.replace('.yaml', '').replace('profile_', '')
    return cfg


def is_profile_enabled(name: str) -> tuple:
    """检查档位是否被 default.yaml 允许使用。返回 (allowed: bool, reason: str)。"""
    d = load_default()
    if name in d.get('disabled_profiles', {}):
        info = d['disabled_profiles'][name] or {}
        return False, f"{name}: {info.get('reason', 'disabled')}"
    if name in d.get('available_profiles', []):
        return True, ''
    return False, f'{name}: not in available_profiles'


def livetalking_cli_args(cfg: dict) -> list:
    """把 profile 中 livetalking 段转成 LiveTalking app.py 的 CLI 参数。

    采用**白名单**（vendor config.py argparse 真实参数, 2026-09-08 核对）:
    新增编排字段不会意外泄入 CLI 导致 argparse rc=2（曾发生 --avatar_source_id 事故）。
    不在白名单的键一律视为编排器专用, 跳过。
    """
    VENDOR_ARGS = {
        'fps', 'l', 'm', 'r',
        'model', 'avatar_id', 'batch_size', 'modelres', 'modelfile',
        'customvideo_config',
        'tts', 'REF_FILE', 'REF_TEXT', 'TTS_SERVER',
        'llm_provider', 'llm_model',
        'transport', 'stun', 'push_url', 'max_session', 'listenport',
        'audio_output_device', 'config',
    }
    args = []
    lt = cfg.get('livetalking', {})
    for k, v in lt.items():
        if k not in VENDOR_ARGS or v is None or v == '':
            continue
        flag = f'--{k}'
        if isinstance(v, bool):
            if v:
                args.append(flag)
        else:
            args.extend([flag, str(v)])
    return args


def build_env(cfg: dict, which: str) -> dict:
    """合并档位里各进程的 env（在当前环境之上叠加）。which ∈ {livetalking, tts}。"""
    env = dict(os.environ)
    env.update({k: str(v) for k, v in (cfg.get(which, {}) or {}).get('env', {}).items()})
    return env
