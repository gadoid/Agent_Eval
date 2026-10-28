"""scoring 命令行（python -m scoring，评测结果管理改造方案 §10）。

评测库（SQLite，默认 archive/eval.db）是判定与人工数据的唯一事实来源；
原始运行产物是不可变文件，导出层（jsonl/CSV/scoring.json）可随时重新生成。
除 serve 外只依赖标准库与 PyYAML。

命令                作用                                          调用算子
ingest <run>        登记 run、题目、执行时标准、答案（可重复）        否
specs import        导入评测集快照（标准+测法+权重初始值）            否
specs set-current   逐题偏离实验（该题标准改为某版本，标 off_snapshot） 否
snapshot use        把某快照设为当前（清偏离标记）                    否
score <run>         ingest + 按当前标准判定 + 物化结论               是
rejudge             补判缺"当前标准 + 当前链配置"判定的答案           是
rescore [--case]    只重算会话结论表（--case 只重算指定用例）         否
compare --axis …    比较轴：题目 × 轴值通过矩阵（§7）               否
weights list|set    条目权重（item_weights，按用例）                 否
rules show|diff     计分规则                                          否
snapshot list|method 快照与测法                                      否
migrate-verify      v8 迁移对拍（conclusions_legacy vs 新表）         否
export              导出 verdicts/reviews/scoring.json/CSV           否
backup <目标文件>   SQLite 原生备份评测库                            否
import-legacy       一次性导入旧版 verdicts/review/cache jsonl       否
serve               复核服务（依赖可选安装，见 requirements.txt）     否
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

from . import views
from .chain import (VerdictCache, default_cache_path, default_operators_path, episode_id_of,
                    judge_episode, load_operator_config, load_verdicts)
from .effective import load_reviews
from .items import build_context, extract_items, item_sha_of
from .policy import default_policy_path, load_policy, rules_dict_of_policy
from .rules import (EpisodeVerdict, Method, Rules, canonical_sha, default_rules_path,
                    load_rules, rules_from_dict, rules_to_dict)
from .store import Store, StoreError, answer_id_of, evalset_version_of

DEFAULT_DB = "archive/eval.db"


# ---------------------------------------------------------------- 装载（原始层文件）

def load_run(run_dir: str | Path) -> tuple[list[dict], dict | None]:
    p = Path(run_dir)
    records = [json.loads(l) for l in (p / "results.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    manifest = None
    mj = p / "run.json"
    if mj.exists():
        manifest = json.loads(mj.read_text(encoding="utf-8"))
    return records, manifest


def run_id_of(manifest: dict | None, run_dir: str | Path) -> str:
    return (manifest or {}).get("run_id") or Path(run_dir).name


def conclusions(records: list[dict], rows: list[dict], reviews: list, policy,
                routing: dict | None = None) -> list[EpisodeVerdict]:
    """对全部会话计算有效结论 + 口径结论（内存版，import-legacy 对拍与单测用）；
    线上路径见 store.recompute_conclusion。"""
    from .effective import effective_final
    from .items import episode_error
    from .operators import Verdict
    from .rules import session_conclusion

    rows_by_ep: dict[str, dict[str, dict]] = defaultdict(dict)
    for row in rows:
        rows_by_ep[row["episode_id"]][row["item"]["id"]] = row
    reviews_by_ep: dict[str, list] = defaultdict(list)
    for r in reviews:
        reviews_by_ep[r.episode_id].append(r)
    out: list[EpisodeVerdict] = []
    for record in records:
        ctx = build_context(record)
        err = episode_error(record, ctx)
        if err:
            out.append(EpisodeVerdict(uid=record.get("uid", "?"), mode=record.get("mode", "?"),
                                      trial=record.get("trial", 0), dim=(record.get("case") or {}).get("dim", "?"),
                                      status="error", error=err))
            continue
        items = extract_items(ctx)
        ep_rows = rows_by_ep.get(episode_id_of(record), {})
        eff = {}
        statuses: dict[str, str] = {}
        for item in items:
            row = ep_rows.get(item.id)
            if row is None:
                eff[item.id] = Verdict(None, "", "")
                statuses[item.id] = "undecided"
            else:
                eff[item.id] = effective_final(row, reviews_by_ep.get(row["episode_id"], []), row["answer_sha"])
                statuses[item.id] = "ok" if eff[item.id].source == "human" else row["status"]
        out.append(session_conclusion(record, ctx, items, eff, policy, routing, statuses=statuses))
    return out


def operators_of_config(cfg) -> list[str]:
    """链配置的算子清单（判定配置摘要，取自 operators.yaml，§11 修订）。"""
    out = set()
    for chain in cfg.chains.values():
        for op in chain:
            out.add(f"{op.id}@{op.version}")
    return sorted(out)


def open_store(db: str | None) -> Store:
    return Store(db or DEFAULT_DB)


def load_rules_pair(policy_arg: str | None = None) -> tuple[Rules, str]:
    """计分规则装载：--policy 为弃用别名（打警告后把旧 policy.yaml 转成规则加载，
    只转发不另解析一份真相，doc/15 §9）。"""
    if policy_arg:
        print(f"提示：--policy 已由 --rules 取代（一个版本的别名），本次按旧 policy 转换加载：{policy_arg}",
              file=sys.stderr)
        policy, _ = load_policy(policy_arg)
        d = rules_dict_of_policy(policy)
        return rules_from_dict(d), canonical_sha(d)
    return load_rules(default_rules_path())


def current_method(store: Store) -> Method:
    """当前快照的测法（headline 口径随快照，doc/15 §3.6）。"""
    snap = store.current_snapshot()
    if snap:
        row = store.snapshot(snap)
        if row:
            return Method.from_dict(row.get("method"))
    return Method()


def register_chain(store: Store, cfg) -> None:
    """登记判定配置（复核服务的判定配置切换用）：算子清单 + 模型算子的判定模型（实例参数 model）。"""
    models = sorted({str(spec["model"]) for spec in cfg.instance_specs.values() if spec.get("model")})
    has_judge = any(op.source == "judge" for chain in cfg.chains.values() for op in chain)
    store.register_chain(cfg.chain_sha, "规则+模型" if has_judge else "规则", operators_of_config(cfg), models)


class DbCache(VerdictCache):
    """判定缓存读写评测库 verdict_cache 表（§6.2）；键规则与 jsonl 版一致。"""

    def __init__(self, store: Store):
        super().__init__(None)
        self._store = store

    def get(self, key: str):
        if key not in self._map:
            v = self._store.cache_get(key)
            if v is not None:
                self._map[key] = v
        return self._map.get(key)

    def put(self, key: str, v) -> None:
        self._map[key] = v
        self._store.cache_put(key, v)


# ---------------------------------------------------------------- 入库与判定

def ingest_run(store: Store, run_dir: str | Path, records: list[dict] | None = None,
               manifest: dict | None = None) -> tuple[str, int]:
    run_dir = Path(run_dir)
    if records is None:
        records, manifest = load_run(run_dir)
    run_id = run_id_of(manifest, run_dir)
    store.register_run(run_id, run_dir, manifest)
    for record in records:
        store.ingest_answer(record, run_id, manifest)
    return run_id, len(records)


def judge_pending(store: Store, pending: list[dict], cfg, rules: Rules, rules_sha: str,
                  cache: VerdictCache | None = None) -> tuple[int, int]:
    """对待重判答案按当前标准执行算子链并物化结论。"""
    eps = items = 0
    for p in pending:
        a = store.answer(p["answer_id"])
        record = json.loads(a["record_json"])
        spec = store.spec_of(a["qkey"], p["standard_fp"])
        judged = judge_episode(record, cfg, a["run_id"], cache, case=spec)
        if judged.rows:
            store.write_verdicts(a["answer_id"], p["standard_fp"], cfg.chain_sha, judged.rows)
        eps += 1
        items += len(judged.rows)
        store.recompute_conclusion(a["answer_id"], p["standard_fp"], cfg.chain_sha, rules, rules_sha)
    return eps, items


def _print_summary(store: Store, chain_sha: str, rules: Rules, rules_sha: str,
                   run_id: str | None = None) -> None:
    rows = views.formal_rows(store.answer_view_rows(chain_sha, rules_sha))
    if run_id:
        rows = [r for r in rows if r["run_id"] == run_id]
    ability = views.ability_view(views.verdicts_of(rows), rules, current_method(store))
    provisional = any(c.get("undecided") for c in ability.values())
    for cell, m in ability.items():
        mark = "  ← 主口径" if m["headline"] else ""
        prov = "  临时（已排除未决）" if provisional else ""
        avg = "" if m["score_mean"] is None else f"均分 {m['score_mean']}（{m['score_n']} 段）· "
        print(f"  {cell}: {avg}通过 {m['pass']}/{m['n']}"
              f"{'' if m['pass_rate'] is None else f' = {m['pass_rate']}'} 区间 {m['pass_ci']}{mark}{prov}")
    if provisional:
        n_und = sum(c.get("undecided", 0) for c in ability.values())
        print(f"  注意：{n_und} 段会话有未决条目被排除，数字为临时值")
    pending = sum(1 for r in rows if r["c_status"] is None)
    if pending:
        print(f"  注意：{pending} 段会话待重判（python -m scoring rejudge）")


# ---------------------------------------------------------------- 子命令

def cmd_ingest(args) -> int:
    store = open_store(args.db)
    run_id, n = ingest_run(store, args.run)
    print(f"入库 run {run_id}：{n} 段会话 → {store.path}")
    return 0


def cmd_specs_import(args) -> int:
    store = open_store(args.db)
    dist = Path(args.dist)
    lines = [json.loads(l) for l in dist.read_text(encoding="utf-8").splitlines() if l.strip()]
    header = next((r.get("snapshot") for r in lines if isinstance(r, dict) and r.get("snapshot")), None)
    rows = [r for r in lines if isinstance(r, dict) and r.get("uid")]   # 跳过快照头行（P2）
    version = args.version or (header or {}).get("version")
    if not version:
        version = next((r.get("evalset_version") for r in rows if r.get("evalset_version")), "?")
    method = None
    if args.method:
        import yaml
        method = yaml.safe_load(Path(args.method).read_text(encoding="utf-8"))
    else:                                  # 优先读 dist 同名 sidecar（build 产物，doc/15 §3.9）
        sidecar = dist.with_name(dist.stem + ".snapshot.json")
        if sidecar.exists():
            method = (json.loads(sidecar.read_text(encoding="utf-8")) or {}).get("method")
    stats = store.import_evalset(rows, version, keep_weights=args.keep_weights, method=method)
    print(f"导入快照 {stats['questions']} 题（评测集 {version}）→ 已设为当前快照：{store.path}")
    for w in stats.get("weights") or []:
        if w.get("action") != "unchanged":
            print(f"  权重[{w['action']}] {w['case_id']}"
                  + (f"（覆盖 {w['overwrote_manual']} 条手工修改）" if w.get("overwrote_manual") else ""))
    if stats.get("weights_incomplete"):
        print(f"  注意：{len(stats['weights_incomplete'])} 个用例权重不完整（新条目未分配，按 0 计分）："
              + "、".join(stats["weights_incomplete"]))
    return 0


def cmd_specs_set_current(args) -> int:
    store = open_store(args.db)
    try:
        n = store.set_current(args.version, qkey=getattr(args, "question", None))
    except StoreError as e:
        print(str(e))
        return 1
    if getattr(args, "question", None):
        print(f"题目 {args.question} 偏离快照：当前标准改为 {args.version}（恢复对齐用 snapshot use）")
    else:
        print(f"当前快照切换为 {args.version}（{n} 题，等价 snapshot use）")
    return 0


def cmd_score(args) -> int:
    store = open_store(args.db)
    cfg = load_operator_config(args.operators or default_operators_path())
    rules, rules_sha = load_rules_pair(args.policy)
    run_id, n_records = ingest_run(store, args.run)
    register_chain(store, cfg)
    store.register_rules(rules_sha, rules_to_dict(rules))
    pending = store.pending_rejudge(cfg.chain_sha, run_id=run_id)
    no_spec = [a for a in store.answers(run_id=run_id) if store.current_spec(a["qkey"]) is None]
    eps, items = judge_pending(store, pending, cfg, rules, rules_sha, DbCache(store))
    print(f"判定 run {run_id}：{n_records} 段会话，本轮补判 {eps} 段 / {items} 条条目（当前标准 + 链 {cfg.chain_sha[:8]}）")
    if no_spec:
        print(f"  注意：{len(no_spec)} 段会话的题目没有当前标准（先 specs import），未判定")
    _print_summary(store, cfg.chain_sha, rules, rules_sha, run_id=run_id)
    return 0


def cmd_rejudge(args) -> int:
    store = open_store(args.db)
    cfg = load_operator_config(args.operators or default_operators_path())
    rules, rules_sha = load_rules_pair(args.policy)
    register_chain(store, cfg)
    pending = store.pending_rejudge(cfg.chain_sha, qkey=args.question, run_id=args.run)
    if not pending:
        print("没有待重判的答案")
        return 0
    eps, items = judge_pending(store, pending, cfg, rules, rules_sha, DbCache(store))
    print(f"补判 {eps} 段会话 / {items} 条条目（当前标准 + 链 {cfg.chain_sha[:8]}）")
    _print_summary(store, cfg.chain_sha, rules, rules_sha, run_id=args.run)
    return 0


def cmd_rescore(args) -> int:
    store = open_store(args.db)
    cfg = load_operator_config(args.operators or default_operators_path())
    rules, rules_sha = load_rules_pair(args.policy)
    if args.case:
        n = store.rescore_cases(list(args.case), rules, rules_sha)
        print(f"重算 {len(args.case)} 个用例（{n} 组会话结论，未调用任何算子）→ {store.path}")
    else:
        n = store.rescore(rules, rules_sha, cfg.chain_sha)
        print(f"重算 {n} 组会话结论（未调用任何算子）→ {store.path}")
    _print_summary(store, cfg.chain_sha, rules, rules_sha)
    return 0


def cmd_compare(args) -> int:
    store = open_store(args.db)
    cfg = load_operator_config(args.operators or default_operators_path())
    rules, rules_sha = load_rules_pair(args.policy)
    fixes = {}
    for kv in args.fix or []:
        k, _, v = kv.partition("=")
        fixes[k] = v
    rows = store.answer_view_rows(cfg.chain_sha, rules_sha, with_records=True)
    run_order = {r["run_id"]: i for i, r in enumerate(store.runs())}

    if args.base and args.target:
        # 分数对比（doc/10 §5.5）：两侧综合分与维度分、单组件贡献、逐题差异
        rep = views.compare_scores(rows, axis=args.axis, base=args.base, target=args.target,
                                   rules=rules, method=current_method(store), fixes=fixes,
                                   anys=args.any or (), run_order=run_order, delta=args.delta)
        if rep.get("blocked"):
            print(f"已拒绝比较：{rep['blocked']}")
            return 1
        print(f"比较 {args.axis}：{rep['base']} → {rep['target']}（共有 {rep['n_shared']} 题 / "
              f"{rep['n_shared_cases']} 用例）")
        for c in rep["contributions"]:
            ci = f"（95% 区间 {c['lo']:+.1f} ~ {c['hi']:+.1f}）" if c.get("lo") is not None else ""
            base_s = f"{c['base']:.1f}" if c.get("base") is not None else "—"
            tgt_s = f"{c['target']:.1f}" if c.get("target") is not None else "—"
            d = f"{c['delta']:+.1f}" if c.get("delta") is not None else "—"
            print(f"  {c['name']:<12} {base_s:>6} → {tgt_s:<6} {d:>7} {ci}")
        for q in rep["questions"]:
            note = " · 锚点绊线" if q["anchor"] else ""
            print(f"  {q['uid']:<14} {q['base']:.1f} → {q['target']:.1f}（{q['delta']:+.1f}）{note}")
        if args.factors:
            _print_shapley(store, cfg, rules, rows, args)
        return 0

    if args.factors:
        _print_shapley(store, cfg, rules, rows, args)
        return 0
    rep = views.compare_axis(rows, axis=args.axis, fixes=fixes, anys=args.any or (), run_order=run_order)
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    return 0


def _print_shapley(store, cfg, rules: Rules, rows, args) -> None:
    """--factors a,b：全因子各格综合分 + Shapley 分摊（缺格列出需补跑组合）。
    各格取共有题目、按快照测法口径（views.shapley_cells），与对比页同源。"""
    facs = [f.strip() for f in args.factors.split(",") if f.strip()]
    try:
        cells, shared = views.shapley_cells(rows, facs, rules, current_method(store))
    except ValueError as e:
        print(f"{e}，跳过分摊")
        return
    res = views.shapley(cells, facs)
    if res.get("missing"):
        print("缺组合，不能分摊；需补跑：" + "；".join(res["missing"]))
        return
    print(f"  （{len(shared)} 个共有题目）")
    for label, val in res["cells"].items():
        print(f"  {label:<24} 综合分 {val}")
    for f, val in res["phi"].items():
        print(f"  φ({f}) = {val:+.1f}")
    print(f"  合计 = {res['total']:+.1f}")


# ---------------------------------------------------------------- 导出与备份

def _export_flat(store: Store, out: Path, chain_sha: str, rules_sha: str, run_id: str | None) -> None:
    rows = store.answer_view_rows(chain_sha, rules_sha)
    if run_id:
        rows = [r for r in rows if r["run_id"] == run_id]
    out.mkdir(parents=True, exist_ok=True)
    with (out / "verdicts.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            if not r["current_standard_fp"]:
                continue
            for v in store.verdicts(r["answer_id"], r["current_standard_fp"], chain_sha):
                f.write(json.dumps({"answer_id": r["answer_id"], "run_id": r["run_id"],
                                    "episode_id": r["episode_id"], "uid": r["uid"], "qkey": r["qkey"],
                                    "standard_fp": r["current_standard_fp"],
                                    "judge_fp": r["current_standard_fp"],   # 别名（S8，保留一个版本）
                                    "subject_fp": r.get("subject_fp"),
                                    "chain_sha": chain_sha,
                                    "item": {"kind": v["kind"], "id": v["item_id"], "turn": v["turn"]},
                                    "item_sha": v["item_sha"], "answer_sha": v["answer_sha"],
                                    "machine": {"value": v["value"], "source": v["source"],
                                                "operator": v["operator"], "reason": v["reason"],
                                                "score": v["score"]},
                                    "trace": json.loads(v["trace_json"] or "[]"), "status": v["status"]},
                                   ensure_ascii=False) + "\n")
    with (out / "reviews.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            for rv in store.reviews_for(r["answer_id"]):
                f.write(json.dumps(rv, ensure_ascii=False) + "\n")
    with (out / "episodes.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["run_id", "episode_id", "uid", "qkey", "mode", "trial", "dim", "status", "reason",
                    "score", "score_min", "score_max", "score_source",
                    "subject_fp", "profile", "model", "reasoning_effort", "agent",
                    "skills_mode", "skills_base", "evalset_version"])
        for r in rows:
            w.writerow([r["run_id"], r["episode_id"], r["uid"], r["qkey"], r["mode"], r["trial"], r["dim"],
                        r["c_status"] or "待重判", r["c_reason"] or "",
                        r["c_score"], r["c_score_min"], r["c_score_max"], r["c_score_source"] or "",
                        r.get("subject_fp") or r["profile_id"], r["profile_id"],
                        r.get("model") or views.model_label(r), r.get("reasoning_effort"),
                        r.get("agent"), r["skills_mode"], r["skills_base"],
                        r["evalset_version"]])
    with (out / "items.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["answer_id", "uid", "kind", "item_id", "turn", "status", "machine_value",
                    "machine_score", "machine_source", "eff_value", "eff_score", "eff_source", "reason"])
        for r in rows:
            if not r["current_standard_fp"]:
                continue
            for er in store.effective_rows(r["answer_id"], r["current_standard_fp"], chain_sha):
                v, eff = er["row"], er["eff"]
                w.writerow([r["answer_id"], r["uid"], v["kind"], v["item_id"], v["turn"], v["status"],
                            v["value"], v["score"], v["source"], eff.value, eff.score, eff.source, eff.reason])


def _export_bundle(store: Store, out: Path, run_id: str, chain_sha: str,
                   at_run: bool) -> dict:
    """--run --spec at-run：导出为旧 run 目录布局（results.jsonl + run.json +
    verdicts.jsonl + review.jsonl），可被 import-legacy 重新导入（§12 步骤 6 验收）。"""
    run = store.run(run_id)
    if run is None:
        raise SystemExit(f"未知 run：{run_id}")
    records = [json.loads(a["record_json"]) for a in store.answers(run_id=run_id)]
    out.mkdir(parents=True, exist_ok=True)
    with (out / "results.jsonl").open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    (out / "run.json").write_text(run["snapshot_json"], encoding="utf-8")
    with (out / "verdicts.jsonl").open("w", encoding="utf-8") as f:
        for a in store.answers(run_id=run_id):
            fp = a["judge_fp_at_run"] if at_run else (store.current_spec(a["qkey"]) or (None,))[0]
            if fp is None:
                continue
            for v in store.verdicts(a["answer_id"], fp, chain_sha):
                f.write(json.dumps({
                    "schema": 1, "run_id": run_id, "episode_id": a["episode_id"],
                    "uid": _uid_of(records, a["episode_id"]),
                    "item": {"kind": v["kind"], "id": v["item_id"], "turn": v["turn"]},
                    "machine": {"value": v["value"], "source": v["source"],
                                "operator": v["operator"], "reason": v["reason"]},
                    "trace": json.loads(v["trace_json"] or "[]"), "answer_sha": v["answer_sha"],
                    "item_sha": v["item_sha"], "status": v["status"],
                    "_judge_fp": fp, "_chain_sha": chain_sha}, ensure_ascii=False) + "\n")
    with (out / "review.jsonl").open("w", encoding="utf-8") as f:
        for a in store.answers(run_id=run_id):
            for rv in store.reviews_for(a["answer_id"]):
                f.write(json.dumps({"run_id": run_id, "episode_id": a["episode_id"],
                                    "item_id": rv["item_id"], "value": rv["value"],
                                    "answer_sha": rv["answer_sha"], "reason": rv["reason"],
                                    "reviewer": rv["reviewer"], "at": rv["at"], "item_sha": rv["item_sha"],
                                    "schema": 1}, ensure_ascii=False) + "\n")
    return {"run_id": run_id, "records": len(records)}


def _uid_of(records: list[dict], episode_id: str) -> str:
    for r in records:
        if episode_id_of(r) == episode_id:
            return r.get("uid", "?")
    return "?"


def cmd_export(args) -> int:
    store = open_store(args.db)
    cfg = load_operator_config(args.operators or default_operators_path())
    rules, rules_sha = load_rules_pair(args.policy)
    out = Path(args.out) if args.out else Path(DEFAULT_DB).parent / "export"
    if args.run and args.spec == "at-run":
        info = _export_bundle(store, out, args.run, cfg.chain_sha, at_run=True)
        print(f"导出 run {info['run_id']}（执行时标准，bundle 布局）→ {out}")
        return 0
    _export_flat(store, out, cfg.chain_sha, rules_sha, args.run)
    # scoring.json（当前标准 + 当前链 + 当前计分规则下的聚合报告）
    rows = store.answer_view_rows(cfg.chain_sha, rules_sha, with_records=True)
    if args.run:
        rows = [r for r in rows if r["run_id"] == args.run]
    verdicts = views.verdicts_of(rows)
    applied = voided = 0
    for r in rows:
        if r["current_standard_fp"]:
            applied += sum(er["eff"].source == "human"
                           for er in store.effective_rows(r["answer_id"], r["current_standard_fp"],
                                                          cfg.chain_sha))
        voided += len(store.voided_reviews(r["answer_id"]))
    report = {
        "judgment_snapshot": {
            "schema": 4, "chain_sha": cfg.chain_sha, "operators_config_sha": cfg.config_sha,
            "rules_sha": rules_sha, "policy_sha": rules_sha,      # 别名（S8，保留一个版本）
            "operators": operators_of_config(cfg), "standard_fp": "current", "judge_fp": "current",
            "reviews_applied": applied, "reviews_voided": voided,
            "undecided_excluded": sum(v.status == "undecided" for v in verdicts),
        },
        "episodes": len(rows),
        "views": {
            "ability": views.ability_view(verdicts, rules, current_method(store)),
            "attribution": views.attribution_view(verdicts),
            "quality": views.quality_from_db(store, rows, cfg.chain_sha),
        },
        "verdicts": [v.as_dict() for v in verdicts],
    }
    (out / "scoring.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"导出 {len(rows)} 段会话的判定明细与报告（当前标准）→ {out}")
    return 0


def cmd_backup(args) -> int:
    store = open_store(args.db)
    target = store.backup(args.target)
    print(f"备份评测库 → {target}")
    return 0


# ---------------------------------------------------------------- 快照/权重/规则/对拍（doc/15 §3.8）

def cmd_snapshot_list(args) -> int:
    store = open_store(args.db)
    current = store.current_snapshot()
    for s in store.snapshots():
        modes = s["method"].get("modes") or {}
        mark = "  ← 当前" if s["evalset_version"] == current else ""
        n_inc = ""
        incomplete = 0
        for q in store.questions():
            if q["case_id"]:
                _, _, inc = store.effective_weights(q["case_id"])
                incomplete += 1 if inc else 0
        if incomplete:
            n_inc = f"  权重不完整用例 {incomplete}"
        print(f"  {s['evalset_version']}  sha {s['snapshot_sha'][:8]}  测法 {len(modes)} 维"
              f"  {s['questions']} 题{n_inc}{mark}")
    return 0


def cmd_snapshot_method(args) -> int:
    store = open_store(args.db)
    s = store.snapshot(args.version)
    if s is None:
        print(f"未知快照：{args.version}")
        return 1
    print(json.dumps(s["method"], ensure_ascii=False, indent=1))
    return 0


def cmd_snapshot_use(args) -> int:
    store = open_store(args.db)
    try:
        n = store.snapshot_use(args.version)
    except StoreError as e:
        print(str(e))
        return 1
    print(f"当前快照切换为 {args.version}（{n} 题对齐，偏离标记已清）")
    return 0


def cmd_weights_list(args) -> int:
    store = open_store(args.db)
    for r in store.conn.execute(
            "SELECT case_id, item_id, weight, label, source, set_by, set_at, active FROM item_weights"
            " WHERE case_id=? ORDER BY active DESC, set_at, item_id", (args.case,)):
        print(f"  {'有效' if r['active'] else '历史'}  {r['item_id']:<6} {r['weight']:>7.2f}"
              f"  [{r['source']}" + (f"/{r['set_by']}" if r['set_by'] else "") + "]"
              + (f"  {r['label']}" if r['label'] else "") + f"  {r['set_at']}")
    w, wfp, inc = store.effective_weights(args.case)
    if w:
        print(f"  有效权重 → weight_fp {wfp}" + ("（权重不完整）" if inc else ""))
    else:
        print("  该用例无权重行（等权计分，weight_fp=equal）")
    return 0


def cmd_weights_set(args) -> int:
    store = open_store(args.db)
    weights = {}
    for kv in args.items:
        k, _, v = kv.partition("=")
        try:
            weights[k] = float(v)
        except ValueError:
            print(f"权重应为数值：{kv}")
            return 1
    rules, rules_sha = load_rules_pair(None)
    try:
        res = store.revise_weights(args.case, weights, label=args.label or "", by="cli")
    except StoreError as e:
        print(str(e))
        return 1
    n = store.rescore_cases([args.case], rules, rules_sha)
    print(f"已保存 {args.case} 权重（weight_fp {res['weight_fp']}），重算 {n} 组会话结论")
    return 0


def cmd_rules_show(args) -> int:
    rules, rules_sha = load_rules_pair(args.policy)
    print(f"rules_sha {rules_sha}")
    print(json.dumps(rules_to_dict(rules), ensure_ascii=False, indent=1))
    return 0


def cmd_rules_diff(args) -> int:
    rows = {r["rules_sha"]: r for r in open_store(args.db).rules_rows()}
    for sha in (args.a, args.b):
        if sha not in rows:
            print(f"库中没有规则 {sha}（rules show 看当前；snapshot 表存历史）")
            return 1
    a, b = rows[args.a]["rules"], rows[args.b]["rules"]
    keys = sorted(set(a) | set(b))
    for k in keys:
        if a.get(k) != b.get(k):
            print(f"  {k}:")
            print(f"    A {json.dumps(a.get(k), ensure_ascii=False)}")
            print(f"    B {json.dumps(b.get(k), ensure_ascii=False)}")
    return 0


def cmd_migrate_verify(args) -> int:
    """v8 迁移对拍（doc/15 §6 第 7 步）：conclusions_legacy vs 新表逐段比对 + 聚合口径。"""
    store = open_store(args.db)
    rules, rules_sha = load_rules_pair(args.policy)
    has_legacy = store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='conclusions_legacy'").fetchone()
    if not has_legacy:
        print("没有 conclusions_legacy（全新库或已清理），无需对拍")
        return 0
    report = json.loads(store.meta_get("v8_report") or "{}")
    allowed = {d["answer_id"] for d in report.get("discarded_verdict_diff", [])}
    legacy = store.conn.execute(
        "SELECT answer_id, judge_fp, chain_sha, policy_sha, status, reason,"
        " score, score_min, score_max, score_source FROM conclusions_legacy").fetchall()
    n = diffs = 0
    seen: set = set()
    for r in legacy:
        key = (r["answer_id"], r["chain_sha"])
        if key in seen:                       # 旧键下多份期望 → 只对拍一份（迁移已按当前期望优先合并）
            continue
        seen.add(key)
        n += 1
        a = store.answer(r["answer_id"])
        q = store.question(a["qkey"]) if a else None
        new = store.conclusion(r["answer_id"], q["current_standard_fp"], r["chain_sha"], rules_sha) \
            if q and q.get("current_standard_fp") else None
        if new is None:
            print(f"  缺新结论：{r['answer_id']} {r['chain_sha'][:8]}（legacy {r['status']}/{r['score']}）")
            diffs += 1
            continue
        fields = ("status", "score", "score_min", "score_max", "score_source")
        old_v = {f: r[f] for f in fields}
        new_v = {f: new.get(f) for f in fields}
        if old_v != new_v:
            if r["answer_id"] in allowed:
                print(f"  允许差异（合并丢弃判定行）：{r['answer_id']} {old_v} → {new_v}")
                continue
            print(f"  差异：{r['answer_id']} {old_v} → {new_v}")
            diffs += 1
    print(f"对拍 {n} 段会话结论：差异 {diffs}（允许差异不计）")
    if report.get("variant_weight_mismatch"):
        print("  变体权重不一致（以种子为准，需人工确认）："
              + "、".join(report["variant_weight_mismatch"]))
    return 0 if diffs == 0 else 1


# ---------------------------------------------------------------- import-legacy（C14）

def cmd_import_legacy(args) -> int:
    run = Path(args.run)
    records, manifest = load_run(run)
    run_id = run_id_of(manifest, run)
    rules, rules_sha = load_rules_pair(args.policy)
    store = open_store(args.db)
    ingest_run(store, run, records, manifest)
    raw_sha = None
    old = run / "scoring.json"
    if old.exists():
        raw_sha = (json.loads(old.read_text(encoding="utf-8")).get("judgment_snapshot") or {}).get("operators_config_sha")
    # R4：旧记录里的哈希是 operators.yaml 原文哈希；与当前配置原文一致时映射到规范化
    # chain_sha，否则原样保留（不匹配任何当前链 → 按待重判处理，历史不丢）
    chain_sha = raw_sha or "legacy"
    try:
        cur = load_operator_config(default_operators_path())
        if raw_sha and raw_sha == cur.config_sha:
            chain_sha = cur.chain_sha
    except Exception:
        pass

    sha_cache: dict[str, dict[str, str]] = {}          # episode_id → item_id → item_sha
    for record in records:
        ctx = build_context(record)
        sha_cache[episode_id_of(record)] = {i.id: item_sha_of(i) for i in extract_items(ctx)}

    n_verdicts = 0
    try:
        rows = load_verdicts(run)
    except FileNotFoundError:
        rows = []
    by_key: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        a = store.answer(answer_id_of(run_id, row["episode_id"]))
        if a is None:
            continue
        row = dict(row)
        row.setdefault("item_sha", sha_cache.get(row["episode_id"], {}).get(row["item"]["id"]))
        fp = row.get("_judge_fp") or a["judge_fp_at_run"]
        by_key[(a["answer_id"], fp, row.get("_chain_sha") or chain_sha)].append(row)
    for (a_id, fp, ch), rs in by_key.items():
        n_verdicts += store.write_verdicts(a_id, fp, ch, rs)

    # R9：legacy_key 唯一，重复 import-legacy 不再追加改判
    n_reviews = 0
    for r in load_reviews(run):
        a_id = answer_id_of(run_id, r.episode_id)
        if store.answer(a_id) is None:
            continue
        item_sha = getattr(r, "item_sha", None) or sha_cache.get(r.episode_id, {}).get(r.item_id)
        legacy_key = f"{run_id}/{r.episode_id}/{r.item_id}/{getattr(r, 'at', '')}/{r.value}"
        if store.add_legacy_review(a_id, r.item_id, r.value, r.reason or "", r.reviewer or "?",
                                   item_sha, r.answer_sha, legacy_key):
            n_reviews += 1

    # R9：旧版判定缓存是全局文件（scoring/verdict-cache.jsonl），缺省导入它
    n_cache = 0
    cache_path = Path(args.cache) if args.cache else default_cache_path()
    if cache_path.exists():
        n_cache = store.cache_bulk([json.loads(l) for l in
                                    cache_path.read_text(encoding="utf-8").splitlines() if l.strip()])
    elif not args.cache:
        print(f"  提示：未找到判定缓存 {cache_path}（--cache 可指定其他路径）")
    n_concl = store.rescore(rules, rules_sha, chain_sha)
    print(f"导入 run {run_id}：{n_verdicts} 条判定、{n_reviews} 条改判、{n_cache} 条缓存；"
          f"重算 {n_concl} 组结论（链 {chain_sha[:8]}）")
    return 0


def cmd_serve(args) -> int:
    try:
        from .serve import run_server
    except ImportError as e:
        print(f"复核服务需要可选依赖：{e}\n安装：pip install -r scoring/requirements.txt", file=sys.stderr)
        return 1
    rules_arg = args.rules or args.policy      # --policy 为弃用别名（serve 内同规则装载）
    run_server(db=args.db or DEFAULT_DB, host=args.host, port=args.port,
               rules_path=rules_arg, operators_path=args.operators)
    return 0


# ---------------------------------------------------------------- 入口

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="scoring", description="评测库判定与聚合（评测结果管理改造方案 §10）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_db(p):
        p.add_argument("--db", help=f"评测库路径（默认 {DEFAULT_DB}）")

    p = sub.add_parser("score", help="ingest + 按当前期望判定 + 物化结论")
    p.add_argument("run")
    p.add_argument("--operators"); p.add_argument("--policy")
    add_db(p); p.set_defaults(func=cmd_score)

    p = sub.add_parser("ingest", help="登记 run、题目、执行时期望、答案（可重复执行）")
    p.add_argument("run")
    add_db(p); p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("specs", help="评测集期望管理")
    specs = p.add_subparsers(dest="specs_cmd", required=True)
    pi = specs.add_parser("import", help="导入 dist.jsonl 快照（标准+测法+权重初始值）并设为当前")
    pi.add_argument("dist"); pi.add_argument("--version", help="覆盖评测集版本（缺省取行内 evalset_version）")
    pi.add_argument("--method", help="测法文件（缺省 evalset/method.yaml）")
    pi.add_argument("--keep-weights", action="store_true", help="初始权重变化时不覆盖手工修改（doc/15 B3）")
    add_db(pi); pi.set_defaults(func=cmd_specs_import)
    ps = specs.add_parser("set-current", help="逐题偏离实验：该题标准改为某版本（off_snapshot）")
    ps.add_argument("version")
    ps.add_argument("--question", help="限定题目 qkey（不给则整版切换，等价 snapshot use）")
    add_db(ps); ps.set_defaults(func=cmd_specs_set_current)

    p = sub.add_parser("snapshot", help="评测集快照与测法")
    sn = p.add_subparsers(dest="snapshot_cmd", required=True)
    sl = sn.add_parser("list", help="快照清单（版本、sha、测法、题数）")
    add_db(sl); sl.set_defaults(func=cmd_snapshot_list)
    sm = sn.add_parser("method", help="查看某快照的测法")
    sm.add_argument("version"); add_db(sm); sm.set_defaults(func=cmd_snapshot_method)
    su = sn.add_parser("use", help="把某快照设为当前（清偏离标记）")
    su.add_argument("version"); add_db(su); su.set_defaults(func=cmd_snapshot_use)

    p = sub.add_parser("weights", help="条目权重（item_weights，按用例）")
    ws = p.add_subparsers(dest="weights_cmd", required=True)
    wl = ws.add_parser("list", help="查看用例权重行与有效权重")
    wl.add_argument("--case", required=True); add_db(wl); wl.set_defaults(func=cmd_weights_list)
    wse = ws.add_parser("set", help="设置用例权重（如 F1=60 E1=40），随后只重算本用例")
    wse.add_argument("--case", required=True); wse.add_argument("items", nargs="+")
    wse.add_argument("--label"); add_db(wse); wse.set_defaults(func=cmd_weights_set)

    p = sub.add_parser("rules", help="计分规则（rules.yaml）")
    rs = p.add_subparsers(dest="rules_cmd", required=True)
    rsh = rs.add_parser("show", help="当前计分规则与 rules_sha")
    rsh.add_argument("--policy"); add_db(rsh); rsh.set_defaults(func=cmd_rules_show)
    rdf = rs.add_parser("diff", help="比较库中两个规则版本")
    rdf.add_argument("a"); rdf.add_argument("b"); add_db(rdf); rdf.set_defaults(func=cmd_rules_diff)

    p = sub.add_parser("migrate-verify", help="v8 迁移对拍（conclusions_legacy vs 新表）")
    p.add_argument("--policy")
    add_db(p); p.set_defaults(func=cmd_migrate_verify)

    p = sub.add_parser("rejudge", help="补判缺当前期望+链配置判定的答案（只调算子，不跑 agent）")
    p.add_argument("--question", help="限定题目 qkey")
    p.add_argument("--run", help="限定 run")
    p.add_argument("--operators"); p.add_argument("--policy")
    add_db(p); p.set_defaults(func=cmd_rejudge)

    p = sub.add_parser("rescore", help="只重算会话结论表，不调用任何算子")
    p.add_argument("--case", action="append", help="只重算指定用例（可重复）")
    p.add_argument("--operators"); p.add_argument("--policy")
    add_db(p); p.set_defaults(func=cmd_rescore)

    p = sub.add_parser("compare", help="比较轴：题目 × 轴值通过矩阵（§7）；--base/--target 出分数对比")
    p.add_argument("--axis", required=True, choices=views.ATTRS)
    p.add_argument("--base", help="基线轴值（与 --target 同给时输出分数对比与组件贡献）")
    p.add_argument("--target", help="对照轴值")
    p.add_argument("--factors", help="多组件分摊的组件清单（逗号分隔，如 model,skills_base；每组件恰好两值）")
    p.add_argument("--fix", action="append", help="固定属性 k=v，可重复")
    p.add_argument("--any", action="append", choices=views.ATTRS, help="声明不限的属性，可重复")
    p.add_argument("--delta", type=float, default=10.0, help="逐题差异列出的阈值（原 compare_delta）")
    p.add_argument("--operators"); p.add_argument("--policy")
    add_db(p); p.set_defaults(func=cmd_compare)

    p = sub.add_parser("export", help="导出判定明细、改判、scoring.json 与 CSV")
    p.add_argument("--run")
    p.add_argument("--spec", choices=["at-run", "current"], default="current")
    p.add_argument("--out")
    p.add_argument("--operators"); p.add_argument("--policy")
    add_db(p); p.set_defaults(func=cmd_export)

    p = sub.add_parser("backup", help="SQLite 原生备份评测库")
    p.add_argument("target")
    add_db(p); p.set_defaults(func=cmd_backup)

    p = sub.add_parser("import-legacy", help="一次性导入旧版 verdicts/review/cache jsonl")
    p.add_argument("run")
    p.add_argument("--cache", help="额外导入的判定缓存文件（缺省导入全局缓存 scoring/verdict-cache.jsonl）")
    p.add_argument("--policy")
    add_db(p); p.set_defaults(func=cmd_import_legacy)

    p = sub.add_parser("serve", help="复核服务（展示 · 人工评价 · 统计）")
    p.add_argument("--db", help=f"评测库路径（默认 {DEFAULT_DB}）")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--rules"); p.add_argument("--policy", help="弃用别名（转发 --rules）")
    p.add_argument("--operators")
    p.set_defaults(func=cmd_serve)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
