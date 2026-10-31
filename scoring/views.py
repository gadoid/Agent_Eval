"""分析出口（方案 §10、评测结果管理改造方案 §7/§8）：能力/归因/质量视图、
题目级统计与比较轴。全部视图从评测库取数（store.answer_view_rows /
effective_rows），不再读 run 目录文件，也不在请求中全量重算。
"""
from __future__ import annotations

import json
import math
from collections import defaultdict

from .rules import DEFAULT_METHOD, EpisodeVerdict, Method, Rules

# 答案的比较属性（§4.2，R3 修订后分三类；doc/18 §3 增 persona/tools）：
# - 重复采样（trial、run）：默认不限，无需声明——它们是采样标识，不是比较条件
# - 处理条件（profile、model、mode、skills_base、evalset_version、persona）：必须一致，
#   或作为比较轴，或显式固定 / 声明不限
# - 不可比较（skills_mode）：必须一致，不能作轴、不能不限
# - 观测字段（tools，answers.tools_sha）：可作轴；不作轴时不按不一致拦截（旧 run 无观测
#   是常态），真实漂移（多个非空取值）只告警——生效工具的变化由 tools 轴显式对比
ATTRS = ("subject", "model", "effort", "agent", "persona", "tools", "skills_mode", "skills_base",
         "mode", "trial", "evalset_version", "run")
SAMPLING_ATTRS = frozenset({"trial", "run"})
# subject 是被测配置整指纹（doc/15 §3.1 + doc/18 §2.1）；model/effort/agent/persona/
# skills_base/skills_mode 是它的拆字段——一个作轴，其余自动放行（拆字段作轴时 subject
# 整体随之放行；persona 是拆字段、tools 是观测字段，二者作轴也放行 subject，但
# DERIVED_ATTR 不加新键：persona↔subject 会覆盖现有 subject→model，且告警文案会错位）
SUBJECT_FIELDS = ("model", "effort", "agent", "persona", "skills_base", "skills_mode")
DERIVED_ATTR = {"subject": "model", "model": "subject",
                "effort": "subject", "agent": "subject",
                "skills_base": "subject", "skills_mode": "subject"}

# 小样本阈值（doc/18 §8）：只影响展示标注，不进任何指纹
SMALL_SAMPLE_N = 30


def follows(axis: str) -> set[str]:
    """作轴后自动放行的属性（派生/同源）。"""
    out = {axis}
    if axis == "subject":
        out |= set(SUBJECT_FIELDS)
    elif axis in DERIVED_ATTR:
        out.add(DERIVED_ATTR[axis])
    elif axis in ("persona", "tools"):
        out.add("subject")
    return out


# ---------------------------------------------------------------- 非 ATTR subject 字段检查（doc/18 §3）

# subject 放行后仍需显式检查的配置面（不属于 ATTRS，被 subject 豁免静默带过）：
# plugins_sha 按轴放行（见 subject_extra_check）；routing/skill_tool 直接改变会话行为，
# 不一致拦截；model_provider/dsh_profile 是名称性字段（效果已由 model 与 eline 内容
# 承载），不一致只告警。subject 自身作轴（或作 Shapley 因子）时整体跳过——
# subject 整体就是用来跨这些字段做对比的（routing A/B 的出路）。
WARN_SUBJECT_FIELDS = ("model_provider", "dsh_profile")


def _subject_fields_of(row: dict) -> dict:
    try:
        s = json.loads(row.get("subject_json") or "{}")
        return s if isinstance(s, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _sub_val(s: dict, f: str) -> str:
    v = s.get(f)
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, sort_keys=True)
    return "—" if v is None else str(v)


def subject_extra_check(rows: list[dict], axes: set[str]) -> tuple[dict, list[str]]:
    """非 ATTR subject 字段一致性检查（compare 与 shapley 共用，doc/18 §3）。

    axes：作轴的属性集合（compare 单轴；shapley 为因子集），含 subject 时跳过。
    plugins_sha：tools 作轴时跨轴值允许不同（工具增减本意）、同一轴值内必须一致
    （同轴值内 tools_sha 相同而 plugins_sha 不同 = 变的是不暴露工具的集成代码）；
    其余轴下必须一致。行缺 subject_json（测试行/旧查询）时跳过该行。"""
    if "subject" in axes:
        return {}, []
    cache: dict[str, dict] = {}                    # 同一 run 的 subject_json 只解析一次

    def fields_of(r: dict) -> dict:
        key = r.get("subject_json")
        if key not in cache:
            try:
                v = json.loads(key or "{}")
                cache[key] = v if isinstance(v, dict) else {}
            except (json.JSONDecodeError, TypeError):
                cache[key] = {}
        return cache[key]

    have = [(r, s) for r in rows if (s := fields_of(r))]
    if not have:
        return {}, []
    blocked: dict[str, list[str]] = {}
    warns: list[str] = []
    if "tools" in axes:
        by_tv: dict[str, set[str]] = defaultdict(set)
        for r, s in have:
            by_tv[attr_value(r, "tools")].add(_sub_val(s, "plugins_sha"))
        bad = {tv: sorted(vs) for tv, vs in by_tv.items() if len(vs) > 1}
        if bad:
            blocked["plugins_sha"] = [f"轴值 {tv} 内不一致：{'、'.join(vs)}"
                                      for tv, vs in sorted(bad.items())]
    else:
        vs = {_sub_val(s, "plugins_sha") for _, s in have}
        if len(vs) > 1:
            blocked["plugins_sha"] = sorted(vs)
    for f in ("routing", "skill_tool"):
        vs = {_sub_val(s, f) for _, s in have}
        if len(vs) > 1:
            blocked[f] = sorted(vs)
    for f in WARN_SUBJECT_FIELDS:
        vs = {_sub_val(s, f) for _, s in have}
        if len(vs) > 1:
            warns.append(f"{f} 不一致：{'、'.join(sorted(vs))}")
    return blocked, warns


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

def ability_view(verdicts: list[EpisodeVerdict], rules: Rules, method: Method | None = None) -> dict:
    """dim × mode 的通过率与前提成立率（Wilson 区间）；headline 由快照测法决定。"""
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
            "skipped": sum(v.status == "skipped" for v in vs),   # 权重未分配（doc/17 §4）
            "pass_direct": rate(direct),
            "pass_after_ask": rate(after),
            "headline": (method or DEFAULT_METHOD).headline(dim) == mode,
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
    if name in ("subject", "profile"):          # profile 为旧名别名（旧链接兼容）
        return str(row.get("subject_fp") or row.get("profile_id") or "?")
    if name == "model":
        return str(row.get("model") or model_label(row))
    if name == "effort":
        return str(row.get("reasoning_effort") or "?")
    if name == "agent":
        return str(row.get("agent") or "?")
    if name == "persona":                        # persona_sha 落 answers.persona（doc/18 §2.2）
        return str(row.get("persona") or "?")
    if name == "tools":                          # 观测字段：轴名与列名不同（doc/18 §2.2）
        return str(row.get("tools_sha") or "?")
    if name == "run":
        return str(row.get("run_id"))
    if name == "evalset_version":
        from .items import version_label
        return version_label(row.get(name))
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
    follow = follows(axis)
    # tools 是观测字段（doc/18 §3）：不作轴时无观测（"?"）是旧 run 常态，不按不一致拦截；
    # 多个非空取值才是工具清单真实漂移，只告警
    others = [a for a in ATTRS if a not in follow and a not in fixes and a not in anys
              and a not in SAMPLING_ATTRS and a != "tools"]
    inconsistent = {a: sorted({attr_value(r, a) for r in sel}) for a in others
                    if len({attr_value(r, a) for r in sel}) > 1}
    if inconsistent:
        blocked = blocked or ("比较轴之外未固定的属性取值不一致："
                              + "；".join(f"{k}={v}" for k, v in inconsistent.items()))
    axis_values = sorted({attr_value(r, axis) for r in sel})
    warnings: list[str] = []
    if "tools" not in follow and "tools" not in fixes and "tools" not in anys:
        tvs = {attr_value(r, "tools") for r in sel} - {"?"}
        if len(tvs) > 1:
            warnings.append(f"工具清单观测不一致（{sorted(tvs)}）：同一选数内生效工具发生变化")
    # 观测/配置轴缺值拦截（doc/18 §3）："?" 不构成可对比的轴值
    if axis in ("persona", "tools") and "?" in axis_values:
        hint = ("旧 run 无工具观测，tools 轴只对新 run 可用" if axis == "tools"
                else "persona 取不到配置（bundle/缺失），persona 轴不可用")
        blocked = blocked or f"{axis} 轴遇到无取值的答案：{hint}"
    # 非 ATTR subject 字段检查（doc/18 §3）：subject 作轴时跳过（跨字段对比的出路）
    if not blocked and axis != "subject":
        extra_blocked, extra_warns = subject_extra_check(sel, {axis})
        if extra_blocked:
            blocked = ("被测配置的非轴字段不一致："
                       + "；".join(f"{k}={v}" for k, v in extra_blocked.items()))
        warnings.extend(extra_warns)
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

# 被测配置：会话分聚合的分组建 = subject_fp 整指纹（doc/15 §3.6；评测集版本已退出
# 配置身份——同一被测配置跨快照的分数自动合并）。标签按指纹差异属性拼（serve 侧）。
def config_value(row: dict) -> tuple:
    return (attr_value(row, "subject"),)


def config_label(row: dict) -> str:
    return str(row.get("subject_fp") or row.get("profile_id") or "?")


def _bootstrap(values: list[float], stat, b: int = 1000, seed: int = 20261023) -> tuple[float, float]:
    """自助法 95% 区间（固定种子可复现，纯标准库）。"""
    if not values:
        return 0.0, 0.0
    rng = random.Random(seed)
    stats = sorted(stat([values[rng.randrange(len(values))] for _ in range(len(values))]) for _ in range(b))
    return round(stats[int(0.025 * b)], 1), round(stats[min(b - 1, int(0.975 * b))], 1)


def contrib_scale(contributions: list[dict]) -> float:
    """组件贡献图的对称刻度：要同时盖住点估计和 95% 区间，否则区间线会画到轨道外。"""
    vals = [abs(float(c[k])) for c in contributions if c.get("delta") is not None
            for k in ("delta", "lo", "hi") if c.get(k) is not None]
    return max(vals, default=0.0) or 1.0


def formal_rows(rows: list[dict]) -> list[dict]:
    """正式会话（doc/15 §3.2 C5）：诊断执行（runs.diagnostic）的会话不进分数，
    只在对比页的诊断视图里看。"""
    return [r for r in rows if not r.get("diagnostic")]


def diagnostic_rows(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r.get("diagnostic")]


def _headline_rows(rows: list[dict], method: Method | None = None) -> list[dict]:
    """只取各维度主口径（快照测法 modes，原 headline_mode）的会话。"""
    m = method or DEFAULT_METHOD
    return [r for r in rows if r["mode"] == m.headline(r["dim"] or "?")]


def _unsettled(r: dict) -> bool:
    """被排除出均分但结论未落定的会话（§6.2「被排除即标临时」）：未决 / 判定异常 /
    结论已定但区间未收敛（score 为空）。invalid 是合规排除（前提不成立不计分）。"""
    if r["c_status"] in ("undecided", "error"):
        return True
    return r["c_status"] in ("pass", "fail") and r["c_score"] is None


def _select_mode_per_qkey(sel: list[dict], axis: str, base: str, target: str,
                          method: Method | None = None) -> tuple[list[dict], list[dict]]:
    """mode 未固定且不作轴时的逐题口径选择（§6.3 只取 headline_mode）：
    同一 qkey 两侧都有 headline_mode 会话 → 只取主口径；两侧只有同一个别的 mode →
    按该 mode 比（不放走可比的自然数据）；两侧 mode 不同 → 该题不进比较。
    返回（过滤后的行，被排除的 qkey 及原因）。"""
    by: dict[tuple, list[dict]] = defaultdict(list)
    for r in sel:
        by[(r["qkey"], attr_value(r, axis))].append(r)
    out: list[dict] = []
    excluded: list[dict] = []
    for (q, av), rs in by.items():
        if av not in (base, target):
            out.extend(rs)                     # 其他轴值的行与本题无关（调用方只取两侧）
            continue
        other = target if av == base else base
        if not by.get((q, other)):
            out.extend(rs)                     # 单侧题：交给既有「仅单侧」排除
            continue
        b_rs = by[(q, base)]
        h = (method or DEFAULT_METHOD).headline(b_rs[0]["dim"] or "?")
        modes = {a: {r["mode"] for r in by[(q, a)]} for a in (base, target)}
        if all(h in ms for ms in modes.values()):
            mode = h
        elif len(modes[base]) == 1 and modes[base] == modes[target]:
            mode = next(iter(modes[base]))
        else:
            excluded.append({"qkey": q, "reason": f"两侧干预模式不一致（{q} 的主口径 {h} 只在一侧）"})
            continue
        out.extend(r for r in rs if r["mode"] == mode)
    return out, excluded


def question_scores(rows: list[dict], rules: Rules, method: Method | None = None,
                   headline: bool = True) -> dict[str, dict]:
    """题目分（§6.3）：同一配置下有确定会话分的会话平均；有未决被排除时标临时。
    输入 rows 应已限到一个配置（config_value 一致）。headline=False 供比较用
    （比较两侧本就被轴限定，mode 轴的 natural 侧不能被 ec 主口径滤掉）。"""
    out: dict[str, dict] = {}
    by_q: dict[str, list[dict]] = defaultdict(list)
    sel = _headline_rows(rows, method) if headline else rows
    for r in sel:
        by_q[r["qkey"]].append(r)
    for qkey, rs in by_q.items():
        scored = [r["c_score"] for r in rs if r["c_score"] is not None]
        out[qkey] = {
            "score": round(sum(scored) / len(scored), 1) if scored else None,
            "n": len(scored),
            "provisional": any(_unsettled(r) for r in rs),
            "undecided": sum(r["c_status"] == "undecided" for r in rs),
            "invalid": sum(r["c_status"] == "invalid" for r in rs),
            "skipped": sum(r["c_status"] == "skipped" for r in rs),
            "pending": sum(r["c_status"] is None for r in rs),
            "dim": rs[0]["dim"] or "?", "case_id": rs[0]["case_id"],
            "rewrite": rs[0]["rewrite"], "uid": rs[0]["uid"],
        }
    return out


def case_scores(rows: list[dict], rules: Rules, method: Method | None = None,
                headline: bool = True) -> dict[str, dict]:
    """用例分（§6.3）：用例内各题目分平均；诱导变体（exclude_rewrites）不计入；
    没有确定会话分的题目进体检（all_unscored）不进用例分。"""
    qs = question_scores(rows, rules, method=method, headline=headline)
    by_case: dict[str, dict[str, dict]] = defaultdict(dict)
    for qkey, q in qs.items():
        by_case[q["case_id"]][qkey] = q
    out: dict[str, dict] = {}
    for case, members in by_case.items():
        counted = [q for q in members.values()
                   if q["rewrite"] not in rules.aggregate.scored_rewrites_exclude and q["score"] is not None]
        all_unscored = [q["uid"] for qid, q in sorted(members.items())
                        if q["rewrite"] not in rules.aggregate.scored_rewrites_exclude and q["score"] is None]
        out[case] = {
            "score": round(sum(q["score"] for q in counted) / len(counted), 1) if counted else None,
            "n": len(counted), "questions": len(members),
            "provisional": any(q["provisional"] for q in members.values()),
            "all_unscored": all_unscored,
            "dim": next(iter(members.values()))["dim"],
        }
    return out


def case_health(rows: list[dict], cases: dict[str, dict] | None = None) -> dict[str, list[str]]:
    """用例体检标记（§5.4）：变体泄露嫌疑、全部采样无效、注入 found>1、
    前提不成立、forced 前提成立率偏低。一次扫描，用例页与用例详情共用。"""
    leak, inj, invalid, skipped = set(), set(), set(), set()
    forced: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        cid = r["case_id"]
        if json.loads(r.get("variant_leak_json") or "[]"):
            leak.add(cid)
        if r.get("injection_found_gt1"):
            inj.add(cid)
        if r["c_status"] == "invalid":
            invalid.add(cid)
        if r["c_status"] == "skipped":
            skipped.add(cid)
        if r["mode"] == "forced" and r["c_status"]:
            forced[cid].append(r["c_status"])
    out: dict[str, list[str]] = {}
    for cid in {r["case_id"] for r in rows}:
        flags = []
        if cid in leak:
            flags.append("变体泄露嫌疑")
        if (cases or {}).get(cid, {}).get("all_unscored"):
            flags.append("全部采样无效")
        if cid in inj:
            flags.append("注入 found>1")
        if cid in invalid:
            flags.append("前提不成立")
        if cid in skipped:
            flags.append("权重未分配（跳过评测）")
        sts = forced.get(cid) or []
        if len(sts) >= 2 and sts.count("invalid") / len(sts) > 0.5:
            flags.append("forced 前提成立率偏低")
        out[cid] = flags
    return out


def dimension_scores(rows: list[dict], rules: Rules, method: Method | None = None,
                     headline: bool = True) -> dict[str, dict]:
    """维度分（§6.3）：dimension_unit=case 按用例分平均（question 按题目分）；
    95% 区间按用例（题目）自助法；维度临时 = 其下任一会话未决被排除。"""
    unit = rules.aggregate.dimension_unit
    cs = case_scores(rows, rules, method=method, headline=headline)
    qs = question_scores(rows, rules, method=method, headline=headline)
    _rows_h = _headline_rows(rows, method) if headline else rows
    provisional_dims = {r["dim"] or "?" for r in _rows_h if _unsettled(r)}
    groups: dict[str, list[float]] = defaultdict(list)
    for case, c in cs.items():
        if c["score"] is not None:
            groups.setdefault(c["dim"], []).append(c["score"])
    if unit == "question":
        groups = defaultdict(list)
        for q in qs.values():
            if q["rewrite"] not in rules.aggregate.scored_rewrites_exclude and q["score"] is not None:
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


def composite(dims: dict[str, dict], rules: Rules, known_dims: set[str] | None = None) -> dict:
    """综合分（§6.3）：Σ 维度权重 × 维度分 / Σ 权重；缺已知维度按已有归一并标不完整；
    任一参与维度临时则综合分临时。"""
    weights = rules.aggregate.dimensions
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


def score_summary(rows: list[dict], rules: Rules, method: Method | None = None) -> dict:
    """一个配置的完整分数表（题目 / 用例 / 维度 / 综合分）。"""
    return {"questions": question_scores(rows, rules, method=method),
            "cases": case_scores(rows, rules, method=method),
            "dims": dimension_scores(rows, rules, method=method)}


def _side_metrics(rows: list[dict], qkeys: set[str], rules: Rules) -> dict:
    sub = [r for r in rows if r["qkey"] in qkeys]
    dims = dimension_scores(sub, rules, headline=False)
    return {"dims": dims, "composite": composite(dims, rules, known_dims=set(dims))}


def _dim_delta(cs: list[str], dim: str, per_case: dict, base: str, target: str) -> float:
    """配对自助法的一步：重采样用例集合（含重复抽中的重数）上某维度的两侧差。
    per_case[av][case][dim] = [Σ题目分, 条数]。"""
    def side(av):
        tot = n = 0.0
        for c in cs:                      # cs 含重复 → 同一用例的聚合值重复计入
            cell = per_case[av].get(c, {}).get(dim)
            if cell:
                tot += cell[0]
                n += cell[1]
        return tot / n if n else 0.0
    return side(target) - side(base)


def compare_scores(rows: list[dict], axis: str, base: str, target: str, rules: Rules,
                   method: Method | None = None, fixes: dict[str, str] | None = None,
                   anys: tuple[str, ...] | set[str] = (),
                   run_order: dict[str, int] | None = None, delta: float = 10.0) -> dict:
    """分数对比（doc/10 §5.5）：放行规则沿用 compare_axis（_axis_selection）；
    轴不是 mode 且 mode 未固定时按 §6.3 主口径逐题取数（_select_mode_per_qkey），
    与用例页综合分同口径，且同一 qkey 的 natural 与 forced 不混算。两侧在共有题目上
    分别算题目/维度/综合分，输出单组件贡献（配对自助法区间）、逐题差异（只列超过
    compare_delta 的）与锚点绊线。"""
    fixes = dict(fixes or {})
    anys = set(anys)
    mode_select = axis != "mode" and "mode" not in fixes
    if mode_select:
        anys.add("mode")     # mode 的不一致由逐题主口径选择处理（_select_mode_per_qkey），不再拦截
    info = _axis_selection(rows, axis, fixes, anys)
    sel_all, axis_values = info["sel"], info["axis_values"]
    if info["blocked"]:
        return {**info, "base": base, "target": target, "matrix": {}}
    if base not in axis_values or target not in axis_values:
        raise ValueError(f"基线/对照必须是轴上的取值：{axis_values}")
    sel, mode_excluded = (_select_mode_per_qkey(sel_all, axis, base, target, method)
                          if mode_select else (sel_all, []))
    sides = {av: [r for r in sel if attr_value(r, axis) == av] for av in (base, target)}
    qs = {av: question_scores(sides[av], rules, headline=False) for av in sides}
    shared = {q for q in set(qs[base]) & set(qs[target])
              if qs[base][q]["score"] is not None and qs[target][q]["score"] is not None}
    excluded = list(mode_excluded)
    excluded += [{"qkey": q, "reason": "仅单侧有题目（答案）"} for q in sorted(set(qs[base]) ^ set(qs[target]))]
    for q in sorted(set(qs[base]) & set(qs[target])):
        if q not in shared and not any(e["qkey"] == q for e in excluded):
            excluded.append({"qkey": q, "reason": "某侧没有确定会话分"})
    case_of = {q: qs[base][q]["case_id"] for q in shared}
    dim_of = {q: qs[base][q]["dim"] for q in shared}
    wires = anchor_tripwires(sel_all, run_order)
    tripwires = {t["qkey"] for t in wires}

    metrics = {av: _side_metrics(sides[av], shared, rules) for av in sides}
    cases = sorted({case_of[q] for q in shared})
    # 每侧每用例各维度预聚合成 (Σ分, 条数)：重采样按重数重复计入（P1：重复抽中的
    # 用例要重复计），自助法每步只做查表累加
    per_case = {av: defaultdict(lambda: defaultdict(lambda: [0.0, 0])) for av in sides}
    for q in shared:
        for av in sides:
            meta = qs[av][q]
            if meta["score"] is not None:
                cell = per_case[av][case_of[q]][meta["dim"]]
                cell[0] += meta["score"]
                cell[1] += 1

    def paired(fn) -> tuple[float, float]:
        return _bootstrap(cases, fn)

    base_c = metrics[base]["composite"]["score"]
    target_c = metrics[target]["composite"]["score"]
    # 综合分差按两侧维度差加权平均（§6.4 公式），不用已舍入综合分相减（避免 76.8−73.9=2.9 的舍入差）
    weights = rules.aggregate.dimensions
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

    def comp_delta(cs: list[str]) -> float:
        """重采样上的综合分差：各侧先按维度聚合、再按维度权重综合——与点估计同一统计量。"""
        def comp(av):
            dsum: dict[str, float] = defaultdict(float)
            dcnt: dict[str, float] = defaultdict(float)
            for c in cs:                  # cs 含重复 → 同一用例的聚合值重复计入
                for d, (s, n) in per_case[av].get(c, {}).items():
                    dsum[d] += s
                    dcnt[d] += n
            if not dcnt:
                return 0.0
            w = sum(float(weights.get(d, 1)) for d in dcnt)
            return sum(float(weights.get(d, 1)) * (dsum[d] / dcnt[d]) for d in dcnt) / w
        return comp(target) - comp(base)

    lo, hi = paired(comp_delta)
    contributions = [{"name": "综合分", "delta": delta, "lo": lo, "hi": hi,
                      "base": base_c, "target": target_c}]
    all_dims = sorted(set(metrics[base]["dims"]) | set(metrics[target]["dims"]))
    for dim in all_dims:
        b = metrics[base]["dims"].get(dim, {}).get("score")
        t = metrics[target]["dims"].get(dim, {}).get("score")
        d = round(t - b, 1) if b is not None and t is not None else None
        lo_d, hi_d = paired(lambda cs, dim=dim: _dim_delta(cs, dim, per_case, base, target))
        contributions.append({"name": dim, "delta": d, "lo": lo_d, "hi": hi_d, "base": b, "target": t})
    questions = []
    for q in sorted(shared):
        b, t = qs[base][q]["score"], qs[target][q]["score"]
        if b is None or t is None:
            continue
        d = round(t - b, 1)
        if abs(d) > delta or q in tripwires:
            questions.append({"qkey": q, "uid": qs[base][q]["uid"], "case_id": case_of[q], "dim": dim_of[q],
                              "base": b, "target": t, "delta": d, "anchor": q in tripwires})
    questions.sort(key=lambda x: -abs(x["delta"]))

    # 成本差（doc/18 §5）：同题配对差的中位数作点估计，区间沿用同一批重采样
    # （_bootstrap 固定种子按位置抽样，同一 cases 序列 → 与分数差抽到的样本一致）
    def _side_cost(side_rows: list[dict]) -> dict[str, tuple[float, float]]:
        acc: dict[str, list[tuple[float, float]]] = defaultdict(list)
        for r in side_rows:
            if r.get("tokens_total") is not None:
                acc[r["qkey"]].append((r["tokens_total"], r.get("duration_ms") or 0))
        return {q: (sum(t for t, _ in v) / len(v), sum(d for _, d in v) / len(v))
                for q, v in acc.items()}

    def _median(xs: list[float]) -> float | None:
        if not xs:
            return None
        ys = sorted(xs)
        n = len(ys)
        return ys[n // 2] if n % 2 else (ys[n // 2 - 1] + ys[n // 2]) / 2

    cost: dict[str, dict] = {}
    cb, ct = _side_cost(sides[base]), _side_cost(sides[target])
    shared_cost = [q for q in shared if q in cb and q in ct]
    if shared_cost:
        q_by_case_cost: dict[str, list[str]] = defaultdict(list)
        for q in shared_cost:
            q_by_case_cost[case_of[q]].append(q)
        for label, idx in (("tokens", 0), ("duration", 1)):
            rel = {q: (ct[q][idx] - cb[q][idx]) / cb[q][idx]
                   for q in shared_cost if cb[q][idx] and cb[q][idx] > 0}
            cell = {"base": _median([cb[q][idx] for q in shared_cost]),
                    "target": _median([ct[q][idx] for q in shared_cost]),
                    "delta_pct": None, "lo": None, "hi": None}
            if rel:
                def stat(cs: list[str]) -> float:
                    return _median([rel[q] for c in cs for q in q_by_case_cost.get(c, ())]) or 0.0
                lo, hi = paired(stat)
                point = _median([rel[q] for q in shared_cost])
                cell.update(delta_pct=round(point * 100, 1),
                            lo=round(lo * 100, 1), hi=round(hi * 100, 1))
            cost[label] = cell
    return {**info, "base": base, "target": target,
            "n_shared": len(shared), "n_shared_cases": len(cases),
            "small_sample": len(shared) < SMALL_SAMPLE_N,       # doc/18 §8：只影响展示
            "cost": cost or None,                                # 无成本数据的旧 run 为 None
            "metrics": metrics, "contributions": contributions,
            "contrib_scale": contrib_scale(contributions),
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


def shapley_cells(rows: list[dict], factors: list[str], rules: Rules,
                 method: Method | None = None) -> tuple[dict, set[str]]:
    """全因子设计各格的综合分（§6.4）：组件外的处理条件须一致（复用比较轴的一致性
    规则，profile/model 互为派生时另一个放行）；mode 不作组件时逐题按 §6.3 主口径
    选 mode（各格都有 headline_mode 会话取主口径，否则各格同一唯一 mode，再否则
    该题不进分摊）；各格只在全部格共有的题目上算分——各格出自同一题集，Shapley
    分摊才可比。返回 (组件组合 → 综合分, 共有 qkeys)。"""
    if not factors or len(factors) > 3:
        raise ValueError("组件个数须在 1–3 之间")
    unknown = [f for f in factors if f not in ATTRS]
    if unknown:
        raise ValueError(f"未知组件：{unknown}（只能是 {'/'.join(ATTRS)}）")
    if "skills_mode" in factors:
        raise ValueError("skills_mode 不能作组件（跨 mode 对比无效）")
    for f in ("persona", "tools"):
        if f in factors and "?" in {attr_value(r, f) for r in rows}:
            raise ValueError(f"{f} 因子遇到无取值的答案（旧 run 无观测或配置缺失），不能分摊")
    pairs = {}
    for f in factors:
        vals = sorted({attr_value(r, f) for r in rows})
        if len(vals) != 2:
            raise ValueError(f"组件 {f} 需要恰好两个取值（现有 {len(vals)} 个：{vals[:4]}）")
        pairs[f] = vals
    # 放行集合 = ∪ follows(f)（doc/18 §3）：persona/tools 无 DERIVED_ATTR 键，作因子时
    # subject 靠这里放行（原实现只查 DERIVED_ATTR.get(f)，persona 作因子会直接报
    # 「subject 取值不一致」）；mode 不按不一致拦截（逐题主口径选择处理）
    follow_all: set[str] = set()
    for f in factors:
        follow_all |= follows(f)
    others = [a for a in ATTRS if a not in follow_all and a not in SAMPLING_ATTRS
              and a != "tools" and a != "mode"]
    inconsistent = {a: sorted({attr_value(r, a) for r in rows}) for a in others
                    if len({attr_value(r, a) for r in rows}) > 1}
    if inconsistent:
        raise ValueError("组件之外未固定的属性取值不一致："
                         + "；".join(f"{k}={v}" for k, v in inconsistent.items()))
    extra_blocked, _ = subject_extra_check(rows, set(factors))
    if extra_blocked:
        raise ValueError("组件之外的被测配置字段不一致（doc/18 §3）："
                         + "；".join(f"{k}={v}" for k, v in extra_blocked.items()))
    sel_by_cell: dict[frozenset, list[dict]] = {}
    for i in range(2 ** len(factors)):
        on = frozenset(factors[j] for j in range(len(factors)) if i >> j & 1)
        sel_by_cell[on] = [r for r in rows
                           if all(attr_value(r, f) == (pairs[f][1] if f in on else pairs[f][0])
                                  for f in factors)]
    # 逐题选 mode 与共有题目（mode 作组件时各格本就按 mode 分开，不做选择）
    keep: dict[str, str] = {}
    for q in {r["qkey"] for r in rows}:
        per_cell_modes = [{r["mode"] for r in sel if r["qkey"] == q} for sel in sel_by_cell.values()]
        if not per_cell_modes or not all(per_cell_modes):
            continue                                    # 某格没有该题 → 不在共有题目集
        dim = next((r["dim"] for sel in sel_by_cell.values() for r in sel if r["qkey"] == q), "?") or "?"
        h = (method or DEFAULT_METHOD).headline(dim)
        if all(h in ms for ms in per_cell_modes):
            keep[q] = h
        elif len(per_cell_modes[0]) == 1 and all(ms == per_cell_modes[0] for ms in per_cell_modes):
            keep[q] = next(iter(per_cell_modes[0]))
    shared = set(keep)
    cells = {}
    for on, sel in sel_by_cell.items():
        sub = [r for r in sel if r["qkey"] in shared
               and ("mode" in factors or r["mode"] == keep[r["qkey"]])]
        dims = dimension_scores(sub, rules, headline=False)
        cells[on] = composite(dims, rules, known_dims=set(dims)).get("score")
    return cells, shared


def judge_agreement(store, chain_sha: str, rules: Rules) -> list[dict]:
    """判定质量主表（§5.6）：算子 × 条目类型的人工复核数、平均分差、分差 ≤ 10 占比、
    分歧数。机器分 = 显式分 ?? map[结论词]，人工分同；只看有效改判（sha 匹配）。"""
    m = rules.score.map
    from .items import SCALES
    sql = """SELECT v.answer_id, v.item_id, v.kind, v.value AS machine_value, v.score AS machine_score,
             v.source, v.operator, r.value AS review_value, r.score AS review_score, r.review_id
             FROM verdicts v
             JOIN answers a ON a.answer_id = v.answer_id
             JOIN questions q ON q.qkey = a.qkey AND q.current_standard_fp = v.standard_fp
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
