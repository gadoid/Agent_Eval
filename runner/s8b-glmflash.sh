#!/usr/bin/env bash
set -u
cd D:/Agent_Eval/runner || exit 1
EVALSET=../evalset/dist/evalset-v20261030-3.jsonl
OPS=scoring/operators-judge.sparkseed.yaml
for cfg in glm-5.3-flash:low glm-5.3-flash:high; do
  m=${cfg%%:*}; e=${cfg##*:}
  out=out/s8b-${m}-${e}
  echo "===== $(date '+%F %T') run ${m} ${e} → ${out}"
  node lib/cli.js run --evalset "$EVALSET" --profile profile.sparkseed.yaml \
    --out "$out" --modes natural --trials 1 --model "$m" --reasoning-effort "$e" \
    --anchors off --concurrency 2 2>&1 | tail -25
  cd D:/Agent_Eval || exit 1
  python -m scoring judge "runner/$out" --operators "$OPS" 2>&1 | tail -8
  python -m scoring judge "runner/$out" --operators "$OPS" 2>&1 | tail -8
  python -m scoring score "runner/$out" --operators "$OPS" 2>&1 | tail -8
  cd D:/Agent_Eval/runner || exit 1
done
echo "===== $(date '+%F %T') GLMFLASH DONE"
