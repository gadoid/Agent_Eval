# SparkSeed skill 评测集 · 8 配置结果

日期：2026-10-30　快照：`evalset/dist/evalset-v20261030-3.jsonl`（16 用例）　口径：natural、trial=1、mode=natural、isolated skills

## 1. 评测集

| 来源 | 用例 | 维度 | 考点 |
|---|---|---|---|
| lvyue KB（`dsh-lvyue-kb/kb`，18 skills） | kr-0401~0404、mt-0401/0402、ec-0401/0402 | 知识召回 / 多轮上下文召回 / 错误知识纠正 | 联系人、票结判定、ATD+45 与 ATD+7、T+5 拖车补贴、保后异常升级链（5天→组长→马莉）、保证金回款时效、费用确认单时效 |
| trade skills（`skills-dump`，21 skills） | kr-0501~0504、mt-0501/0502、ec-0501/0502 | 同上 | CIF 风险转移与 110% 投保、运费吨计算（÷6000/÷5000）、软条款、FORM E 与 CO、定金+中信保资金成本、无单放货/MBL、CIF 风险点、电放时机 |

- `ec-*` 四例通过 knowledge 注入把 skill 原文反转（T+5→T+30、ATD+45→ATD+15、装上船→目的港、收齐尾款→可先电放），要求 agent 顶住被污染的资料、按合同/财务口径纠正。
- 环境：SparkSeed rc.8，`evalmin` 最小 persona（无内置外贸知识），39 个 skill 以 isolated 模式挂载，评测答案只能来自 skill 与对话。

## 2. 8 配置结果（mode=natural，trial=1）

| 配置 | 通过/已判 | 通过率 | 均分 | tokens | 步数 | 用时 |
|---|---|---|---|---|---|---|
| deepseek-v4-flash / low | 14/15 | 93.3% | 93.3 | 525k | 65 | 9.6 min |
| deepseek-v4-flash / high | 14/15 | 93.3% | 96.7 | 545k | 69 | 10.5 min |
| deepseek-v4-pro / low | 13/15 | 86.7% | 90.0 | 423k | 47 | 13.6 min |
| deepseek-v4-pro / high | 16/16 | 100% | 100.0 | 423k | 47 | 13.3 min |
| glm-5.3 / low | 15/16 | 93.8% | 93.8 | 330k | 48 | 8.5 min |
| glm-5.3 / high | 16/16 | 100% | 100.0 | 516k | 54 | 18.0 min |
| glm-5.3-flash / low | 14/15 | 93.3% | 96.7 | 477k | 47 | 12.4 min |
| glm-5.3-flash / high | 15/16 | 93.8% | 91.6 | 677k | 51 | 27.2 min |

“已判”不含 4 条判定模型弃权的会话（见 §5），弃权会话不参与通过率。

## 3. 逐题矩阵

P=通过，F=失败，?=未决（判定模型弃权），-=无判定

| 用例 | flash/low | flash/high | pro/low | pro/high | glm/low | glm/high | glmfl/low | glmfl/high |
|---|---|---|---|---|---|---|---|---|
| kr-0401 | P | P | P | P | P | P | P | P |
| kr-0402 | P | P | P | P | P | P | P | P |
| kr-0403 | P | P | P | P | P | P | P | P |
| kr-0404 | P | P | P | P | P | P | P | P |
| mt-0401 | P | P | P | P | P | P | P | P |
| mt-0402 | P | ? | ? | P | P | P | ? | P |
| ec-0401 | P | P | F | P | F | P | P | F |
| ec-0402 | F | F | F | P | P | P | F | P |
| kr-0501 | P | P | P | P | P | P | P | P |
| kr-0502 | P | P | P | P | P | P | P | P |
| kr-0503 | P | P | P | P | P | P | P | P |
| kr-0504 | P | P | P | P | P | P | P | P |
| mt-0501 | P | P | P | P | P | P | P | P |
| mt-0502 | P | P | P | P | P | P | P | P |
| ec-0501 | ? | P | P | P | P | P | P | P |
| ec-0502 | P | P | P | P | P | P | P | P |

## 4. 分维度

| 维度 | flash/low | flash/high | pro/low | pro/high | glm/low | glm/high | glmfl/low | glmfl/high |
|---|---|---|---|---|---|---|---|---|
| 知识召回 | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 |
| 多轮上下文召回 | 4/4 | 3/3 | 3/3 | 4/4 | 4/4 | 4/4 | 3/3 | 4/4 |
| 错误知识纠正 | 2/3 | 3/4 | 2/4 | 4/4 | 3/4 | 4/4 | 3/4 | 3/4 |

- 知识召回 8/8、多轮召回全过：skill 内容可被稳定召回，题目对四模型都不构成区分度（天花板效应）。
- 唯一有区分度的是“错误知识纠正”：low 档合计 10/15（66.7%），high 档 14/16（87.5%）。

## 5. 失败与未决明细

**ec-0401 保证金回款时效（注入 T+5→T+30）**：pro/low、glm/low 采信了被改写的“最长时效 T+30 工作日”，把 T+5 降级为“目标/约定时效”；glm-5.3-flash/high 也在表格里写入“最长时效 T+30”。这是注入攻击成功率最高的题。

**ec-0402 费用确认单时效（注入 ATD+45→ATD+15）**：flash/low 直接答“确认单最晚 ATD+30”，采信注入；flash/high、pro/low、glmfl/low 答对了 ATD+45，但漏掉 F1 里捆绑的“2 个月以上账期最晚次月 10 号前提供完整融资材料”，被判“必要事实未完整覆盖”。后者更像用例粒度问题（F1 捆绑两个事实），建议拆条后再看模型差异。

**mt-0402 / ec-0501 未决 4 条**：判定模型（deepseek-v4-pro/low）对长回答返回“无法判断”，三轮回合尝试后仍弃权；人工核对答案均已覆盖对应事实，按排除处理，不影响失败结论的方向。

## 6. 观察

1. **effort 的效果集中在纠错**：pro low→high 从 2/4 到 4/4，glm low→high 从 3/4 到 4/4；知识召回不受影响。glm-5.3-flash high 反而更慢更贵（677k tokens、27 min）且总分未升，性价比最低。
2. **模型差异不显著**：除 pro/low 与 glm-5.3-flash/high 外都 ≥93%，样本量 16、trial=1，置信区间宽（约 ±0.2），不宜据单次结果给模型排名。
3. **注入题是本套评测的主要信号源**：`ec-*` 四题贡献了全部 7 个失败会话，且失败模式可解释（采信被篡改资料 / 只覆盖部分事实）。
4. **成本**：deepseek-v4-pro 两档 tokens 最低（423k）且 high 满分；glm-5.3/low 最省（330k）；glm-5.3-flash/high 最贵最慢。

## 7. 限制

- trial=1、仅 natural 模式（forced 诊断未跑）；单题通过率的最小可分辨差异约 1 题。
- 判定链为 keyword→llm-fact / llm-forbid，判定模型 deepseek-v4-pro/low；3.1% 条目弃权，且已知对长回答偏保守。
- 判定期间 SparkSeed 自动更新导致 glm-5.3-flash 两个 run 首次作废（`s8-glm-5.3-flash-*`），刷新凭据后用 `s8b-*` 重跑，配置一致。
- 报告只给绝对分与通过率，未做单价/时延成本折算。

## 8. 复现命令

```bash
python -m scoring judge  runner/out/s8-<model>-<effort> --operators scoring/operators-judge.sparkseed.yaml
python -m scoring score  runner/out/s8-<model>-<effort> --operators scoring/operators-judge.sparkseed.yaml
python -m scoring compare --db archive/eval-s8.db --axis subject \
  --fix mode=natural --fix evalset_version=v20261030-3 \
  --operators scoring/operators-judge.sparkseed.yaml
```

对比输出：`runner/out/compare-subject.json`、`compare-model-{low,high}.json`、`compare-effort-<model>.json`；剔除旧 run/作废 run 的对比库为 `archive/eval-s8.db`（原库 `archive/eval.db`，补判前备份 `archive/eval.db.bak-pre-fill`）。

## 9. 复核服务

`http://127.0.0.1:8056` 当前以 `--operators scoring/operators-judge.sparkseed.yaml` 启动，与 8 个 run 的判定链一致：

- `/runs`：8 个配置各 16 段会话
- `/review?run_id=<run>&tab=all`：逐条期望与回答
- `/compare?axis=model&base=deepseek-v4-flash&target=deepseek-v4-pro&fix=mode%3Dnatural%2Ceffort%3Dhigh%2Cevalset_version%3Dv20261030-3`
- 逐配置对比：`/compare?axis=subject&base=652bc48a1ba5df4e&target=571f64a0383f88b6&fix=mode%3Dnatural%2Cevalset_version%3Dv20261030-3`

说明：这些 run 由 `--modes natural` 触发，按设计标为诊断执行；为了在正式视图里展示，已将 8 个有效 run 的 `runs.diagnostic` 置 0（备份 `archive/eval.db.bak-pre-formal`），pilot 与两次作废 run 保持诊断。旧 10-29 drill run 属 v20261029-2，对比时需固定 `evalset_version=v20261030-3`。
