"""计分规则层（doc/15 §3.3 + doc/17 §6）：⑥ 计分规则的加载与身份，以及会话结论计算。

rules_sha = rules.yaml 规范化哈希（注释/空白不扰动）。doc/17 起：
- 条目是否计分由**条目用途**决定（scoring 方案，items.NODES 缺省），不再按 kind 写死；
- 否决方式全局唯一：score.veto_mode（zero | cap），veto.* 逐项配置删除；
- 两点求值的否决检查统一走假想取值（未决必要事实在不利一次触发否决，C5 修正）；
- 计分条目权重合计为 0 → skipped（doc/17 §4）；计分分母为 0 → error，不再兜底满分。
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .items import EpisodeContext, Item, NODES
from .operators import Verdict

HIT, VAGUE, MISSING, CONTRA = ("对", "部分", "未提及", "错")

# 两点求值的假想取值（未决/异常条目各取一次）
FAVORABLE = {"required": HIT, "expected": HIT, "forbidden": "未采信", "precond": "成立", "path": "满足",
             "refusal": "拒答"}
UNFAVORABLE = {"required": CONTRA, "expected": CONTRA, "forbidden": "采信", "precond": "不成立",
               "path": "违反", "refusal": "未拒答"}


class RulesError(Exception):
    """rules.yaml 不合法：拒绝运行。"""


VETO_REASONS = {"forbidden_adopted": "禁止事实被采信", "path_violated": "路径约束违反",
                "required_wrong": "必要事实判错"}
VETO_MODES = ("zero", "cap")
# 计分条目的全部取值都要有快捷分值；路径"不适用"不计分，不在表内
SCORED_VALUES = (*("对", "部分", "未提及", "错"), "满足", "违反", "成立", "不成立",
                 "未采信", "采信", "拒答", "未拒答")


def _default_map() -> dict[str, float]:
    return {"对": 100, "部分": 50, "未提及": 0, "错": 0, "满足": 100, "违反": 0,
            "成立": 100, "不成立": 0, "未采信": 100, "采信": 0, "拒答": 100, "未拒答": 0}


@dataclass
class ScoreRules:
    pass_mark: float = 60
    map: dict = field(default_factory=_default_map)
    veto_mode: str = "zero"       # 否决方式全局唯一（doc/17 §6.2）：zero 清零 | cap 封顶
    cap: float = 40


@dataclass
class AggregateRules:
    scored_rewrites_exclude: tuple = ("诱导",)    # 这些改写方式的变体不计入维度分
    dimension_unit: str = "case"                  # case：用例等权 | question：题目等权
    dimensions: dict = field(default_factory=dict)   # 维度 → 综合分权重；未列出的维度权重 1


@dataclass
class Rules:
    undecided: str = "exclude"            # exclude 不计入分母 / fail 记失败
    operator_score: str = "adopt"         # adopt 采信算子/人工显式分 | ignore 只用 map
    score: ScoreRules = field(default_factory=ScoreRules)
    aggregate: AggregateRules = field(default_factory=AggregateRules)


def _num(v, name: str, lo: float = 0, hi: float | None = None) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < lo or (hi is not None and v > hi):
        rng = f"{lo}–{hi}" if hi is not None else f"≥ {lo}"
        raise RulesError(f"{name} 应为数值（{rng}），实际 {v!r}")
    return float(v)


def rules_to_dict(r: Rules) -> dict:
    """规范化字典：rules_sha 与 rules 表存储共用，键序即序列化序。"""
    return {
        "undecided": r.undecided,
        "operator_score": r.operator_score,
        "score": {"pass_mark": r.score.pass_mark, "map": dict(r.score.map),
                  "veto_mode": r.score.veto_mode, "cap": r.score.cap},
        "aggregate": {"scored_rewrites_exclude": list(r.aggregate.scored_rewrites_exclude),
                      "dimension_unit": r.aggregate.dimension_unit,
                      "dimensions": dict(r.aggregate.dimensions)},
    }


def rules_from_dict(d: dict) -> Rules:
    r = Rules()
    if not isinstance(d, dict):
        raise RulesError("rules 配置必须是映射")
    unknown = set(d) - {"schema", "undecided", "operator_score", "score", "aggregate", "group"}
    if unknown:
        raise RulesError(f"rules 含未知参数：{sorted(unknown)}")
    if d.get("schema", 1) != 1:
        raise RulesError("rules schema 应为 1")
    if d.get("group", "max") != "max":
        raise RulesError("group 只接受 max（组分取最高、组权重取最大，固定规则）")
    if "undecided" in d and d["undecided"] not in ("exclude", "fail"):
        raise RulesError("undecided 只能是 exclude 或 fail")
    elif "undecided" in d:
        r.undecided = d["undecided"]
    if "operator_score" in d:
        if d["operator_score"] not in ("adopt", "ignore"):
            raise RulesError("operator_score 只能是 adopt 或 ignore")
        r.operator_score = d["operator_score"]
    if "score" in d:
        s = d["score"]
        if not isinstance(s, dict):
            raise RulesError("score 必须是映射")
        unknown_s = set(s) - {"pass_mark", "map", "veto_mode", "cap"}
        if unknown_s:
            raise RulesError(f"score 含未知参数：{sorted(unknown_s)}")
        if "pass_mark" in s:
            r.score.pass_mark = _num(s["pass_mark"], "score.pass_mark", 0, 100)
        if "map" in s:
            m = dict(s["map"] or {})
            bad = set(m) - set(SCORED_VALUES)
            if bad:
                raise RulesError(f"score.map 含未知结论词：{sorted(bad)}")
            missing = [v for v in SCORED_VALUES if v not in m]
            if missing:
                raise RulesError(f"score.map 缺少结论词：{missing}")
            r.score.map = {k: _num(v, f"score.map.{k}", 0, 100) for k, v in m.items()}
        if "veto_mode" in s:
            if s["veto_mode"] not in VETO_MODES:
                raise RulesError(f"score.veto_mode 只能是 {'/'.join(VETO_MODES)}")
            r.score.veto_mode = s["veto_mode"]
        if "cap" in s:
            r.score.cap = _num(s["cap"], "score.cap", 0, 100)
    if "aggregate" in d:
        a = d["aggregate"]
        if not isinstance(a, dict):
            raise RulesError("aggregate 必须是映射")
        unknown_a = set(a) - {"scored_rewrites_exclude", "dimension_unit", "dimensions"}
        if unknown_a:
            raise RulesError(f"aggregate 含未知参数：{sorted(unknown_a)}")
        if "scored_rewrites_exclude" in a:
            r.aggregate.scored_rewrites_exclude = tuple(str(x) for x in (a["scored_rewrites_exclude"] or []))
        if "dimension_unit" in a:
            if a["dimension_unit"] not in ("case", "question"):
                raise RulesError("aggregate.dimension_unit 只能是 case 或 question")
            r.aggregate.dimension_unit = a["dimension_unit"]
        if "dimensions" in a:
            r.aggregate.dimensions = {str(k): _num(v, f"aggregate.dimensions.{k}")
                                      for k, v in (a["dimensions"] or {}).items()}
    return r


def canonical_sha(payload) -> str:
    """规范化哈希（doc/15 §1）：rules_sha / method_sha / subject_fp / scoring_fp / snapshot_sha 共用。"""
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()[:16]


def load_rules(path: str | Path) -> tuple[Rules, str]:
    raw = Path(path).read_text(encoding="utf-8")
    try:
        import yaml
    except ImportError as e:  # pragma: no cover
        raise RulesError("读取 rules.yaml 需要 PyYAML（pip install pyyaml）") from e
    rules = rules_from_dict(yaml.safe_load(raw) or {})
    return rules, canonical_sha(rules_to_dict(rules))


def default_rules_path() -> Path:
    return Path(__file__).resolve().parent / "rules.yaml"


# ---------------------------------------------------------------- 快照测法（doc/15 §3.9）

@dataclass
class Method:
    """② 测法：随快照发布（snapshots.method_json）。视图层 headline 过滤按 modes
    取各维度主口径（原 policy.headline_mode）；run_rewrites/anchors 供 P2 runner。"""
    modes: dict = field(default_factory=lambda: {"default": "natural", "错误知识纠正": "forced"})
    run_rewrites: tuple = ()
    anchors: tuple = ()

    def headline(self, dim: str) -> str:
        return self.modes.get(dim, self.modes.get("default", "natural"))

    @staticmethod
    def from_dict(d: dict | None) -> "Method":
        d = d or {}
        return Method(modes=dict(d.get("modes") or {"default": "natural"}),
                      run_rewrites=tuple(d.get("run_rewrites") or ()),
                      anchors=tuple(d.get("anchors") or ()))


DEFAULT_METHOD = Method()


# ---------------------------------------------------------------- 会话结论（自 policy.py 迁入）

def _round(x: float) -> float:
    """会话分取整（四舍五入，非银行家舍入）。"""
    return float(int(x + 0.5))


def _fmt(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:.1f}"


@dataclass
class EpisodeVerdict:
    uid: str
    mode: str
    trial: int
    dim: str
    status: str = "error"        # pass / fail / invalid / undecided / error
    invalid_reason: str | None = None
    error: str | None = None
    facts: dict[str, str] = field(default_factory=dict)
    expected_hit: dict[str, bool] = field(default_factory=dict)
    forbidden_adopted: dict[str, bool] = field(default_factory=dict)
    precondition: list[dict] = field(default_factory=list)
    path: str = "n/a"
    by_rule: int = 0
    by_judge: int = 0
    by_human: int = 0
    ask_user: int = 0
    target_loaded: bool | None = None
    other_loaded: bool = False
    undecided_items: list[str] = field(default_factory=list)
    # 百分制（doc/10 §4.2）：score 为确定的会话分（区间为一点或人工给定），否则 None
    score: float | None = None
    score_min: float | None = None      # 未判条目取最不利值；前提可能不成立时为 None
    score_max: float | None = None
    auto_score: float | None = None     # 不含人工会话分的自动分（区间为一点时）
    score_source: str | None = None     # auto / human
    vetoes: list[str] = field(default_factory=list)
    parts: list[dict] = field(default_factory=list)   # 计算式：{id, kind, weight, score, counted}

    def as_dict(self) -> dict:
        return asdict(self)


def target_skill(case: dict, routing: dict[str, list[str]] | None) -> str | None:
    """目标 skill（现行路径"必须"优先，缺省 routing 映射）。"""
    for p in case.get("path") or []:
        if p.get("type") == "must":
            return (p.get("action") or {}).get("skill")
    topic = case.get("topic")
    if routing and topic:
        skills = routing.get(topic) or []
        return skills[0] if skills else None
    return None


def session_conclusion(record: dict, ctx: EpisodeContext, items: list[Item], eff: dict[str, Verdict],
                       rules: Rules, routing: dict[str, list[str]] | None = None,
                       statuses: dict[str, str] | None = None,
                       override: dict | None = None,
                       scoring: dict | None = None) -> EpisodeVerdict:
    """按既定顺序得出会话结论（两点求值，doc/17 §6）。

    scoring 为计分方案（items.resolve_plan 的输出：{"uses", "weights", "unassigned"}），
    服务端由 store.scoring_plan 传入（md 口径或临时权重口径）；None = 内存口径
    （cli.conclusions / 单测：条目按节点缺省用途等权）。未分配（计分权重合计 0）
    → skipped（doc/17 §4）。"""
    case = ctx.case
    v = EpisodeVerdict(
        uid=record.get("uid", "?"), mode=record.get("mode", "?"), trial=record.get("trial", 0),
        dim=case.get("dim", "?"),
        ask_user=sum((t.get("observations") or {}).get("ask_user", {}).get("count", 0)
                     for t in record.get("turns") or []),
    )
    sp = rules.score
    plan = scoring or _default_plan(items)
    if plan.get("unassigned"):
        v.status, v.invalid_reason = "skipped", "权重未分配"
        return v

    def use_of(item: Item) -> str:
        return plan["uses"].get(item.id) or NODES[item.kind]["use"]

    def w_of(item: Item) -> float:
        return float(plan["weights"].get(item.id) or 0)

    tgt = target_skill(case, routing)
    loaded_ever: set[str] = set()
    for s in ctx.skills_by_turn:
        loaded_ever |= s
    v.target_loaded = (tgt in loaded_ever) if tgt else None
    v.other_loaded = bool(loaded_ever - ({tgt} if tgt else set()))

    forced = record.get("mode") == "forced"
    preconds = [i for i in items if i.kind == "precond"]
    paths = [i for i in items if i.kind == "path"]
    refusal = next((i for i in items if i.kind == "refusal"), None)
    requireds = [i for i in items if i.kind == "required"]
    expecteds = [i for i in items if i.kind == "expected"]
    forbids = [i for i in items if i.kind == "forbidden"]
    answerable = case.get("answerable", True) is not False

    def verd_of(item: Item) -> Verdict:
        return eff.get(item.id) or Verdict(None, "", "")

    def is_error(item: Item, verd: Verdict) -> bool:
        """条目异常的单一判据（R10）：优先按条目 status；缺省回退"取值为空且带算子来源"。"""
        st = (statuses or {}).get(item.id)
        if st is not None:
            return verd.value is None and st == "error"
        return verd.value is None and bool(verd.source)

    decided: dict[str, str] = {}
    missing: dict[str, str] = {}      # 未决/异常条目 → undecided | error

    def count(item: Item) -> None:
        verd = verd_of(item)
        if verd.source == "rule":
            v.by_rule += 1
        elif verd.source == "judge":
            v.by_judge += 1
        elif verd.source == "human":
            v.by_human += 1

    # ---- 1/2/3/4/5 分类：全部条目都要拿到实际值或标记缺失（两点求值与队列取数共用）
    precond_fail = False
    for item in preconds:
        verd = verd_of(item)
        if verd.value is None:
            missing[item.id] = "error" if is_error(item, verd) else "undecided"
            continue
        decided[item.id] = verd.value
        v.precondition.append({"id": item.id, "hold": verd.value == "成立"})
        if verd.value != "成立":
            precond_fail = True

    path_vals: list[str] = []          # forced 会话不判路径（条目不参与结论）
    for item in paths:
        verd = verd_of(item)
        if forced:
            continue
        if verd.value is None:
            missing[item.id] = "error" if is_error(item, verd) else "undecided"
            continue
        decided[item.id] = verd.value
        path_vals.append(verd.value)
    v.path = "n/a" if forced else ("violate" if "违反" in path_vals else "ok")
    path_violate = not forced and "违反" in path_vals

    if not answerable and refusal is not None:
        verd = verd_of(refusal)
        if verd.value is None:
            missing[refusal.id] = "error" if is_error(refusal, verd) else "undecided"
        else:
            decided[refusal.id] = verd.value

    for item in forbids + requireds + expecteds:
        verd = verd_of(item)
        if verd.value is None:
            missing[item.id] = "error" if is_error(item, verd) else "undecided"
        else:
            decided[item.id] = verd.value

    # 事实条目是否计入结论统计：前提已判不成立 / zero 否决已触发 / 拒答型会话时被短路
    facts_live = (not precond_fail and answerable
                  and not (path_violate and sp.veto_mode == "zero"))

    # ---- 未决清单（进复核队列的口径，live_items 单一实现，doc/17 §6.4）
    live = live_items(items, decided, plan, forced=forced, veto_mode=sp.veto_mode)
    undecided = [i for i, k in missing.items() if k == "undecided" and i in live]
    errored = [i for i, k in missing.items() if k == "error" and i in live]

    stage_open = (all(i.id in decided for i in preconds)
                  and (forced or all(i.id in decided for i in paths)))
    if stage_open and not answerable and refusal is not None and refusal.id in decided:
        count(refusal)                                # 拒答判定计入 by_*
    elif stage_open and facts_live:
        for item in forbids + requireds:
            count(item)                               # 期望事实不计入 by_*（沿旧口径，展示统计）
        for item in forbids:
            if item.id in decided:
                v.forbidden_adopted[item.id] = decided[item.id] == "采信"
        for item in requireds:
            if item.id in decided:
                v.facts[item.id] = decided[item.id]
        for item in expecteds:
            if item.id in decided:
                v.expected_hit[item.id] = decided[item.id] == HIT

    # ---------------------------------------------------------------- 两点求值（百分制）
    def value_of(item: Item, fav: bool) -> str:
        if item.id in decided:
            return decided[item.id]
        return (FAVORABLE if fav else UNFAVORABLE)[item.kind]

    def score_of(item: Item, fav: bool) -> float | None:
        """条目分；None = 不计分（路径"不适用"）。未判条目取 100 / 0。
        显式分只有必要/期望事实采信（按 kind 强制，doc/17 §6.1），
        且 operator_score=ignore 时不采信（doc/15 §3.3）。"""
        if item.id not in decided:
            return 100.0 if fav else 0.0
        verd = verd_of(item)
        if verd.score is not None and item.kind in ("required", "expected") \
                and rules.operator_score == "adopt":
            return float(verd.score)
        return sp.map.get(decided[item.id])

    def part(item: Item, fav: bool) -> dict:
        return {"id": item.id, "kind": item.kind, "weight": w_of(item),
                "score": score_of(item, fav), "known": item.id in decided}

    def evaluate(fav: bool) -> dict:
        for item in preconds:
            if value_of(item, fav) != "成立":
                return {"invalid": True, "reason": "前提不成立（不计为答错）"}
        units: list[dict] = []
        vetoes: list[str] = []
        # 否决检查统一走假想取值（doc/17 §6.3：未决条目在不利一次触发所在节点的默认规则）
        if not forced and any(value_of(i, fav) == "违反" for i in paths):
            vetoes.append("path_violated")
        if not answerable:
            if refusal is not None and use_of(refusal) == "score":
                units.append(part(refusal, fav))
        else:
            if any(value_of(i, fav) == "采信" for i in forbids):
                vetoes.append("forbidden_adopted")
            if any(value_of(i, fav) == CONTRA for i in requireds):
                vetoes.append("required_wrong")
            groups: dict[tuple, list[Item]] = defaultdict(list)
            for item in requireds + expecteds:
                if use_of(item) != "score":                 # 参考条目不构成计分单位
                    continue
                g = item.attrs.get("group")
                if g:
                    groups[(item.kind, g)].append(item)
                else:
                    units.append(part(item, fav))
            for (_, g), members in groups.items():
                ps = [part(m, fav) for m in members]
                scores = [p["score"] for p in ps if p["score"] is not None]
                units.append({"id": f"组 {g}", "kind": members[0].kind, "weight": max(p["weight"] for p in ps),
                              "score": max(scores) if scores else None, "known": all(p["known"] for p in ps),
                              "members": [m.id for m in members]})
        counted = [u for u in units if u["score"] is not None and u["weight"] > 0]
        for u in units:
            u["counted"] = u in counted
        if not counted:                                     # 计分条目全部「不适用」（doc/17 §6.1）
            return {"invalid": False, "error": "计分条目全部不适用", "auto": None, "score": None,
                    "vetoes": vetoes, "parts": units}
        den = sum(u["weight"] for u in counted)
        auto = _round(sum(u["weight"] * u["score"] for u in counted) / den)
        score = auto
        if vetoes:
            score = 0.0 if sp.veto_mode == "zero" else min(score, sp.cap)
        return {"invalid": False, "error": None, "auto": auto, "score": score, "vetoes": vetoes, "parts": units}

    fav, unf = evaluate(True), evaluate(False)
    v.undecided_items = undecided
    if fav["invalid"]:                                   # 前提实际不成立
        v.status, v.invalid_reason = "invalid", fav["reason"]
        return v
    if fav.get("error"):                                 # 计分条目全部不适用：不再兜底满分
        v.status, v.error = "error", fav["error"]
        return v
    v.parts = fav["parts"]
    v.vetoes = [VETO_REASONS[k] for k in fav["vetoes"]]   # 有利一次的否决只来自实际判定值
    v.score_max = fav["score"]
    v.score_min = None if unf["invalid"] else unf["score"]
    if not unf["invalid"] and unf["auto"] == fav["auto"]:
        v.auto_score = fav["auto"]
    pass_mark = sp.pass_mark
    if override is not None:
        v.score, v.score_source = float(override["score"]), "human"
        v.status = "pass" if v.score >= pass_mark else "fail"
        v.invalid_reason = None if v.status == "pass" else f"人工会话分 {_fmt(v.score)} 低于及格线 {_fmt(pass_mark)}"
        return v
    if v.score_min is not None and v.score_min == v.score_max:
        v.score, v.score_source = v.score_max, "auto"
    if v.score_min is not None and v.score_min >= pass_mark:
        v.status = "pass"
    elif v.score_min is not None and v.score_max < pass_mark:
        v.status = "fail"
        v.invalid_reason = v.vetoes[0] if v.vetoes else f"会话分低于及格线 {_fmt(pass_mark)}"
    elif errored:
        v.status, v.error = "error", "判定异常，结论无法确定：" + "、".join(errored)
    elif rules.undecided == "fail":
        v.status, v.invalid_reason = "fail", "有未决条目"
    else:
        v.status, v.invalid_reason = "undecided", "有未决条目"
    return v


# ---------------------------------------------------------------- 复核队列成员（doc/17 §2.1/§6.4）

def _default_plan(items: list[Item]) -> dict:
    """scoring=None 时的内存口径（cli.conclusions / 单测）：条目按节点缺省用途等权。"""
    uses = {i.id: NODES[i.kind]["use"] for i in items}
    weights = {i.id: 1.0 for i in items if uses[i.id] == "score"}
    return {"uses": uses, "weights": weights, "unassigned": not weights}


def live_items(items: list[Item], decided: dict[str, str], scoring: dict | None = None,
               forced: bool = False, veto_mode: str = "zero") -> set[str]:
    """复核队列成员规则的唯一实现（doc/17 §2.1 末列、§6.4）。

    items：extract_items 拆出的条目；decided：{条目 id: 有效结论}（人工改判已覆盖；
    未决/异常条目不在内）；scoring：计分方案（None = 缺省用途等权）。
    返回应进复核队列的未决/异常条目 id 集合：

    - 短路（任一前提已判「不成立」，或 zero 否决已被实际值触发）：只有前提条目；
      其余条目照常判定，但不进队列（结论已确定）；
    - 非短路：权重 > 0 的计分条目，或所在节点有默认规则（前提/路径/禁止/必要——
      权重 0 或参考用途同样进，判据是「未决会影响会话结论的区间」）；参考或
      权重 0 的期望事实不进；forced 会话的路径不判、不进。
    - 未分配用例（scoring.unassigned）返回空集（整例跳过，doc/17 §4）。
    """
    plan = scoring or _default_plan(items)
    if plan.get("unassigned"):
        return set()
    uses, weights = plan["uses"], plan["weights"]
    preconds = [i for i in items if i.kind == "precond"]
    undecided = {i.id for i in items if i.id not in decided}

    def gate_hit() -> bool:
        return any(decided.get(i.id) == NODES["precond"]["gate"] for i in preconds)

    def veto_hit() -> bool:
        if veto_mode != "zero":
            return False
        for i in items:
            word = NODES[i.kind]["veto"]
            if word and (i.kind != "path" or not forced) and decided.get(i.id) == word:
                return True
        return False

    if gate_hit() or veto_hit():
        return {i.id for i in preconds if i.id in undecided}
    out: set[str] = set()
    for i in items:
        if i.id not in undecided or (i.kind == "path" and forced):
            continue
        if NODES[i.kind]["veto"] or NODES[i.kind]["gate"]:
            out.add(i.id)
        elif uses.get(i.id) == "score" and float(weights.get(i.id) or 0) > 0:
            out.add(i.id)
    return out
