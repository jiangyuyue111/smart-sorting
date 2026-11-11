#!/bin/sh
# 在 PC 上编译并跑固件的纯逻辑测试。不需要 STM32 工具链。
#
#   sh sorting/firmware/host_test/run.sh
#
# 编译产物丢到 /tmp，不污染仓库（drvfs 上生成可执行文件还容易踩权限坑）。
set -e

HERE=$(cd "$(dirname "$0")/.." && pwd)
OUT=${TMPDIR:-/tmp}/sorting_fw_test

cd "$HERE"
gcc -std=c11 -Wall -Wextra -Werror -O1 \
    -I inc \
    src/proto.c src/motion.c src/machine.c \
    host_test/test_firmware.c \
    -lm -o "$OUT"

"$OUT"
