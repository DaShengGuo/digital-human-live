#!/usr/bin/env bash
# cpp_ci.sh — C++ 能力验证全流程（在 ubuntu:24.04 容器内执行）
# 换 aliyun apt 源 → 装 g++/gdb → 编译(-g -Wall) → 运行 → gdb 断点调试 → 异常路径
set -e
sed -i 's/archive.ubuntu.com/mirrors.aliyun.com/g; s/security.ubuntu.com/mirrors.aliyun.com/g' \
    /etc/apt/sources.list.d/ubuntu.sources
apt-get update -qq >/dev/null 2>&1
apt-get install -y -qq g++ gdb >/dev/null 2>&1
echo "[1] 编译器: $(g++ --version | head -1)"
g++ -g -O0 -Wall -std=c++17 main.cpp -o imgbin
echo "[2] 编译通过(0 告警), 开始运行"
./imgbin test.pgm out.pgm
echo "[3] gdb 调试: otsu_threshold 断点观察入参与中间量"
gdb -batch \
    -ex 'break otsu_threshold' \
    -ex 'run test.pgm out2.pgm' \
    -ex 'print total' \
    -ex 'print hist[40]@6' \
    -ex 'finish' \
    ./imgbin 2>&1 | grep -E 'Breakpoint 1|\\$[0-9]|Run till|Value returned' | head -6
echo "[4] 异常路径: 文件不存在应退出码 1"
./imgbin no_such.pgm x.pgm && echo "意外成功(FAIL)" || echo "退出码 $? (预期 1, 异常处理生效)"
echo "[5] 校验输出图与原图同尺寸"
head -c 20 out.pgm | tr '\n' ' '; echo
echo CPP_CI_DONE
