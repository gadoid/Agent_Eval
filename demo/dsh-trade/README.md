# dsh-trade 演示数据包（2026-10-28 执行 / 2026-10-29 按 doc/17 v9 重建）

本地 D:\dsh（DeepSeek Harness）+ 自装 trade-documents 技能的一套完整测评过程产物，
用于复核服务的快速演示。数据均为合成对话（非真实业务），可直接入库展示。

> **v9 重建说明（2026-10-29）**：评测库按 doc/17 从空库重建——导入 v20261029-1 快照
>（含条目计分配置），回灌原 run，原 llm 判定按 (答案, standard_fp, 链) 迁移（条目指纹
> 不含权重，全部有效）。本机无 dsh，未重跑 llm 判定；在装有 dsh 的机器上可
> `python -m scoring rejudge --operators scoring/operators-judge.yaml --db demo/dsh-trade/eval.db` 重判。

## 一键演示（只看展示）

```bash
python -m scoring serve --db demo/dsh-trade/eval.db \
  --operators scoring/operators-judge.yaml --port 8774
# 打开 http://127.0.0.1:8774
```

看什么：

- **用例管理**：尾款放单主题 6 题的三维度通过率（本次全部 100%）与综合分
- **执行记录**：run「dsh-trade-demo · 尾款放单 v1」（6 段会话，deepseek-flash）
- **复核 → 模型抽检**：队列四标签为空、模型抽检 6 段——机器已全判；
  详情页每条目的机器行显示结论徽章、算子 `llm-fact@1+criteria@1` 与模型理由
- **配置管理 → 判定配置 🔍**：「规则+模型 · 判定模型 deepseek-flash」，
  逐条目类型链（必要/期望 keyword → llm-fact；禁止 llm-forbid；前提人工）
- **判定质量**：算子一致率统计

## 包内清单

| 路径 | 内容 |
| --- | --- |
| `evalset/` | 历史快照 v20261028-dsh（6 题：知识召回×3 / 错误知识纠正×2 含注入 / 多轮×1）及测法 sidecar（v9 演示库已导入 v20261029-1 快照；该 dist 文件不进仓库，演示用库内数据即可） |
| `run/` | 会话执行产物 results.jsonl + run.json（runner `out/dsh-trade-01` 的核心文件） |
| `eval.db` | 评测库（v9）：快照 v20261029-1、run、迁移的模型判定、结论与计分；`eval-v8-backup-20261029.db` 为重建前旧库留档 |
| `agent/trade-documents.md` | 被测技能（安装于 `~/.dsh/skills`，单证与放单规则） |
| `agent/profile.dsh-trade.yaml` | 执行 profile（bundled-copy，注入用隔离技能副本） |
| `agent/evalheadless-trade.cordis.patch.yml` | dsh profile 人设层（单证助理 + 技能路由） |

## 全流程复现（从零重跑）

> **注意**：`evalset/dist/` 已不进 git（原因见仓库 `.gitignore` 里该目录的注释）。要复现本页描述的
> 历史快照 `v20261028-dsh`，需先 `git checkout` 到删除 dist 之前的提交，再执行下面的 `build`；
> 新版本直接用当前 md 编译即可。

```bash
# 0) 安装技能与 profile（本包 agent/ 内有副本）
cp demo/dsh-trade/agent/trade-documents.md ~/.dsh/skills/
cp -r ~/.dsh/profiles/evalheadless ~/.dsh/profiles/evalheadless-trade
cat demo/dsh-trade/agent/evalheadless-trade.cordis.patch.yml \
  >> ~/.dsh/profiles/evalheadless-trade/cordis.patch.yml

# 1) 编译测评集（用例源在 evalset/cases/payment_release/）
cd evalset && python md2case.py check cases/payment_release --docs ~/.dsh/skills
python md2case.py build cases/payment_release -o dist/evalset-v20261028-dsh.jsonl \
  --version v20261028-dsh --include structured,reviewed,admitted --db ../archive/eval-dsh.db

# 2) 执行会话（D:\dsh 无头，真实计费）
cd ../runner && node lib/cli.js run \
  --evalset ../evalset/dist/evalset-v20261028-dsh.jsonl \
  --profile profile.dsh-trade.yaml --out out/dsh-trade-01 --trials 1

# 3) 导入快照 → 模型批量评审（dsh 单发，判据契约）→ 计分入库
cd .. && python -m scoring specs import evalset/dist/evalset-v20261028-dsh.jsonl --db archive/eval-dsh.db
python -m scoring judge runner/out/dsh-trade-01 --operators scoring/operators-judge.yaml --db archive/eval-dsh.db
python -m scoring score runner/out/dsh-trade-01 --operators scoring/operators-judge.yaml --db archive/eval-dsh.db

# 4) 展示（同一键演示）
python -m scoring serve --db archive/eval-dsh.db --operators scoring/operators-judge.yaml
```

判定链配置 `scoring/operators-judge.yaml` 含本机路径（D:/dsh、~/.dsh），换机器时改
`dsh_bin`/`dsh_home`/`dsh_profile` 三个参数即可。本次结果：三维度 100%——agent 在
电放时机删除、单单一致弱化两处知识注入下均守住了规则。
