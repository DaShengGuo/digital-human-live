#!/usr/bin/env bash
# build_and_debug.sh — gcc 容器内的 C++ 编译 + 运行 + gdb 调试演示
# 用法(宿主): docker run --rm -v "$PWD:/src" -w /src gcc:13 bash build_and_debug.sh
set -e
echo '=== 1. 编译(-g 带调试符号, -Wall 全告警) ==='
g++ -g -O0 -Wall -std=c++17 main.cpp -o imgbin
echo '=== 2. 生成测试图 ==='
python3 make_test_pgm.py test.pgm || py3=skip
[ -f test.pgm ] || (command -v python3 >/dev/null && python3 make_test_pgm.py test.pgm)
echo '=== 3. 正常运行 ==='
./imgbin test.pgm out.pgm
echo '=== 4. gdb 调试: 在 otsu_threshold 断点, 观察 hist/中间量 ==='
gdb -batch \
    -ex 'break otsu_threshold' \
    -ex 'run test.pgm out.pgm' \
    -ex 'print total' \
    -ex 'print hist[40]@8' \
    -ex 'finish' \
    ./imgbin 2>&1 | grep -E 'Breakpoint|\\$[0-9]|Run till|value|Otsu' || true
echo '=== 5. 异常路径验证(不存在文件) ==='
./imgbin no_such.pgm out2.pgm || echo "退出码 $? (预期 1)"
echo DONE
