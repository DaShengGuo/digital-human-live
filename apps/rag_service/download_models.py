# -*- coding: utf-8 -*-
"""download_models — 按 configs/rag.yaml 预下载 embedding/精排模型。

用法: .venv-rag/Scripts/python.exe -m apps.rag_service.download_models
镜像: 由 scripts/rag_download_models.ps1 设置 HF_ENDPOINT, 本模块不写死。
"""
import argparse

from apps.rag_service.indexer import load_config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='configs/rag.yaml')
    args = parser.parse_args()
    cfg = load_config(args.config)['rag']
    models = [cfg['embedding']['model']]
    if cfg['rerank']['enabled']:
        models.append(cfg['rerank']['model'])
    from huggingface_hub import snapshot_download
    for m in models:
        print(f'下载 {m} ...')
        snapshot_download(m)
        print(f'完成 {m}')


if __name__ == '__main__':
    main()
