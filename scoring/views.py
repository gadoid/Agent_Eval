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


def _axis_selection(rows: list[dict], axis: str, fixes: dict[str, str], anys: set[str]) -> dict:
    """比较轴的放行校验与选数（R3 三类属性规则；compare_axis 与 compare_scores 共用）。"""
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
    warnings: list[str] = []
    if not blocked and axis in DERIVED_ATTR:
        other = DERIVED_ATTR[axis]
        by_axis: dict[str, set[str]] = defaultdict(set)
        for r in sel:
            by_axis[attr_value(r, axis)].add(attr_value(r, other))
        for av, others_seen in sorted(by_axis.items()):
            if len(others_seen) > 1:
                warnings.append(f"{axis}「{av}」下观测到多个 {other}：{'、'.join(sorted(others_seen))}")
    return {"sel": sel, "axis_values": axis_values, "blocked": blocked,
            "inconsistent": inconsistent, "warnings": warnings}


def compare_axis(rows: list[dict], axis: str, fixes: dict[str, str] | None = None,
                 anys: tuple[str, ...] | set[str] = (), run_order: dict[str, int] | None = None) -> dict:
    """比较轴对比（§7.1，R3 修订）：题目 × 轴值通过矩阵 + 维度聚合 + 锚点绊线。

    放行条件（属性三类，见 ATTRS 注释）：处理条件属性必须一致、作轴或显式固定/
    声明不限；trial/run 默认不限；profile 与 model 互为派生，一个作轴另一个自动
    放行（同轴值下观测到多个模型记入 warnings，不拦截）；skills_mode 永远不可跨。
    比较按题目交集（qkey）对齐；缺判定的答案计为待重判，不参与比较。"""
    fixes = fixes or {}
    anys = set(anys)
    base = _axis_selection(rows, axis, fixes, anys)
    sel, axis_values = base["sel"], base["axis_values"]
    blocked, inconsistent, warnings = base["blocked"], base["inconsistent"], base["warnings"]

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

# ================================================================ 百分制聚合（doc/10 §6.3–6.4）

import random

# 被测配置：会话分聚合的分组建（mode 不进键——headline 按维度各取一个干预）
CONFIG_ATTRS = ("profile", "model", "skills_mode", "skills_base", "evalset_version")


def config_value(row: dict) -> tuple:
    return tuple(attr_value(row, a) for a in CONFIG_ATTRS)


def config_label(row: dict) -> str:
    return str(row.get("profile_id") or "?")


def _bootstrap(values: list[float], stat, b: int = 1000, seed: int = 20261023) -> tuple[float, float]:
    """自助法 95% 区间（固定种子可复现，纯标准库）。"""
    if not values:
        return 0.0, 0.0
    rng = random.Random(seed)
    stats = sorted(stat([values[rng.randrange(len(values))] for _ in range(len(values))]) for _ in range(b))
    return round(stats[int(0.025 * b)], 1), round(stats[min(b - 1, int(0.975 * b))], 1)


def _headline_rows(rows: list[dict], policy) -> list[dict]:
    """只取各维度主口径（headline_mode）的会话。"""
    return [r for r in rows if r["mode"] == policy.headline(r["dim"] or "?")]


def question_scores(rows: list[dict], policy, headline: bool = True) -> dict[str, dict]:
    """题目分（§6.3）：同一配置下有确定会话分的会话平均；有未决被排除时标临时。
    输入 rows 应已限到一个配置（config_value 一致）。headline=False 供比较用
    （比较两侧本就被轴限定，mode 轴的 natural 侧不能被 ec 主口径滤掉）。"""
    out: dict[str, dict] = {}
    by_q: dict[str, list[dict]] = defaultdict(list)
    sel = _headline_rows(rows, policy) if headline else rows
    for r in sel:
        by_q[r["qkey"]].append(r)
    for qkey, rs in by_q.items():
        scored = [r["c_score"] for r in rs if r["c_score"] is not None]
        out[qkey] = {
            "score": round(sum(scored) / len(scored), 1) if scored else None,
            "n": len(scored),
            "provisional": any(r["c_status"] == "undecided" for r in rs),
            "undecided": sum(r["c_status"] == "undecided" for r in rs),
            "invalid": sum(r["c_status"] == "invalid" for r in rs),
            "pending": sum(r["c_status"] is None for r in rs),
            "dim": rs[0]["dim"] or "?", "case_id": rs[0]["case_id"],
            "rewrite": rs[0]["rewrite"], "uid": rs[0]["uid"],
        }
    return out


def case_scores(rows: list[dict], policy, headline: bool = True) -> dict[str, dict]:
    """用例分（§6.3）：用例内各题目分平均；诱导变体（exclude_rewrites）不计入；
    没有确定会话分的题目进体检（all_unscored）不进用例分。"""
    qs = question_scores(rows, policy, headline=headline)
    by_case: dict[str, dict[str, dict]] = defaultdict(dict)
    for qkey, q in qs.items():
        by_case[q["case_id"]][qkey] = q
    out: dict[str, dict] = {}
    for case, members in by_case.items():
        counted = [q for q in members.values()
                   if q["rewrite"] not in policy.score.exclude_rewrites and q["score"] is not None]
        all_unscored = [q["uid"] for qid, q in sorted(members.items())
                        if q["rewrite"] not in policy.score.exclude_rewrites and q["score"] is None]
        out[case] = {
            "score": round(sum(q["score"] for q in counted) / len(counted), 1) if counted else None,
            "n": len(counted), "questions": len(members),
            "provisional": any(q["provisional"] for q in members.values()),
            "all_unscored": all_unscored,
            "dim": next(iter(members.values()))["dim"],
        }
    return out


def dimension_scores(rows: list[dict], policy, headline: bool = True) -> dict[str, dict]:
    """维度分（§6.3）：dimension_unit=case 按用例分平均（question 按题目分）；
    95% 区间按用例（题目）自助法；维度临时 = 其下任一会话未决被排除。"""
    unit = policy.score.dimension_unit
    cs = case_scores(rows, policy, headline=headline)
    qs = question_scores(rows, policy, headline=headline)
    _rows_h = _headline_rows(rows, policy) if headline else rows
    provisional_dims = {r["dim"] or "?" for r in _rows_h if r["c_status"] == "undecided"}
    groups: dict[str, list[float]] = defaultdict(list)
    for case, c in cs.items():
        if c["score"] is not None:
            groups.setdefault(c["dim"], []).append(c["score"])
    if unit == "question":
        groups = defaultdict(list)
        for q in qs.values():
            if q["rewrite"] not in policy.score.exclude_rewrites and q["score"] is not None:
                groups.setdefault(q["dim"], []).append(q["score"])
    meta: dict[str, dict] = {}
    for dim, vals in groups.items():
        meta[dim] = {
            "score": round(sum(vals) / len(vals), 1),
            "n": len(vals), "unit": unit,
            "ci": _bootstrap(vals, lambda xs: sum(xs) / len(xs)),
            "provisional": dim in provisional_dims,
        }
    return meta


def composite(dims: dict[str, dict], policy, known_dims: set[str] | None = None) -> dict:
    """综合分（§6.3）：Σ 维度权重 × 维度分 / Σ 权重；缺已知维度按已有归一并标不完整；
    任一参与维度临时则综合分临时。"""
    weights = policy.score.dimensions
    present = {d: v for d, v in dims.items() if v.get("score") is not None}
    if not present:
        return {"score": None, "complete": False, "provisional": False, "parts": {}}
    known = known_dims if known_dims is not None else set(weights)
    wsum = sum(float(weights.get(d, 1)) for d in present)
    return {
        "score": round(sum(float(weights.get(d, 1)) * v["score"] for d, v in present.items()) / wsum, 1),
        "complete": set(present) >= {d for d in known if d} or not known,
        "provisional": any(v.get("provisional") for v in present.values()),
        "parts": {d: {"score": v["score"], "weight": float(weights.get(d, 1)),
                      "ci": v.get("ci"), "provisional": v.get("provisional")}
                  for d, v in sorted(present.items())},
    }


def score_summary(rows: list[dict], policy) -> dict:
    """一个配置的完整分数表（题目 / 用例 / 维度 / 综合分）。"""
    return {"questions": question_scores(rows, policy),
            "cases": case_scores(rows, policy),
            "dims": dimension_scores(rows, policy)}


def _side_metrics(rows: list[dict], qkeys: set[str], policy) -> dict:
    sub = [r for r in rows if r["qkey"] in qkeys]
    dims = dimension_scores(sub, policy, headline=False)
    return {"dims": dims, "composite": composite(dims, policy, known_dims=set(dims))}


def _dim_delta(cs: list[str], dim: str, case_of: dict, qs: dict, base: str, target: str) -> float:
    """配对自助法的一步：重采样用例集合上某维度的两侧差。"""
    def side(av):
        vals = []
        for q, meta in qs[av].items():
            if q in case_of and case_of[q] in cs and meta["dim"] == dim and meta["score"] is not None:
                vals.append(meta["score"])
        return sum(vals) / len(vals) if vals else 0.0
    return side(target) - side(base)


def compare_scores(rows: list[dict], axis: str, base: str, target: str, policy,
                   fixes: dict[str, str] | None = None, anys: tuple[str, ...] | set[str] = (),
                   run_order: dict[str, int] | None = None) -> dict:
    """分数对比（doc/10 §5.5）：放行规则沿用 compare_axis（_axis_selection）；
    两侧在共有题目上分别算题目/维度/综合分，输出单组件贡献（配对自助法区间）、
    逐题差异（只列超过 compare_delta 的）与锚点绊线。"""
    fixes = dict(fixes or {})
    info = _axis_selection(rows, axis, fixes, set(anys))
    sel, axis_values = info["sel"], info["axis_values"]
    if info["blocked"]:
        return {**info, "base": base, "target": target, "matrix": {}}
    if base not in axis_values or target not in axis_values:
        raise ValueError(f"基线/对照必须是轴上的取值：{axis_values}")
    sides = {av: [r for r in sel if attr_value(r, axis) == av] for av in (base, target)}
    qs = {av: question_scores(sides[av], policy, headline=False) for av in sides}
    shared = {q for q in set(qs[base]) & set(qs[target])
              if qs[base][q]["score"] is not None and qs[target][q]["score"] is not None}
    excluded = [{"qkey": q, "reason": "仅单侧有题目（答案）"} for q in sorted(set(qs[base]) ^ set(qs[target]))]
    for q in sorted(set(qs[base]) & set(qs[target])):
        if q not in shared and not any(e["qkey"] == q for e in excluded):
            excluded.append({"qkey": q, "reason": "某侧没有确定会话分"})
    case_of = {q: qs[base][q]["case_id"] for q in shared}
    dim_of = {q: qs[base][q]["dim"] for q in shared}
    wires = anchor_tripwires(sel, run_order)
    tripwires = {t["qkey"] for t in wires}

    metrics = {av: _side_metrics(sides[av], shared, policy) for av in sides}
    cases = sorted({case_of[q] for q in shared})
    by_case = {av: defaultdict(list) for av in sides}
    for q in shared:
        by_case[base][case_of[q]].append(qs[base][q]["score"])
        by_case[target][case_of[q]].append(qs[target][q]["score"])
    case_mean = {av: {c: sum(v) / len(v) for c, v in by_case[av].items()} for av in sides}

    def paired(fn) -> tuple[float, float]:
        return _bootstrap(cases, fn)

    base_c = metrics[base]["composite"]["score"]
    target_c = metrics[target]["composite"]["score"]
    # 综合分差按两侧维度差加权平均（§6.4 公式），不用已舍入综合分相减（避免 76.8−73.9=2.9 的舍入差）
    weights = policy.score.dimensions
    shared_dims = [d for d in set(metrics[base]["dims"]) & set(metrics[target]["dims"])
                   if metrics[base]["dims"][d].get("score") is not None
                   and metrics[target]["dims"][d].get("score") is not None]
    if shared_dims:
        wsum = sum(float(weights.get(d, 1)) for d in shared_dims)
        delta = round(sum(float(weights.get(d, 1)) *
                          (metrics[target]["dims"][d]["score"] - metrics[base]["dims"][d]["score"])
                          for d in shared_dims) / wsum, 1)
    else:
        delta = round(target_c - base_c, 1) if base_c is not None and target_c is not None else None
    lo, hi = paired(lambda cs: (sum(case_mean[target][c] for c in cs) / len(cs)
                                - sum(case_mean[base][c] for c in cs) / len(cs)) if cs else 0.0)
    contributions = [{"name": "综合分", "delta": delta, "lo": lo, "hi": hi,
                      "base": base_c, "target": target_c}]
    all_dims = sorted(set(metrics[base]["dims"]) | set(metrics[target]["dims"]))
    for dim in all_dims:
        b = metrics[base]["dims"].get(dim, {}).get("score")
        t = metrics[target]["dims"].get(dim, {}).get("score")
        d = round(t - b, 1) if b is not None and t is not None else None
        lo_d, hi_d = paired(lambda cs, dim=dim: _dim_delta(cs, dim, case_of, qs, base, target))
        contributions.append({"name": dim, "delta": d, "lo": lo_d, "hi": hi_d, "base": b, "target": t})
    questions = []
    for q in sorted(shared):
        b, t = qs[base][q]["score"], qs[target][q]["score"]
        if b is None or t is None:
            continue
        d = round(t - b, 1)
        if abs(d) >= policy.score.compare_delta or q in tripwires:
            questions.append({"qkey": q, "uid": qs[base][q]["uid"], "case_id": case_of[q], "dim": dim_of[q],
                              "base": b, "target": t, "delta": d, "anchor": q in tripwires})
    questions.sort(key=lambda x: -abs(x["delta"]))
    return {**info, "base": base, "target": target,
            "n_shared": len(shared), "n_shared_cases": len(cases),
            "metrics": metrics, "contributions": contributions,
            "questions": questions, "excluded_qkeys": excluded,
            "anchor_tripwires": wires}


def shapley(cells: dict, factors: list[str]) -> dict:
    """多组件分摊（§6.4）：k ≤ 3 个两水平组件的全因子 Shapley 值。
    cells：处于「对照」水平的组件集合（frozenset 或序列）→ 该组合的综合分。
    缺格返回 missing（列出需补跑的组合）。"""
    from math import factorial
    cells = {frozenset(c): v for c, v in cells.items()}
    k = len(factors)
    if k == 0 or k > 3:
        raise ValueError("组件个数须在 1–3 之间")
    combos = [frozenset(factors[j] for j in range(k) if i >> j & 1) for i in range(2 ** k)]
    missing = [sorted(c) for c in combos if cells.get(c) is None]
    if missing:
        return {"missing": [" × ".join(m) if m else "（基线）" for m in missing]}
    phi = {}
    for f in factors:
        v = 0.0
        for s in (c for c in combos if f not in c):
            v += (factorial(len(s)) * factorial(k - len(s) - 1) / factorial(k)) * (cells[s | {f}] - cells[s])
        phi[f] = round(v, 1)
    return {"phi": phi, "total": round(cells[frozenset(factors)] - cells[frozenset()], 1),
            "cells": {" × ".join(sorted(c)) if c else "（基线）": val
                      for c, val in sorted(cells.items())}}


def judge_agreement(store, chain_sha: str, policy) -> list[dict]:
    """判定质量主表（§5.6）：算子 × 条目类型的人工复核数、平均分差、分差 ≤ 10 占比、
    分歧数。机器分 = 显式分 ?? map[结论词]，人工分同；只看有效改判（sha 匹配）。"""
    m = policy.score.map
    from .items import SCALES
    sql = """SELECT v.answer_id, v.item_id, v.kind, v.value AS machine_value, v.score AS machine_score,
             v.source, v.operator, r.value AS review_value, r.score AS review_score, r.review_id
             FROM verdicts v
             JOIN answers a ON a.answer_id = v.answer_id
             JOIN questions q ON q.qkey = a.qkey AND q.current_judge_fp = v.judge_fp
             JOIN reviews r ON r.answer_id = v.answer_id AND r.item_id = v.item_id
                AND r.answer_sha = v.answer_sha AND r.item_sha = v.item_sha
             WHERE v.chain_sha = ?"""
    best: dict[tuple, dict] = {}
    for row in store.conn.execute(sql, (chain_sha,)):
        d = dict(row)
        if d["review_value"] not in SCALES.get(d["kind"], ()):
            continue
        key = (d["answer_id"], d["item_id"])
        if key not in best or d["review_id"] > best[key]["review_id"]:
            best[key] = d
    agg: dict[tuple, dict] = defaultdict(lambda: {"n": 0, "diffs": [], "within": 0, "disagree": set()})
    for d in best.values():
        ms = d["machine_score"] if d["machine_score"] is not None else m.get(d["machine_value"])
        hs = d["review_score"] if d["review_score"] is not None else m.get(d["review_value"])
        if ms is None or hs is None:
            continue
        g = agg[(d["operator"] or "?", d["source"] or "?", d["kind"])]
        g["n"] += 1
        diff = abs(float(ms) - float(hs))
        g["diffs"].append(diff)
        g["within"] += diff <= 10
        if d["machine_value"] != d["review_value"] or diff > 10:
            g["disagree"].add(d["answer_id"])
    out = []
    for (op, src, kind), g in sorted(agg.items()):
        n = g["n"]
        ci = wilson(g["within"], n)
        out.append({"operator": op, "source": src, "kind": kind, "n": n,
                    "mean_diff": round(sum(g["diffs"]) / n, 1),
                    "within": g["within"], "within_ci": ci,
                    "disagreements": len(g["disagree"]),
                    "suggest": "可转抽检" if n >= 30 and ci[0] >= 0.9
                    else ("样本不足，继续全量复核" if n < 30 else "分差偏大，继续全量复核")})
    return out
