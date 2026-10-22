"""【冻结的对拍参照】旧版单文件 scoring.py 的逐字拷贝（仅作 tests/test_scoring.py
新旧对拍之用，不再维护；线上路径已是 scoring/ 包）。
原文件头：离线判定与聚合（v2，只读 results.jsonl——编译行内嵌在 EpisodeRecord.case 中）。

分层：前提有效性 → 事实四级判定（F/E/X + 组）→ 路径约束 → 单段会话 → 聚合（dim×topic×mode，Wilson 区间）
→ 归因（natural 三分）→ 视图（能力/归因/用例质量/版本对比/锚点绊线）。

judge 仍以函数参数传入（fact/forbidden/refusal/precond）；判定快照由调用方以 JudgesMeta 一并给出，
scoring 原样写进输出头部——判定变化不被误读为 agent 变化。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean
from typing import Callable, Iterable

HIT, VAGUE, MISSING, CONTRA = "对", "部分", "未提及", "错"
LEVELS = (HIT, VAGUE, MISSING, CONTRA)
SCORE = {HIT: 1.0, VAGUE: 0.5, MISSING: 0.0, CONTRA: 0.0}

FactJudge = Callable[[str, str], str]
ForbiddenJudge = Callable[[str, str], bool]
RefusalJudge = Callable[[str], bool]
PrecondJudge = Callable[[str, str], bool]

NEGATIONS = ("不是", "并非", "没有", "不由", "无需", "不", "非", "无", "未")
NEGATION_WINDOW = 4
RULE_VERSION = "rule-v2"


class JudgeError(Exception):
    """judge 基础设施出错：记为 error，不计入分母。"""


@dataclass
class Judges:
    fact: FactJudge
    forbidden: ForbiddenJudge
    refusal: RefusalJudge
    precond: PrecondJudge


@dataclass
class JudgesMeta:
    """判定快照（04 §6）：judge 名、prompt 模板 sha、规则版本；scoring 自算的部分是 RULE_VERSION。"""

    model: str = "<default>"
    prompt_sha: str = "-"


# ---------------------------------------------------------------- 前提与事实

def keyword_hit(keywords: Iterable[str], answer: str) -> bool:
    for kw in keywords:
        start = answer.find(kw)
        while start >= 0:
            before = answer[max(0, start - NEGATION_WINDOW):start]
            if not any(neg in before for neg in NEGATIONS):
                return True
            start = answer.find(kw, start + 1)
    return False


def judge_fact(fact: dict, answer: str, j: Judges) -> tuple[str, str]:
    if fact.get("keywords") and keyword_hit(fact["keywords"], answer):
        return HIT, "rule"
    verdict = j.fact(fact["text"], answer)
    if verdict not in LEVELS:
        raise JudgeError(f"judge 输出了非法等级：{verdict!r}")
    return verdict, "judge"


def _pick(outputs: list[str], item: dict) -> str:
    turn = item.get("turn")
    if turn is not None and 1 <= turn <= len(outputs):
        return outputs[turn - 1]
    return outputs[-1]


def loaded_skills_by_turn(record: dict) -> list[set[str]]:
    out: list[set[str]] = []
    for t in record.get("turns", []):
        names = {s["name"] for s in (t.get("observations") or {}).get("skills", []) if s.get("ok")}
        out.append(names)
    return out


def check_preconditions(case: dict, outputs: list[str], j: Judges) -> tuple[bool, list[dict]]:
    """前提有效性（CASE-STRUCTURE §2.8）：任一不成立 → episode 无效。"""
    details = []
    ok = True
    for p in case.get("preconditions") or []:
        hold = j.precond(p["text"], _pick(outputs, p))
        details.append({"id": p["id"], "hold": hold})
        if not hold:
            ok = False
    return ok, details


def check_path(case: dict, record: dict) -> str:
    """路径约束（CASE-STRUCTURE §2.10）：forced 记 n/a；必须=截至 N 轮已加载；禁止=第 N 轮/全程。"""
    if record.get("mode") == "forced":
        return "n/a"
    per_turn = loaded_skills_by_turn(record)
    n = len(per_turn)
    ever: set[str] = set()
    for p in case.get("path") or []:
        skill = (p.get("action") or {}).get("skill")
        if not skill:
            continue
        upto = per_turn[: p.get("turn") or n]
        if p["type"] == "must":
            if not any(skill in turns for turns in upto):
                return "violate"
            ever.add(skill)
        else:
            window = per_turn[p.get("turn") - 1:p.get("turn")] if p.get("turn") else per_turn
            if any(skill in turns for turns in window):
                return "violate"
    return "ok"


# ---------------------------------------------------------------- 单段会话

@dataclass
class EpisodeVerdict:
    uid: str
    mode: str
    trial: int
    status: str                      # pass / fail / invalid / error（error=运行未完成，不进分母）
    invalid_reason: str | None = None
    score: float = 0.0
    strict: bool = False
    vetoed: bool = False
    facts: dict[str, str] = field(default_factory=dict)
    expected_hit: dict[str, bool] = field(default_factory=dict)
    forbidden_adopted: dict[str, bool] = field(default_factory=dict)
    precondition: list[dict] = field(default_factory=list)
    path: str = "n/a"
    by_rule: int = 0
    by_judge: int = 0
    ask_user: int = 0
    target_loaded: bool | None = None
    other_loaded: bool = False
    error: str | None = None


def target_skill(case: dict, routing: dict[str, list[str]] | None) -> str | None:
    for p in case.get("path") or []:
        if p.get("type") == "must":
            return (p.get("action") or {}).get("skill")
    topic = case.get("topic")
    if routing and topic:
        skills = routing.get(topic) or []
        return skills[0] if skills else None
    return None


def score_episode(record: dict, j: Judges, routing: dict[str, list[str]] | None = None) -> EpisodeVerdict:
    case = record.get("case") or {}
    v = EpisodeVerdict(uid=record.get("uid", "?"), mode=record.get("mode", "?"), trial=record.get("trial", 0),
                       status="error", ask_user=sum((t.get("observations") or {}).get("ask_user", {}).get("count", 0)
                                                     for t in record.get("turns", [])))
    if record.get("status") != "ok":
        v.error = f"运行未完成：{record.get('failed_turn') or '会话'} {record.get('status')}"
        return v
    outputs = [t["output"] for t in record.get("turns", []) if t.get("source") == "live"] or \
              [t["output"] for t in record.get("turns", [])]
    if not outputs:
        v.error = "没有可判定的回答"
        return v

    v.path = check_path(case, record)
    tgt = target_skill(case, routing)
    loaded_ever = set().union(*loaded_skills_by_turn(record)) if record.get("turns") else set()
    v.target_loaded = (tgt in loaded_ever) if tgt else None
    v.other_loaded = bool(loaded_ever - ({tgt} if tgt else set()))

    hold, v.precondition = check_preconditions(case, outputs, j)
    if not hold:
        v.status, v.invalid_reason = "invalid", "前提不成立（不计为答错）"
        return v
    if v.path == "violate":
        v.status, v.invalid_reason = "fail", "路径约束违反"
        return v

    if not case.get("answerable", True):
        ok = j.refusal(outputs[-1])
        v.status = "pass" if ok else "fail"
        v.by_judge = 1
        return v

    try:
        req = [f for f in case.get("facts") or [] if f.get("kind") == "required"]
        exp = [f for f in case.get("facts") or [] if f.get("kind") == "expected"]
        forb = [f for f in case.get("facts") or [] if f.get("kind") == "forbidden"]
        # 组：同组满足其一即可
        groups: dict[str, list[dict]] = defaultdict(list)
        singles = []
        for f in req:
            (groups[f["group"]] if f.get("group") else singles).append(f)

        def group_ok(members: list[dict]) -> tuple[bool, dict[str, str]]:
            verdicts = {}
            for f in members:
                verdict, source = judge_fact(f, _pick(outputs, f), j)
                verdicts[f["id"]] = verdict
                if source == "rule":
                    v.by_rule += 1
                else:
                    v.by_judge += 1
            return any(verdicts[f["id"]] == HIT for f in members), verdicts

        all_pass = True
        for f in singles:
            verdict, source = judge_fact(f, _pick(outputs, f), j)
            v.facts[f["id"]] = verdict
            if source == "rule":
                v.by_rule += 1
            else:
                v.by_judge += 1
            if verdict != HIT:
                all_pass = False
        for g, members in groups.items():
            ok, verdicts = group_ok(members)
            v.facts.update(verdicts)
            if not ok:
                all_pass = False
        for f in exp:
            verdict, _ = judge_fact(f, _pick(outputs, f), j)
            v.expected_hit[f["id"]] = verdict == HIT

        for x in forb:
            adopted = j.forbidden(x["text"], _pick(outputs, x))
            v.forbidden_adopted[x["id"]] = adopted
            v.by_judge += 1

        v.vetoed = any(v.forbidden_adopted.values()) or CONTRA in v.facts.values()
        scored = [s for s in v.facts.values()]
        v.score = 0.0 if v.vetoed else (mean(SCORE[s] for s in scored) if scored else 1.0)
        v.strict = all_pass and not v.vetoed
        v.status = "pass" if v.strict else "fail"
        return v
    except JudgeError as e:
        v.status, v.error = "error", str(e)
        return v


# ---------------------------------------------------------------- 聚合

def wilson(passes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 95% 区间；total=0 返回 (0,0)。"""
    if total == 0:
        return 0.0, 0.0
    p = passes / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return round(max(0.0, center - half), 4), round(min(1.0, center + half), 4)


def score_run(records: list[dict], j: Judges, routing: dict[str, list[str]] | None = None,
              meta: JudgesMeta | None = None) -> dict:
    """判定全部会话并聚合。返回带判定快照头部的报告。"""
    verdicts = [score_episode(r, j, routing) for r in records]
    header = {
        "judgment_snapshot": {
            "judge_model": (meta or JudgesMeta()).model,
            "judge_prompt_sha": (meta or JudgesMeta()).prompt_sha,
            "rule_version": RULE_VERSION,
        },
        "episodes": len(verdicts),
    }
    header["views"] = {
        "ability": ability_view(verdicts, records),
        "attribution": attribution_view(verdicts),
        "quality": quality_view(verdicts, records),
    }
    header["verdicts"] = [v.__dict__ for v in verdicts]
    return header


def ability_view(verdicts: list[EpisodeVerdict], records: list[dict]) -> dict:
    """dim × mode 的通过率 + 前提成立率（Wilson 区间）；ec 主口径取 forced。"""
    # dim 从内嵌 case 取
    def dim_of(v: EpisodeVerdict) -> str:
        rec = next(r for r in records if r.get("uid") == v.uid and r.get("mode") == v.mode and r.get("trial") == v.trial)
        return (rec.get("case") or {}).get("dim", "?")

    cells: dict[tuple, list[EpisodeVerdict]] = defaultdict(list)
    for v in verdicts:
        cells[(dim_of(v), v.mode)].append(v)
    out: dict[str, dict] = {}
    for (dim, mode), vs in sorted(cells.items()):
        denom = [v for v in vs if v.status in ("pass", "fail")]      # invalid/error 不进通过率分母
        passes = sum(v.status == "pass" for v in denom)
        precond_denom = [v for v in vs if v.status != "error"]
        precond_ok = sum(v.status != "invalid" for v in precond_denom)
        direct = [v for v in denom if v.ask_user == 0]
        after = [v for v in denom if v.ask_user > 0]
        rate = lambda xs: (sum(v.status == "pass" for v in xs), len(xs))
        out[f"{dim}|{mode}"] = {
            "n": len(denom),
            "pass": passes,
            "pass_rate": round(passes / len(denom), 4) if denom else None,
            "pass_ci": wilson(passes, len(denom)),
            "precond_rate": round(precond_ok / len(precond_denom), 4) if precond_denom else None,
            "precond_ci": wilson(precond_ok, len(precond_denom)),
            "invalid": sum(v.status == "invalid" for v in vs),
            "error": sum(v.status == "error" for v in vs),
            "pass_direct": rate(direct),
            "pass_after_ask": rate(after),
            "headline": dim == "错误知识纠正" and mode == "forced",
        }
    return out


def attribution_view(verdicts: list[EpisodeVerdict]) -> dict:
    """natural 三分归因（04 §6.3）：未加载 / 误加载 / 加载但答错。"""
    nat = [v for v in verdicts if v.mode == "natural" and v.status in ("pass", "fail")]
    cats = {"not_loaded": 0, "mis_loaded": 0, "loaded_wrong": 0, "loaded_pass": 0, "unknown": 0}
    for v in nat:
        if v.target_loaded is None:
            cats["unknown"] += 1
        elif not v.target_loaded and not v.other_loaded:
            cats["not_loaded"] += 1
        elif not v.target_loaded and v.other_loaded:
            cats["mis_loaded"] += 1
        elif v.status == "pass":
            cats["loaded_pass"] += 1
        else:
            cats["loaded_wrong"] += 1
    total = sum(cats.values())
    rates = {k: round(c / total, 4) for k, c in cats.items()} if total else dict(cats)
    return {"n": total, **rates}


def quality_view(verdicts: list[EpisodeVerdict], records: list[dict]) -> dict:
    """用例质量视图入口：规则判对清单（复核输入）、注入 found>1、前提不成立清单。"""
    by_uid: dict[str, dict] = {}
    for r in records:
        by_uid[f'{r.get("uid")}|{r.get("mode")}|{r.get("trial")}'] = r
    rule_hit_uids = sorted({v.uid for v in verdicts if v.by_rule > 0})
    found_gt1 = sorted({r.get("uid", "?") for r in records
                        if any((a or {}).get("found", 1) > 1 for a in r.get("injections_applied") or [])})
    invalid = [f'{v.uid}|{v.mode}|t{v.trial}' for v in verdicts if v.status == "invalid"]
    return {"rule_judged_uids": rule_hit_uids, "injection_found_gt1": found_gt1, "invalid_episodes": invalid}


# ---------------------------------------------------------------- 版本对比与锚点绊线

def _load_run(run_dir: str) -> tuple[list[dict], dict | None]:
    run_dir_path = Path(run_dir)
    records = [json.loads(l) for l in (run_dir_path / "results.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    manifest = None
    mj = run_dir_path / "run.json"
    if mj.exists():
        manifest = json.loads(mj.read_text(encoding="utf-8"))
    return records, manifest


def snapshot_diff(a: dict | None, b: dict | None) -> list[str]:
    keys = ["evalset", "runner_profile", "eline_profile", "skills_mode", "skills_base", "agent", "models_observed", "judge"]
    diff = []
    for k in keys:
        va = json.dumps((a or {}).get(k), sort_keys=True, ensure_ascii=False)
        vb = json.dumps((b or {}).get(k), sort_keys=True, ensure_ascii=False)
        if va != vb:
            diff.append(k)
    return diff


def compare_runs(run_a: str, run_b: str, diff_md: str | None = None) -> dict:
    """版本对比视图：快照差异键 → 按 uid 配对通过率差（带区间）→ 锚点绊线。"""
    recs_a, man_a = _load_run(run_a)
    recs_b, man_b = _load_run(run_b)
    snap_a = (man_a or {}).get("snapshot") or {}
    snap_b = (man_b or {}).get("snapshot") or {}
    blocked = None
    if snap_a.get("skills_mode") != snap_b.get("skills_mode"):
        blocked = "skills_mode 不同，跨 mode 对比无效（过渡期自成基线）"
    keys_diff = snapshot_diff(snap_a, snap_b)

    def pass_map(records: list[dict]) -> dict[str, list[int]]:
        out: dict[str, list[int]] = defaultdict(list)
        for r in records:
            out[r["uid"]].append(1 if r.get("status") == "ok" else 0)
        return out

    paired = {}
    pa, pb = pass_map(recs_a), pass_map(recs_b)
    demoted_out: set[str] = set()
    if diff_md and Path(diff_md).exists():
        text = diff_md.read_text(encoding="utf-8")
        if "## 因降级移出" in text:
            section = text.split("## 因降级移出")[1].split("##")[0]
            demoted_out = {line.split("（")[0].lstrip("- ").strip() for line in section.splitlines() if line.strip().startswith("- ")}
    for uid in sorted(set(pa) & set(pb) - demoted_out):
        a_pass = sum(pa[uid]) / len(pa[uid])
        b_pass = sum(pb[uid]) / len(pb[uid])
        d = b_pass - a_pass
        n = min(len(pa[uid]), len(pb[uid]))
        se = math.sqrt(a_pass * (1 - a_pass) / max(n, 1) + b_pass * (1 - b_pass) / max(n, 1)) or 1e-9
        paired[uid] = {"delta": round(d, 4), "ci": (round(d - 1.96 * se, 4), round(d + 1.96 * se, 4)),
                       "significant": not (d - 1.96 * se <= 0 <= d + 1.96 * se)}

    tripwires = []
    if not blocked:
        def anchor_state(records: list[dict]) -> dict[str, tuple[bool, int]]:
            out = {}
            for r in records:
                if r.get("anchor"):
                    out.setdefault(r["uid"], (True, 0))
                    ok, cnt = out[r["uid"]]
                    out[r["uid"]] = (ok and r.get("status") == "ok", cnt + 1)
            return out

        sa, sb = anchor_state(recs_a), anchor_state(recs_b)
        for uid in sorted(set(sa) & set(sb)):
            (a_ok, a_n), (b_ok, b_n) = sa[uid], sb[uid]
            if a_n >= 2 and b_n >= 2 and a_ok and not b_ok:
                tripwires.append(uid)

    return {
        "snapshot_diff_keys": keys_diff,
        "blocked": blocked,
        "note": "差异键多于被比较项时，结论需注明" if len(keys_diff) > 1 else None,
        "paired": paired,
        "significant_uids": [u for u, p in paired.items() if p["significant"]],
        "anchor_tripwires": tripwires,
        "demoted_excluded": sorted(demoted_out),
    }


# ---------------------------------------------------------------- CLI

def _default_judges() -> Judges:
    """无外部 judge 时的规则兜底：仅作结构演示，正式判定必须接 judge。"""
    return Judges(
        fact=lambda text, answer: (HIT if text[:6] in answer else MISSING),
        forbidden=lambda text, answer: text[:6] in answer,
        refusal=lambda answer: "不知道" in answer or "无相关" in answer,
        precond=lambda text, answer: text[:6] in answer,
    )


def cmd_score(args) -> int:
    records = [json.loads(l) for l in Path(args.run).joinpath("results.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    manifest = json.loads(Path(args.run).joinpath("run.json").read_text(encoding="utf-8")) if Path(args.run).joinpath("run.json").exists() else {}
    routing = (manifest.get("profile", {}).get("content", {}) or {}).get("routing")
    report = score_run(records, _default_judges(), routing, JudgesMeta(model=args.judge_model))
    out = Path(args.out) if args.out else Path(args.run) / "scoring.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"判定 {len(records)} 段会话 → {out}")
    for cell, m in report["views"]["ability"].items():
        print(f"  {cell}: 通过 {m['pass']}/{m['n']}{'' if m['pass_rate'] is None else f" = {m['pass_rate']}"} 区间 {m['pass_ci']}"
              f"{'  ← ec 主口径' if m['headline'] else ''}")
    return 0


def cmd_compare(args) -> int:
    report = compare_runs(args.run_a, args.run_b, args.diff)
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="离线判定与聚合（只读 results.jsonl）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("score", help="判定单个 run")
    p.add_argument("run")
    p.add_argument("--out")
    p.add_argument("--judge-model", default="<default>")
    p.set_defaults(func=cmd_score)
    p = sub.add_parser("compare", help="两个 run 的版本对比与锚点绊线")
    p.add_argument("run_a")
    p.add_argument("run_b")
    p.add_argument("--diff", help="md2case 生成的 diff.md（对齐因降级移出的分母）")
    p.set_defaults(func=cmd_compare)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
