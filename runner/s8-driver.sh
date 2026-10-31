#!/usr/bin/env bash
# 8 配置 × 16 用例 × trials 1 的顺序执行 + 判定入库。
# 每个 run 后做两轮 judge（模型偶发不返回条目，第二轮只补缺）再 score。
set -u
cd D:/Agent_Eval/runner || exit 1
EVALSET=../evalset/dist/evalset-v20261030-3.jsonl
OPS=scoring/operators-judge.sparkseed.yaml
for cfg in deepseek-v4-flash:low deepseek-v4-flash:high \
           deepseek-v4-pro:low deepseek-v4-pro:high \
           glm-5.3:low glm-5.3:high \
           glm-5.3-flash:low glm-5.3-flash:high; do
  m=${cfg%%:*}; e=${cfg##*:}
  out=out/s8-${m}-${e}
  echo "===== $(date '+%F %T') run ${m} ${e} → ${out}"
  node lib/cli.js run --evalset "$EVALSET" --profile profile.sparkseed.yaml \
    --out "$out" --modes natural --trials 1 --model "$m" --reasoning-effort "$e" \
    --anchors off --concurrency 3 2>&1 | tail -25
  cd D:/Agent_Eval || exit 1
  python -m scoring judge "runner/$out" --operators "$OPS" 2>&1 | tail -8
  python -m scoring judge "runner/$out" --operators "$OPS" 2>&1 | tail -8
  python -m scoring score "runner/$out" --operators "$OPS" 2>&1 | tail -8
  cd D:/Agent_Eval/runner || exit 1
done
echo "===== $(date '+%F %T') ALL DONE"
