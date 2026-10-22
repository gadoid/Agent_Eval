"""分析出口（方案 §10，04 §7 五类出口的迁移落点）与多 run 对比（§9.5）。"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .effective import Review, effective_final
from .policy import EpisodeVerdict, Policy

SNAPSHOT_KEYS = ["evalset", "runner_profile", "eline_profile", "skills_mode", "skills_base", "agent", "models_observed"]
ALLOWED_DIFF = {
    "model": {"runner_profile", "eline_profile", "models_observed"},
    "skill": {"skills_base"},
}


@dataclass
class RunBundle:
    """一个 run 的全部判定材料（cli 装载，视图只读）。"""

    run_id: str
    run_dir: str
    records: list[dict]
    manifest: dict | None
    rows: list[dict]                 # verdicts.jsonl 行
    reviews: list[Review]
    verdicts: list[EpisodeVerdict]   # 会话结论（有效结论 + 口径）
    scored: bool = True              # 是否已跑过 score（无 verdicts.jsonl 时 False）


# ---------------------------------------------------------------- 区间

def wilson(passes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 95% 区间；total=0 返回 (0,0)。"""
    if total == 0:
        return 0.0, 0.0
    p = passes / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return round(max(0.0, center - half), 4), round(min(1.0, center + half), 4)


# ---------------------------------------------------------------- 出口 1：能力视图

def ability_view(verdicts: list[EpisodeVerdict], policy: Policy) -> dict:
    """dim × mode 的通过率与前提成立率（Wilson 区间）；headline 由 policy 决定。"""
    cells: dict[tuple, list[EpisodeVerdict]] = defaultdict(list)
    for v in verdicts:
        cells[(v.dim, v.mode)].append(v)
    out: dict[str, dict] = {}
    for (dim, mode), vs in sorted(cells.items()):
        denom = [v for v in vs if v.status in ("pass", "fail")]       # invalid/undecided/error 不进通过率分母
        passes = sum(v.status == "pass" for v in denom)
        precond_denom = [v for v in vs if v.status in ("pass", "fail", "invalid")]
        precond_ok = sum(v.status != "invalid" for v in precond_denom)
        direct = [v for v in denom if v.ask_user == 0]
        after = [v for v in denom if v.ask_user > 0]
        rate = lambda xs: (sum(v.status == "pass" for v in xs), len(xs))  # noqa: E731
        out[f"{dim}|{mode}"] = {
            "n": len(denom),
            "pass": passes,
            "pass_rate": round(passes / len(denom), 4) if denom else None,
            "pass_ci": wilson(passes, len(denom)),
            "precond_rate": round(precond_ok / len(precond_denom), 4) if precond_denom else None,
            "precond_ci": wilson(precond_ok, len(precond_denom)),
            "invalid": sum(v.status == "invalid" for v in vs),
            "undecided": sum(v.status == "undecided" for v in vs),
            "error": sum(v.status == "error" for v in vs),
            "pass_direct": rate(direct),
            "pass_after_ask": rate(after),
            "headline": policy.headline(dim) == mode,
        }
    return out


# ---------------------------------------------------------------- 出口 2：归因视图

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


# ---------------------------------------------------------------- 出口 3：用例质量视图

def quality_view(verdicts: list[EpisodeVerdict], records: list[dict], rows: list[dict], reviews: list[Review]) -> dict:
    """人工与机器不一致清单、规则判定一致率、注入 found>1、无效清单、forced 前提成立率偏低的 ec。"""
    by_ep: dict[str, list[Review]] = defaultdict(list)
    for r in reviews:
        by_ep[r.episode_id].append(r)
    mismatch: list[dict] = []
    rule_total = rule_agree = 0
    for row in rows:
        ep_reviews = by_ep.get(row["episode_id"])
        if not ep_reviews:
            continue
        eff = effective_final(row, ep_reviews, row["answer_sha"])
        if eff.source != "human":
            continue
        machine = row["machine"]
        if machine.get("source") == "rule" and machine.get("value") is not None:
            rule_total += 1
            if machine.get("value") == eff.value:
                rule_agree += 1
        if machine.get("value") != eff.value:
            mismatch.append({
                "episode_id": row["episode_id"], "uid": row["uid"],
                "kind": row["item"]["kind"], "item": row["item"]["id"],
                "machine": machine.get("value"), "human": eff.value,
            })
    found_gt1 = sorted({r.get("uid", "?") for r in records
                        if any((a or {}).get("found", 1) > 1 for a in r.get("injections_applied") or [])})
    invalid = [f"{v.uid}|{v.mode}|t{v.trial}" for v in verdicts if v.status == "invalid"]
    forced_ec_low = []
    forced_ec: dict[str, list[EpisodeVerdict]] = defaultdict(list)
    for v in verdicts:
        if v.mode == "forced" and v.dim == "错误知识纠正" and v.status != "error":
            forced_ec[v.uid].append(v)
    for uid, vs in sorted(forced_ec.items()):
        n_invalid = sum(v.status == "invalid" for v in vs)
        if len(vs) >= 2 and n_invalid / len(vs) > 0.5:
            forced_ec_low.append(uid)
    return {
        "human_machine_mismatch": mismatch,
        "rule_agreement": {"agree": rule_agree, "total": rule_total,
                           "rate": round(rule_agree / rule_total, 4) if rule_total else None},
        "injection_found_gt1": found_gt1,
        "invalid_episodes": invalid,
        "forced_ec_precond_low": forced_ec_low,
    }


# ---------------------------------------------------------------- 出口 4/5：多 run 对比与锚点绊线

def operators_of(rows: list[dict]) -> set[str]:
    """一个 run 实际用到的机器算子版本清单（判定配置摘要 operators 的同源数据）。"""
    return {r["machine"].get("operator") for r in rows
            if r["machine"].get("value") is not None and r["machine"].get("operator")}


def parse_diff(diff_path: str | Path) -> dict[str, list[str]]:
    """解析 md2case diff.md 的五类清单（新增/退役/因降级移出/内容修订/注入 rev 变化）。"""
    text = Path(diff_path).read_text(encoding="utf-8")
    sections: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:].split("（")[0].strip()
            sections.setdefault(current, [])
        elif line.strip().startswith("- ") and current:
            sections[current].append(line.lstrip("- ").strip())
    return sections


def _snapshot_value(snapshot: dict, key: str) -> str:
    return json.dumps(snapshot.get(key), sort_keys=True, ensure_ascii=False)


def compare_runs(a: RunBundle, b: RunBundle, intended: str = "model", diff_md: str | None = None) -> dict:
    """多 run 对比（§9.5）：声明预期变量（model / skill）放行对应快照键差异；
    通过率取有效结论，修正现行以完成率计通过的问题；锚点绊线按结论判定。"""
    if intended not in ALLOWED_DIFF:
        raise ValueError(f"预期变量只能是 {'/'.join(ALLOWED_DIFF)}")
    snap_a = (a.manifest or {}).get("snapshot") or {}
    snap_b = (b.manifest or {}).get("snapshot") or {}
    diff_keys = [k for k in SNAPSHOT_KEYS if _snapshot_value(snap_a, k) != _snapshot_value(snap_b, k)]
    allowed = ALLOWED_DIFF[intended]
    blocked = None
    if snap_a.get("skills_mode") != snap_b.get("skills_mode"):
        blocked = "skills_mode 不同，跨 mode 对比无效（过渡期自成基线）"
    ops_a, ops_b = operators_of(a.rows), operators_of(b.rows)
    if ops_a != ops_b:
        blocked = blocked or f"算子版本不同：{sorted(ops_a ^ ops_b)}"
    unexpected = [k for k in diff_keys if k not in allowed and k != "evalset"]
    if unexpected:
        blocked = blocked or f"预期变量（{intended}）之外的快照键不同：{unexpected}"

    demoted_out: set[str] = set()
    if "evalset" in diff_keys:
        sections = parse_diff(diff_md) if diff_md and Path(diff_md).exists() else None
        if sections is None:
            blocked = blocked or "evalset 不同且未提供 diff.md（--diff），无法对齐分母"
        else:
            other = {k: v for k, v in sections.items() if k != "因降级移出" and v}
            if other:
                blocked = blocked or f"evalset 不同且 diff 含 {'、'.join(sorted(other))}，无法对齐分母"
            demoted_out = {u.split("（")[0].strip() for u in sections.get("因降级移出", [])}
    elif diff_md and Path(diff_md).exists():
        demoted_out = {u.split("（")[0].strip() for u in parse_diff(diff_md).get("因降级移出", [])}

    def pass_map(verdicts: list[EpisodeVerdict]) -> dict[str, list[int]]:
        out: dict[str, list[int]] = defaultdict(list)
        for v in verdicts:
            if v.status in ("pass", "fail"):
                out[v.uid].append(1 if v.status == "pass" else 0)
        return out

    paired: dict[str, dict] = {}
    pa, pb = pass_map(a.verdicts), pass_map(b.verdicts)
    for uid in sorted((set(pa) & set(pb)) - demoted_out):
        a_pass = sum(pa[uid]) / len(pa[uid])
        b_pass = sum(pb[uid]) / len(pb[uid])
        d = b_pass - a_pass
        n = min(len(pa[uid]), len(pb[uid]))
        se = math.sqrt(a_pass * (1 - a_pass) / max(n, 1) + b_pass * (1 - b_pass) / max(n, 1)) or 1e-9
        paired[uid] = {"delta": round(d, 4), "ci": (round(d - 1.96 * se, 4), round(d + 1.96 * se, 4)),
                       "significant": not (d - 1.96 * se <= 0 <= d + 1.96 * se)}

    tripwires: list[str] = []
    if not blocked:
        def anchor_state(bundle: RunBundle) -> dict[str, tuple[bool, int]]:
            out: dict[str, tuple[bool, int]] = {}
            for rec, v in zip(bundle.records, bundle.verdicts):
                if rec.get("anchor"):
                    ok, cnt = out.get(v.uid, (True, 0))
                    out[v.uid] = (ok and v.status == "pass", cnt + 1)
            return out

        sa, sb = anchor_state(a), anchor_state(b)
        for uid in sorted(set(sa) & set(sb)):
            (a_ok, a_n), (b_ok, b_n) = sa[uid], sb[uid]
            if a_n >= 2 and b_n >= 2 and a_ok and not b_ok:
                tripwires.append(uid)

    allowed_diff_keys = sorted(k for k in diff_keys if k in allowed)
    return {
        "run_a": a.run_id,
        "run_b": b.run_id,
        "intended": intended,
        "snapshot_diff_keys": diff_keys,
        "allowed_diff_keys": allowed_diff_keys,
        "blocked": blocked,
        "note": "差异键多于被比较项时，结论需注明" if len(diff_keys) > len(allowed_diff_keys) + (1 if "evalset" in diff_keys else 0) else None,
        "paired": paired,
        "significant_uids": [u for u, p in paired.items() if p["significant"]],
        "anchor_tripwires": tripwires,
        "demoted_excluded": sorted(demoted_out),
    }
