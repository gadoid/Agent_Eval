"""scoring 命令行（python -m scoring，评测结果管理改造方案 §10）。

评测库（SQLite，默认 archive/eval.db）是判定与人工数据的唯一事实来源；
原始运行产物是不可变文件，导出层（jsonl/CSV/scoring.json）可随时重新生成。
除 serve 外只依赖标准库与 PyYAML。

命令                作用                                          调用算子
ingest <run>        登记 run、题目、执行时期望、答案（可重复）      否
specs import        导入评测集期望，更新当前期望                    否
specs set-current   指定某评测集版本的期望为当前期望                否
score <run>         ingest + 按当前期望判定 + 物化结论              是
rejudge             补判缺"当前期望 + 当前链配置"判定的答案          是
rescore             只重算会话结论表                                否
compare --axis …    比较轴：题目 × 轴值通过矩阵（§7）               否
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
from .chain import (VerdictCache, default_operators_path, episode_id_of, judge_episode,
                    load_operator_config, load_verdicts)
from .effective import load_reviews
from .items import build_context, extract_items, item_sha_of
from .policy import EpisodeVerdict, default_policy_path, load_policy
from .store import Store, answer_id_of, evalset_version_of

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
    from .policy import session_conclusion

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
        for item in items:
            row = ep_rows.get(item.id)
            eff[item.id] = (effective_final(row, reviews_by_ep.get(row["episode_id"], []), row["answer_sha"])
                            if row else Verdict(None, "", ""))
        out.append(session_conclusion(record, ctx, items, eff, policy, routing))
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


def judge_pending(store: Store, pending: list[dict], cfg, policy, policy_sha: str,
                  cache: VerdictCache | None = None) -> tuple[int, int]:
    """对待重判答案按当前期望执行算子链并物化结论。"""
    eps = items = 0
    for p in pending:
        a = store.answer(p["answer_id"])
        record = json.loads(a["record_json"])
        spec = store.spec_of(a["qkey"], p["judge_fp"])
        judged = judge_episode(record, cfg, a["run_id"], cache, case=spec)
        if judged.rows:
            store.write_verdicts(a["answer_id"], p["judge_fp"], cfg.config_sha, judged.rows)
        eps += 1
        items += len(judged.rows)
        store.recompute_conclusion(a["answer_id"], p["judge_fp"], cfg.config_sha, policy, policy_sha)
    return eps, items


def _print_summary(store: Store, chain_sha: str, policy, policy_sha: str, run_id: str | None = None) -> None:
    rows = store.answer_view_rows(chain_sha, policy_sha)
    if run_id:
        rows = [r for r in rows if r["run_id"] == run_id]
    ability = views.ability_view(views.verdicts_of(rows), policy)
    provisional = any(c.get("undecided") for c in ability.values())
    for cell, m in ability.items():
        mark = "  ← 主口径" if m["headline"] else ""
        prov = "  临时（已排除未决）" if provisional else ""
        print(f"  {cell}: 通过 {m['pass']}/{m['n']}"
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
    rows = [json.loads(l) for l in Path(args.dist).read_text(encoding="utf-8").splitlines() if l.strip()]
    version = args.version
    if not version:
        version = next((r.get("evalset_version") for r in rows if r.get("evalset_version")), "?")
    stats = store.import_evalset(rows, version)
    print(f"导入期望 {stats['questions']} 题（评测集 {version}）→ 已设为当前期望：{store.path}")
    return 0


def cmd_specs_set_current(args) -> int:
    store = open_store(args.db)
    n = store.set_current(args.version)
    if n == 0:
        print(f"评测集中没有期望：{args.version}")
        return 1
    print(f"当前期望切换为 {args.version}（{n} 题）")
    return 0


def cmd_score(args) -> int:
    store = open_store(args.db)
    cfg = load_operator_config(args.operators or default_operators_path())
    policy, policy_sha = load_policy(args.policy or default_policy_path())
    run_id, n_records = ingest_run(store, args.run)
    pending = store.pending_rejudge(cfg.config_sha, run_id=run_id)
    no_spec = [a for a in store.answers(run_id=run_id) if store.current_spec(a["qkey"]) is None]
    eps, items = judge_pending(store, pending, cfg, policy, policy_sha, DbCache(store))
    print(f"判定 run {run_id}：{n_records} 段会话，本轮补判 {eps} 段 / {items} 条条目（当前期望 + 链 {cfg.config_sha[:8]}）")
    if no_spec:
        print(f"  注意：{len(no_spec)} 段会话的题目没有当前期望（先 specs import），未判定")
    _print_summary(store, cfg.config_sha, policy, policy_sha, run_id=run_id)
    return 0


def cmd_rejudge(args) -> int:
    store = open_store(args.db)
    cfg = load_operator_config(args.operators or default_operators_path())
    policy, policy_sha = load_policy(args.policy or default_policy_path())
    pending = store.pending_rejudge(cfg.config_sha, qkey=args.question, run_id=args.run)
    if not pending:
        print("没有待重判的答案")
        return 0
    eps, items = judge_pending(store, pending, cfg, policy, policy_sha, DbCache(store))
    print(f"补判 {eps} 段会话 / {items} 条条目（当前期望 + 链 {cfg.config_sha[:8]}）")
    _print_summary(store, cfg.config_sha, policy, policy_sha, run_id=args.run)
    return 0


def cmd_rescore(args) -> int:
    store = open_store(args.db)
    cfg = load_operator_config(args.operators or default_operators_path())
    policy, policy_sha = load_policy(args.policy or default_policy_path())
    n = store.rescore(policy, policy_sha, cfg.config_sha)
    print(f"重算 {n} 组会话结论（未调用任何算子）→ {store.path}")
    _print_summary(store, cfg.config_sha, policy, policy_sha)
    return 0


def cmd_compare(args) -> int:
    store = open_store(args.db)
    cfg = load_operator_config(args.operators or default_operators_path())
    _, policy_sha = load_policy(args.policy or default_policy_path())
    fixes = {}
    for kv in args.fix or []:
        k, _, v = kv.partition("=")
        fixes[k] = v
    rows = store.answer_view_rows(cfg.config_sha, policy_sha, with_records=True)
    run_order = {r["run_id"]: i for i, r in enumerate(store.runs())}
    rep = views.compare_axis(rows, axis=args.axis, fixes=fixes, anys=args.any or (), run_order=run_order)
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    return 0


# ---------------------------------------------------------------- 导出与备份

def _export_flat(store: Store, out: Path, chain_sha: str, policy_sha: str, run_id: str | None) -> None:
    rows = store.answer_view_rows(chain_sha, policy_sha)
    if run_id:
        rows = [r for r in rows if r["run_id"] == run_id]
    out.mkdir(parents=True, exist_ok=True)
    with (out / "verdicts.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            if not r["current_judge_fp"]:
                continue
            for v in store.verdicts(r["answer_id"], r["current_judge_fp"], chain_sha):
                f.write(json.dumps({"answer_id": r["answer_id"], "run_id": r["run_id"],
                                    "episode_id": r["episode_id"], "uid": r["uid"], "qkey": r["qkey"],
                                    "judge_fp": r["current_judge_fp"], "chain_sha": chain_sha,
                                    "item": {"kind": v["kind"], "id": v["item_id"], "turn": v["turn"]},
                                    "item_sha": v["item_sha"], "answer_sha": v["answer_sha"],
                                    "machine": {"value": v["value"], "source": v["source"],
                                                "operator": v["operator"], "reason": v["reason"]},
                                    "trace": json.loads(v["trace_json"] or "[]"), "status": v["status"]},
                                   ensure_ascii=False) + "\n")
    with (out / "reviews.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            for rv in store.reviews_for(r["answer_id"]):
                f.write(json.dumps(rv, ensure_ascii=False) + "\n")
    with (out / "episodes.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["run_id", "episode_id", "uid", "qkey", "mode", "trial", "dim", "status", "reason",
                    "profile", "model", "skills_mode", "skills_base", "evalset_version"])
        for r in rows:
            w.writerow([r["run_id"], r["episode_id"], r["uid"], r["qkey"], r["mode"], r["trial"], r["dim"],
                        r["c_status"] or "待重判", r["c_reason"] or "", r["profile_id"],
                        views.model_label(r), r["skills_mode"], r["skills_base"], r["evalset_version"]])
    with (out / "items.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["answer_id", "uid", "kind", "item_id", "turn", "status", "machine_value",
                    "machine_source", "eff_value", "eff_source", "reason"])
        for r in rows:
            if not r["current_judge_fp"]:
                continue
            for er in store.effective_rows(r["answer_id"], r["current_judge_fp"], chain_sha):
                v, eff = er["row"], er["eff"]
                w.writerow([r["answer_id"], r["uid"], v["kind"], v["item_id"], v["turn"], v["status"],
                            v["value"], v["source"], eff.value, eff.source, eff.reason])


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
    policy, policy_sha = load_policy(args.policy or default_policy_path())
    out = Path(args.out) if args.out else Path(DEFAULT_DB).parent / "export"
    if args.run and args.spec == "at-run":
        info = _export_bundle(store, out, args.run, cfg.config_sha, at_run=True)
        print(f"导出 run {info['run_id']}（执行时期望，bundle 布局）→ {out}")
        return 0
    _export_flat(store, out, cfg.config_sha, policy_sha, args.run)
    # scoring.json（当前期望 + 当前链 + 当前口径下的聚合报告）
    rows = store.answer_view_rows(cfg.config_sha, policy_sha, with_records=True)
    if args.run:
        rows = [r for r in rows if r["run_id"] == args.run]
    verdicts = views.verdicts_of(rows)
    applied = voided = 0
    for r in rows:
        if r["current_judge_fp"]:
            applied += sum(er["eff"].source == "human"
                           for er in store.effective_rows(r["answer_id"], r["current_judge_fp"], cfg.config_sha))
        voided += len(store.voided_reviews(r["answer_id"]))
    report = {
        "judgment_snapshot": {
            "schema": 3, "chain_sha": cfg.config_sha, "policy_sha": policy_sha,
            "operators": operators_of_config(cfg), "judge_fp": "current",
            "reviews_applied": applied, "reviews_voided": voided,
            "undecided_excluded": sum(v.status == "undecided" for v in verdicts),
        },
        "episodes": len(rows),
        "views": {
            "ability": views.ability_view(verdicts, policy),
            "attribution": views.attribution_view(verdicts),
            "quality": views.quality_from_db(store, rows, cfg.config_sha),
        },
        "verdicts": [v.as_dict() for v in verdicts],
    }
    (out / "scoring.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"导出 {len(rows)} 段会话的判定明细与报告（当前期望）→ {out}")
    return 0


def cmd_backup(args) -> int:
    store = open_store(args.db)
    target = store.backup(args.target)
    print(f"备份评测库 → {target}")
    return 0


# ---------------------------------------------------------------- import-legacy（C14）

def cmd_import_legacy(args) -> int:
    run = Path(args.run)
    records, manifest = load_run(run)
    run_id = run_id_of(manifest, run)
    policy, policy_sha = load_policy(args.policy or default_policy_path())
    store = open_store(args.db)
    ingest_run(store, run, records, manifest)
    chain_sha = None
    old = run / "scoring.json"
    if old.exists():
        chain_sha = (json.loads(old.read_text(encoding="utf-8")).get("judgment_snapshot") or {}).get("operators_config_sha")
    chain_sha = chain_sha or "legacy"

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

    n_reviews = 0
    for r in load_reviews(run):
        a_id = answer_id_of(run_id, r.episode_id)
        if store.answer(a_id) is None:
            continue
        item_sha = getattr(r, "item_sha", None) or sha_cache.get(r.episode_id, {}).get(r.item_id)
        store.add_review(a_id, r.item_id, r.value, r.reason or "", r.reviewer or "?", item_sha, r.answer_sha)
        n_reviews += 1

    n_cache = 0
    cache_path = Path(args.cache) if args.cache else run / "verdict-cache.jsonl"
    if cache_path.exists():
        n_cache = store.cache_bulk([json.loads(l) for l in
                                    cache_path.read_text(encoding="utf-8").splitlines() if l.strip()])
    n_concl = store.rescore(policy, policy_sha, chain_sha)
    print(f"导入 run {run_id}：{n_verdicts} 条判定、{n_reviews} 条改判、{n_cache} 条缓存；"
          f"重算 {n_concl} 组结论（链 {chain_sha[:8]}）")
    return 0


def cmd_serve(args) -> int:
    try:
        from .serve import run_server
    except ImportError as e:
        print(f"复核服务需要可选依赖：{e}\n安装：pip install -r scoring/requirements.txt", file=sys.stderr)
        return 1
    run_server(db=args.db or DEFAULT_DB, host=args.host, port=args.port,
               policy_path=args.policy, operators_path=args.operators)
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
    pi = specs.add_parser("import", help="导入 dist.jsonl 期望并设为当前期望")
    pi.add_argument("dist"); pi.add_argument("--version", help="覆盖评测集版本（缺省取行内 evalset_version）")
    add_db(pi); pi.set_defaults(func=cmd_specs_import)
    ps = specs.add_parser("set-current", help="指定某评测集版本的期望为当前期望")
    ps.add_argument("version")
    add_db(ps); ps.set_defaults(func=cmd_specs_set_current)

    p = sub.add_parser("rejudge", help="补判缺当前期望+链配置判定的答案（只调算子，不跑 agent）")
    p.add_argument("--question", help="限定题目 qkey")
    p.add_argument("--run", help="限定 run")
    p.add_argument("--operators"); p.add_argument("--policy")
    add_db(p); p.set_defaults(func=cmd_rejudge)

    p = sub.add_parser("rescore", help="只重算会话结论表，不调用任何算子")
    p.add_argument("--operators"); p.add_argument("--policy")
    add_db(p); p.set_defaults(func=cmd_rescore)

    p = sub.add_parser("compare", help="比较轴：题目 × 轴值通过矩阵（§7）")
    p.add_argument("--axis", required=True, choices=views.ATTRS)
    p.add_argument("--fix", action="append", help="固定属性 k=v，可重复")
    p.add_argument("--any", action="append", choices=views.ATTRS, help="声明不限的属性，可重复")
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
    p.add_argument("--cache", help="额外导入的判定缓存文件（缺省 run 目录下 verdict-cache.jsonl）")
    p.add_argument("--policy")
    add_db(p); p.set_defaults(func=cmd_import_legacy)

    p = sub.add_parser("serve", help="复核服务（展示 · 人工评价 · 统计）")
    p.add_argument("--db", help=f"评测库路径（默认 {DEFAULT_DB}）")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--policy"); p.add_argument("--operators")
    p.set_defaults(func=cmd_serve)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
