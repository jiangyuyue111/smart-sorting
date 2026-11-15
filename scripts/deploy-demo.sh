#!/usr/bin/env bash
# 把 sorting/ 同步到演示机并重启服务。
#
#   bash scripts/deploy-demo.sh
#
# 前提：本机 ~/.ssh/config 里有 `garden` 这台（腾讯云东京 43.165.190.220）。
# 演示地址：https://jiangbot.com/sort/   识别实况：https://jiangbot.com/sort/recognition
#
# 只同步 sorting/ 这一个包。演示机上跑的是 `--base /sort`，前缀由工作台自己剥掉，
# 页面里的接口全是相对路径，所以换机器、换前缀都不用改代码。
set -euo pipefail

HOST="${HOST:-garden}"
DEST="${DEST:-/opt/sorting-demo}"
URL="${URL:-https://jiangbot.com/sort/}"

cd "$(dirname "$0")/.."

echo "== 打包 sorting/"
tar czf /tmp/sorting-demo.tar.gz --exclude=__pycache__ --exclude='*.pyc' sorting

echo "== 传到 $HOST"
ssh "$HOST" "cat > /tmp/sorting-demo.tar.gz" < /tmp/sorting-demo.tar.gz

echo "== 解包并重启服务"
ssh "$HOST" "cd $DEST && rm -rf sorting && tar xzf /tmp/sorting-demo.tar.gz \
  && sudo systemctl restart sorting-demo && sleep 2 \
  && systemctl is-active sorting-demo"

echo "== 自检（时间轴 == 报告节拍）"
ssh "$HOST" "cd $DEST && ./venv/bin/python -m sorting.app.studio --selftest"

echo "== 公网可达性"
curl -fsS -o /dev/null -w "  $URL -> %{http_code}\n" "$URL"
curl -fsS -o /dev/null -w "  ${URL}recognition -> %{http_code}\n" "${URL}recognition"

echo "完成：$URL"
