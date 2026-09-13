# -*- coding: utf-8 -*-
"""make_test_pgm — 生成 Otsu 分割测试图: 暗背景 + 两个亮斑 + 渐变噪声。
用法: python make_test_pgm.py out.pgm"""
import sys


def main(path):
    w, h = 128, 96
    rows = bytearray()
    for y in range(h):
        for x in range(w):
            v = 40 + (x * 30 // w)                       # 暗背景 + 轻渐变
            if (x - 40) ** 2 + (y - 30) ** 2 < 300:      # 亮斑1
                v = 220
            if (x - 90) ** 2 + (y - 65) ** 2 < 180:      # 亮斑2
                v = 200
            rows.append(min(255, v))
    with open(path, 'wb') as f:
        f.write(f'P5\n{w} {h}\n255\n'.encode())
        f.write(bytes(rows))
    print(f'已生成 {path} ({w}x{h})')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'test.pgm')
