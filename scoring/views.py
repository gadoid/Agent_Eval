"""分析出口（方案 §10、评测结果管理改造方案 §7/§8）：能力/归因/质量视图、
题目级统计与比较轴。全部视图从评测库取数（store.answer_view_rows /
effective_rows），不再读 run 目录文件，也不在请求中全量重算。
"""
from __future__ import annotations

import json
import math
from collections import defaultdict

from .policy import EpisodeVerdict, Policy

# 答案的比较属性（§4.2，R3 修订后分三类）：
# - 重复采样（trial、run）：默认不限，无需声明——它们是采样标识，不是比较条件
# - 处理条件（profile、model、mode、skills_base、evalset_version）：必须一致，
#   或作为比较轴，或显式固定 / 声明不限
# - 不可比较（skills_mode）：必须一致，不能作轴、不能不限
ATTRS = ("profile", "model", "mode", "trial", "skills_mode", "skills_base", "evalset_version", "run")
SAMPLING_ATTRS = frozenset({"trial", "run"})
DERIVED_ATTR = {"profile": "model", "model": "profile"}   # 互为派生：一个作轴，另一个自动放行


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


# ---------------------------------------------------------------- 出口 1/2：能力与归因

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
        scores = [v.score for v in denom if v.score is not None]          # 区间未收敛为一点的不计均分
        out[f"{dim}|{mode}"] = {
            "n": len(denom),
            "pass": passes,
            "pass_rate": round(passes / len(denom), 4) if denom else None,
            "pass_ci": wilson(passes, len(denom)),
            "score_mean": round(sum(scores) / len(scores), 1) if scores else None,
            "score_n": len(scores),
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


def verdicts_of(rows: list[dict]) -> list[EpisodeVerdict]:
    """answer_view_rows → 会话结论对象（缺判定 / 待重判的行跳过）。"""
    return [EpisodeVerdict(**json.loads(r["detail_json"])) for r in rows if r["c_status"]]


# ---------------------------------------------------------------- 比较属性（§4.2/§7.1）

def model_label(row: dict) -> str:
    models = json.loads(row["models_json"]) if row.get("models_json") else []
    return ",".join(str(m) for m in models) if models else "?"


def attr_value(row: dict, name: str) -> str:
    if name == "profile":
        return str(row.get("profile_id") or "?")
    if name == "model":
        return model_label(row)
    if name == "run":
        return str(row.get("run_id"))
    return str(row.get(name))


# ---------------------------------------------------------------- 题目级统计（C12，§4.3）

def question_stats(rows: list[dict], run_order: dict[str, int] | None = None) -> dict[str, dict]:
    """难度 / 区分度 / 稳定性 / 题组一致性 / 历史。全部按当前期望下的有效结论计算，
    只计 pass / fail 的答案；诱导变体不计入题组一致性（CASE-STRUCTURE §2.13）。"""
    order = run_order or {}
    by_q: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_q[r["qkey"]].append(r)

    # 题组一致性（§4.3）：种子与非诱导变体 qkey 不同（输入被改写），按
    # case_id + 模型 + 干预配对：变体结论与种子结论的一致率
    by_mm: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        if r["c_status"] in ("pass", "fail"):
            by_mm[(r["case_id"], model_label(r), r["mode"])].append(r)
    gc_by_case: dict[str, tuple[int, int, float]] = {}
    for (case, _m, _mode), grp in by_mm.items():
        seeds = {r["c_status"] for r in grp if r["variant_index"] == 0}
        if len(seeds) != 1:
            continue                                      # 无种子或种子结论不一致：无法配对
        seed_status = next(iter(seeds))
        variants = [r for r in grp if r["variant_index"] > 0 and r["rewrite"] != "诱导"]
        if variants:
            agree = sum(r["c_status"] == seed_status for r in variants)
            gc_by_case[case] = (agree, len(variants), round(agree / len(variants), 4))

    out: dict[str, dict] = {}
    for qkey, rs in by_q.items():
        judged = [r for r in rs if r["c_status"] in ("pass", "fail")]
        n_pass = sum(r["c_status"] == "pass" for r in judged)
        by_model: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for r in judged:
            c = by_model[model_label(r)]
            c[0] += r["c_status"] == "pass"
            c[1] += 1
        rates = [p / t for p, t in by_model.values() if t]
        by_mode: dict[tuple, list[dict]] = defaultdict(list)
        for r in judged:
            by_mode[(model_label(r), r["mode"])].append(r)
        stab_groups = [g for g in by_mode.values() if len(g) >= 2]
        stability = None
        if stab_groups:
            total = sum(len(g) for g in stab_groups)
            consist = sum(max(sum(r["c_status"] == "pass" for r in g),
                              sum(r["c_status"] == "fail" for r in g)) for g in stab_groups)
            stability = round(consist / total, 4)
        by_env: dict[str, list] = defaultdict(lambda: [0, 0, None])   # pass, n, 首个 run 序
        for r in judged:
            env = f"{r['skills_mode'] or '?'}/{r['skills_base'] or '?'}"
            e = by_env[env]
            e[0] += r["c_status"] == "pass"
            e[1] += 1
            seq = order.get(r["run_id"], 0)
            e[2] = seq if e[2] is None else min(e[2], seq)
        history = [{"env": env, "pass": e[0], "n": e[1], "pass_rate": round(e[0] / e[1], 4)}
                   for env, e in sorted(by_env.items(), key=lambda kv: (kv[1][2], kv[0]))]
        gc = gc_by_case.get(rs[0]["case_id"])
        out[qkey] = {
            "difficulty": round(1 - n_pass / len(judged), 4) if judged else None,
            "discrimination": round(max(rates) - min(rates), 4) if len(rates) >= 2 else None,
            "models_n": len(by_model),
            "stability": stability,
            "group_consistency": gc[2] if gc else None,
            "group_pairs": gc[1] if gc else 0,
            "history": history,
            "judged": len(judged),
            "pending": sum(r["c_status"] is None for r in rs),
        }
    return out


# ---------------------------------------------------------------- 出口 4/5：比较轴（C11，§7）

def anchor_tripwires(sel: list[dict], run_order: dict[str, int] | None = None) -> list[dict]:
    """锚点绊线（§7.2）：上一 run 全部 trial 通过、本 run 全部失败（各 ≥2），按 qkey 匹配。"""
    order = run_order or {}
    by_q: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for r in sel:
        if json.loads(r.get("record_json") or "{}").get("anchor"):
            by_q[r["qkey"]][r["run_id"]].append(r)
    out: list[dict] = []
    for qkey, by_run in by_q.items():
        runs_sorted = sorted(by_run.items(), key=lambda kv: (order.get(kv[0], 10 ** 9), kv[0]))
        for (ra, as_), (rb, bs) in zip(runs_sorted, runs_sorted[1:]):
            sa = [r["c_status"] for r in as_ if r["c_status"] in ("pass", "fail")]
            sb = [r["c_status"] for r in bs if r["c_status"] in ("pass", "fail")]
            if len(sa) >= 2 and all(s == "pass" for s in sa) and len(sb) >= 2 and all(s == "fail" for s in sb):
                out.append({"qkey": qkey, "from_run": ra, "to_run": rb})
    return out


def compare_axis(rows: list[dict], axis: str, fixes: dict[str, str] | None = None,
                 anys: tuple[str, ...] | set[str] = (), run_order: dict[str, int] | None = None) -> dict:
    """比较轴对比（§7.1，R3 修订）：题目 × 轴值通过矩阵 + 维度聚合 + 锚点绊线。

    放行条件（属性三类，见 ATTRS 注释）：处理条件属性必须一致、作轴或显式固定/
    声明不限；trial/run 默认不限；profile 与 model 互为派生，一个作轴另一个自动
    放行（同轴值下观测到多个模型记入 warnings，不拦截）；skills_mode 永远不可跨。
    比较按题目交集（qkey）对齐；缺判定的答案计为待重判，不参与比较。"""
    fixes = fixes or {}
    anys = set(anys)
    if axis not in ATTRS:
        raise ValueError(f"比较轴只能是 {'/'.join(ATTRS)}")
    blocked: str | None = None
    if axis == "skills_mode":
        blocked = "skills_mode 不能作比较轴（跨 mode 对比无效）"
    if "skills_mode" in anys:
        blocked = blocked or "skills_mode 不能声明为不限（跨 mode 对比无效）"
    unknown = [k for k in (*fixes, *anys) if k not in ATTRS]
    if unknown:
        raise ValueError(f"未知比较属性：{unknown}")

    sel = [r for r in rows if all(attr_value(r, k) == v for k, v in fixes.items())]
    others = [a for a in ATTRS if a != axis and a not in fixes and a not in anys
              and a not in SAMPLING_ATTRS and a != DERIVED_ATTR.get(axis)]
    inconsistent = {a: sorted({attr_value(r, a) for r in sel}) for a in others
                    if len({attr_value(r, a) for r in sel}) > 1}
    if inconsistent:
        blocked = blocked or ("比较轴之外未固定的属性取值不一致："
                              + "；".join(f"{k}={v}" for k, v in inconsistent.items()))
    axis_values = sorted({attr_value(r, axis) for r in sel})

    # 派生属性漂移：不拦截，记入 warnings（R3）
    warnings: list[str] = []
    if not blocked and axis in DERIVED_ATTR:
        other = DERIVED_ATTR[axis]
        by_axis: dict[str, set[str]] = defaultdict(set)
        for r in sel:
            by_axis[attr_value(r, axis)].add(attr_value(r, other))
        for av, others_seen in sorted(by_axis.items()):
            if len(others_seen) > 1:
                warnings.append(f"{axis}「{av}」下观测到多个 {other}：{'、'.join(sorted(others_seen))}")

    by_q: dict[str, list[dict]] = defaultdict(list)
    for r in sel:
        by_q[r["qkey"]].append(r)
    # 按题目交集对齐（§7.2）：在多个轴值上有答案的题目进入矩阵；
    # 只存在于单侧的题（降级移出/退役/输入已改）列出并排除。
    # 缺判定（待重判）不影响交集——该轴值记 pending，不参与通过率
    excluded: list[dict] = []
    included: set[str] = set()
    if len(axis_values) >= 2:
        for qkey, qrows in by_q.items():
            covered = {attr_value(r, axis) for r in qrows}
            if len(covered) >= 2:
                included.add(qkey)
            elif covered:
                excluded.append({"qkey": qkey, "reason": f"仅轴值 {sorted(covered)} 有答案"})
    else:
        included = set(by_q)
    matrix: dict[str, dict] = {}
    pending = 0
    for qkey in sorted(included):
        qrows = by_q[qkey]
        cells = {}
        for av in axis_values:
            grp = [r for r in qrows if attr_value(r, axis) == av]
            judged = [r for r in grp if r["c_status"] in ("pass", "fail")]
            pend = sum(r["c_status"] is None for r in grp)
            pending += pend
            p = sum(r["c_status"] == "pass" for r in judged)
            cells[av] = {"pass": p, "n": len(judged), "pending": pend,
                         "rate": round(p / len(judged), 4) if judged else None}
        matrix[qkey] = cells
    dims: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for r in sel:
        if r["qkey"] in included and r["c_status"] in ("pass", "fail"):
            c = dims[r["dim"] or "?"][attr_value(r, axis)]
            c[0] += r["c_status"] == "pass"
            c[1] += 1
    dims_out = {dim: {av: {"pass": c[0], "n": c[1], "rate": round(c[0] / c[1], 4) if c[1] else None,
                           "ci": wilson(c[0], c[1])}
                      for av, c in per_axis.items()}
                for dim, per_axis in sorted(dims.items())}
    tripwires = [] if blocked else anchor_tripwires(sel, run_order)
    evalset_versions = None
    if "evalset_version" in anys and not blocked:
        counts: dict[str, set[str]] = defaultdict(set)    # 版本 → 题目数（qkey）
        for qkey in included:
            for r in by_q[qkey]:
                counts[r["evalset_version"] or "?"].add(qkey)
        evalset_versions = {v: len(qs) for v, qs in sorted(counts.items())}
    return {
        "axis": axis,
        "axis_values": axis_values,
        "fixed": fixes,
        "any": sorted(anys),
        "blocked": blocked,
        "inconsistent": inconsistent,
        "warnings": warnings,
        "matrix": matrix,
        "excluded_qkeys": excluded,
        "dims": dims_out,
        "evalset_versions": evalset_versions,
        "anchor_tripwires": tripwires,
        "pending_answers": pending,
    }


# ---------------------------------------------------------------- 出口 3：用例质量（DB 版）

def quality_from_db(store, rows: list[dict], chain_sha: str) -> dict:
    """人工与机器不一致清单、规则判定一致率、注入 found>1、无效清单、forced 前提
    成立率偏低的 ec、变体泄露嫌疑清单（C5）。全部经评测库聚合取数（R7）：
    不一致/一致率一条 SQL；found>1 与变体泄露读 answers 预计算列，不解析 record。"""
    mismatch, rule_agree, rule_total = store.human_machine_compare(chain_sha)
    invalid: list[str] = []
    found_gt1: set[str] = set()
    forced_ec: dict[str, list[str]] = defaultdict(list)
    leaks: list[dict] = []
    for r in rows:
        if r["c_status"] == "invalid":
            invalid.append(f'{r["uid"]}|{r["mode"]}|t{r["trial"]}')
        if r.get("injection_found_gt1"):
            found_gt1.add(r["uid"])
        if r["c_status"] and r["mode"] == "forced" and r["dim"] == "错误知识纠正":
            forced_ec[r["uid"]].append(r["c_status"])
        leaks.extend(json.loads(r.get("variant_leak_json") or "[]"))
    forced_ec_low = [uid for uid, sts in sorted(forced_ec.items())
                     if len(sts) >= 2 and sts.count("invalid") / len(sts) > 0.5]
    return {
        "human_machine_mismatch": mismatch,
        "rule_agreement": {"agree": rule_agree, "total": rule_total,
                           "rate": round(rule_agree / rule_total, 4) if rule_total else None},
        "injection_found_gt1": sorted(found_gt1),
        "invalid_episodes": invalid,
        "forced_ec_precond_low": forced_ec_low,
        "variant_leak_suspects": leaks,
    }
