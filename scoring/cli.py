"""scoring 命令行（python -m scoring）：score / rescore / compare / serve。

score   执行机器算子链，落盘 verdicts.jsonl，产出 scoring.json
rescore 只读 verdicts.jsonl + review.jsonl 经 effective_final 取数重算口径，不调用任何算子
compare 多 run 对比（预期变量 model/skill，§9.5）
serve   复核服务（依赖可选安装，见 §11）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import views
from .chain import (VerdictCache, default_cache_path, default_operators_path, episode_id_of,
                    judge_episode, load_operator_config, load_verdicts, write_verdicts)
from .effective import applied_count, effective_final, load_reviews, voided_reviews
from .items import build_context, episode_error, extract_items
from .operators import Verdict
from .policy import EpisodeVerdict, Policy, default_policy_path, load_policy, session_conclusion
from .views import RunBundle


# ---------------------------------------------------------------- 装载

def load_run(run_dir: str | Path) -> tuple[list[dict], dict | None]:
    p = Path(run_dir)
    records = [json.loads(l) for l in (p / "results.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    manifest = None
    mj = p / "run.json"
    if mj.exists():
        manifest = json.loads(mj.read_text(encoding="utf-8"))
    return records, manifest


def routing_of(manifest: dict | None) -> dict | None:
    return (((manifest or {}).get("profile", {}) or {}).get("content", {}) or {}).get("routing")


def run_id_of(manifest: dict | None, run_dir: str | Path) -> str:
    return (manifest or {}).get("run_id") or Path(run_dir).name


def conclusions(records: list[dict], rows: list[dict], reviews: list, policy: Policy,
                routing: dict | None = None) -> list[EpisodeVerdict]:
    """对全部会话计算有效结论 + 口径结论。verdicts 缺行的条目按未决处理。"""
    rows_by_ep: dict[str, dict[str, dict]] = defaultdict_rows(rows)
    reviews_by_ep: dict[str, list] = {}
    for r in reviews:
        reviews_by_ep.setdefault(r.episode_id, []).append(r)
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
        eff: dict[str, Verdict] = {}
        for item in items:
            row = ep_rows.get(item.id)
            if row is None:
                eff[item.id] = Verdict(None, "", "")
            else:
                eff[item.id] = effective_final(row, reviews_by_ep.get(row["episode_id"], []), row["answer_sha"])
        out.append(session_conclusion(record, ctx, items, eff, policy, routing))
    return out


def defaultdict_rows(rows: list[dict]) -> dict[str, dict[str, dict]]:
    out: dict[str, dict[str, dict]] = {}
    for row in rows:
        out.setdefault(row["episode_id"], {})[row["item"]["id"]] = row
    return out


def load_bundle(run_dir: str | Path, policy: Policy) -> RunBundle:
    records, manifest = load_run(run_dir)
    rows = load_verdicts(run_dir)
    reviews = load_reviews(run_dir)
    return RunBundle(run_id=run_id_of(manifest, run_dir), run_dir=str(run_dir), records=records,
                     manifest=manifest, rows=rows, reviews=reviews,
                     verdicts=conclusions(records, rows, reviews, policy, routing_of(manifest)))


# ---------------------------------------------------------------- 报告

def build_report(run_id: str, records: list[dict], rows: list[dict], reviews: list,
                 verdicts: list[EpisodeVerdict], policy: Policy, policy_sha: str,
                 operators_config_sha: str | None) -> dict:
    voided = voided_reviews(rows, reviews)
    snapshot = {
        "schema": 2,
        "run_id": run_id,
        "operators_config_sha": operators_config_sha,
        "policy_sha": policy_sha,
        "operators": sorted(views.operators_of(rows)),
        "reviews_applied": applied_count(rows, reviews),
        "reviews_voided": len(voided),
        "undecided_excluded": sum(v.status == "undecided" for v in verdicts),
    }
    return {
        "judgment_snapshot": snapshot,
        "episodes": len(verdicts),
        "views": {
            "ability": views.ability_view(verdicts, policy),
            "attribution": views.attribution_view(verdicts),
            "quality": views.quality_view(verdicts, records, rows, reviews),
        },
        "voided_reviews": voided,
        "verdicts": [v.as_dict() for v in verdicts],
    }


def write_report(report: dict, out: Path) -> None:
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")


def _print_summary(report: dict) -> None:
    snap = report["judgment_snapshot"]
    provisional = snap["undecided_excluded"] > 0
    for cell, m in report["views"]["ability"].items():
        mark = "  ← 主口径" if m["headline"] else ""
        prov = "  临时（已排除未决）" if provisional else ""
        print(f"  {cell}: 通过 {m['pass']}/{m['n']}"
              f"{'' if m['pass_rate'] is None else f' = {m['pass_rate']}'} 区间 {m['pass_ci']}{mark}{prov}")
    if provisional:
        print(f"  注意：{snap['undecided_excluded']} 段会话有未决条目被排除，数字为临时值")
    if snap["reviews_voided"]:
        print(f"  注意：{snap['reviews_voided']} 条人工改判已作废（见 scoring.json 的 voided_reviews）")


# ---------------------------------------------------------------- 子命令

def cmd_score(args) -> int:
    run = Path(args.run)
    records, manifest = load_run(run)
    cfg = load_operator_config(args.operators or default_operators_path())
    policy, policy_sha = load_policy(args.policy or default_policy_path())
    cache = VerdictCache.load(args.cache or default_cache_path())
    run_id = run_id_of(manifest, run)
    rows: list[dict] = []
    for record in records:
        rows.extend(judge_episode(record, cfg, run_id, cache).rows)
    write_verdicts(run, rows)
    reviews = load_reviews(run)
    verdicts = conclusions(records, rows, reviews, policy, routing_of(manifest))
    report = build_report(run_id, records, rows, reviews, verdicts, policy, policy_sha, cfg.config_sha)
    out = Path(args.out) if args.out else run / "scoring.json"
    write_report(report, out)
    print(f"判定 {len(records)} 段会话、{len(rows)} 条条目 → {out}")
    _print_summary(report)
    return 0


def rescore_run(run_dir: str | Path, policy_path: str | Path | None = None,
                out: str | Path | None = None) -> Path:
    """只读 verdicts.jsonl + review.jsonl 重算口径（不调用任何算子）。serve 的重算同走此处。"""
    run = Path(run_dir)
    records, manifest = load_run(run)
    policy, policy_sha = load_policy(policy_path or default_policy_path())
    rows = load_verdicts(run)
    reviews = load_reviews(run)
    # 机器结论未变，算子配置摘要沿用上次 score 写入的值
    old = run / "scoring.json"
    cfg_sha = None
    if old.exists():
        cfg_sha = (json.loads(old.read_text(encoding="utf-8")).get("judgment_snapshot") or {}).get("operators_config_sha")
    run_id = run_id_of(manifest, run)
    verdicts = conclusions(records, rows, reviews, policy, routing_of(manifest))
    report = build_report(run_id, records, rows, reviews, verdicts, policy, policy_sha, cfg_sha)
    out_path = Path(out) if out else run / "scoring.json"
    write_report(report, out_path)
    return out_path


def cmd_rescore(args) -> int:
    out = rescore_run(args.run, policy_path=args.policy, out=args.out)
    report = json.loads(out.read_text(encoding="utf-8"))
    print(f"重算 {report['episodes']} 段会话（未调用任何算子）→ {out}")
    _print_summary(report)
    return 0


def cmd_compare(args) -> int:
    policy, _ = load_policy(args.policy or default_policy_path())
    a = load_bundle(args.run_a, policy)
    b = load_bundle(args.run_b, policy)
    report = views.compare_runs(a, b, intended=args.intended, diff_md=args.diff)
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


def cmd_serve(args) -> int:
    try:
        from .serve import run_server
    except ImportError as e:
        print(f"复核服务需要可选依赖：{e}\n安装：pip install -r scoring/requirements.txt", file=sys.stderr)
        return 1
    run_server(root=args.root, host=args.host, port=args.port, policy_path=args.policy)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="scoring", description="离线判定与聚合（判定算子扩展方案 v2）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("score", help="执行算子链并落盘逐条判定")
    p.add_argument("run")
    p.add_argument("--out")
    p.add_argument("--operators", help="operators.yaml 路径（默认用包内配置）")
    p.add_argument("--policy", help="policy.yaml 路径（默认用包内配置）")
    p.add_argument("--cache", help="模型判定缓存文件（默认 scoring/verdict-cache.jsonl）")
    p.set_defaults(func=cmd_score)

    p = sub.add_parser("rescore", help="只读逐条判定与改判重算口径，不调用算子")
    p.add_argument("run")
    p.add_argument("--out")
    p.add_argument("--policy")
    p.set_defaults(func=cmd_rescore)

    p = sub.add_parser("compare", help="多 run 对比与锚点绊线（按有效结论计通过率）")
    p.add_argument("run_a")
    p.add_argument("run_b")
    p.add_argument("--intended", choices=["model", "skill"], default="model", help="本次对比的预期变量")
    p.add_argument("--diff", help="md2case 的 diff.md（evalset 不同时对齐因降级移出的分母）")
    p.add_argument("--policy")
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("serve", help="复核服务（展示 · 人工评价 · 统计）")
    p.add_argument("--root", required=True, help="运行输出目录（其下扫描含 results.jsonl 的 run）")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--policy")
    p.set_defaults(func=cmd_serve)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
