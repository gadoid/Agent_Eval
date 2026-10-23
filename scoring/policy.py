"""口径层（复核服务改造方案 §4）：百分制会话分 + 会话结论判定。

条目分 = 显式分（模型算子或人工自定）?? score.map[结论词]；会话分 = Σ 权重 × 条目分 / Σ 权重。
依次：前提（实际）不成立 → invalid、不计分 → 否决项（禁止事实被采信、路径违反、必要事实
实际判"错"，各按 score.veto 记 0 / 封顶 / 按权重计）→ 有效人工会话分取代自动分。
通过 = 会话分 ≥ score.pass_mark。组（满足其一）为一个计分单位：组分取组内最高分、组权重取最大。

未决与异常按"能否改变结论"处理（两点求值，评测结果管理改造方案 §3.2/§3.3）：
未决/异常条目分别取最有利（100、成立、未采信、满足）与最不利值各算一次，得到会话分区间；
区间整体在及格线一侧即确定；否则有异常条目记 error（不进分母，不受 policy.undecided 影响），
没有则按 policy.undecided。拆条与短路记录（facts / forbidden_adopted / 计数）沿现行顺序。
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .items import FACT_SCALE, EpisodeContext, Item, weight_of
from .operators import Verdict

HIT, VAGUE, MISSING, CONTRA = FACT_SCALE

# 两点求值的假想取值（改造方案 §3.2）：未决/异常条目各取一次
FAVORABLE = {"required": HIT, "expected": HIT, "forbidden": "未采信", "precond": "成立", "path": "满足",
             "refusal": "拒答"}
UNFAVORABLE = {"required": CONTRA, "expected": CONTRA, "forbidden": "采信", "precond": "不成立", "path": "违反",
               "refusal": "未拒答"}


def _round(x: float) -> float:
    """会话分取整（四舍五入，非银行家舍入）。"""
    return float(int(x + 0.5))


def _fmt(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:.1f}"


class PolicyError(Exception):
    """policy.yaml 不合法：拒绝运行。"""


VETO_KEYS = ("forbidden_adopted", "path_violated", "required_wrong")
VETO_MODES = ("zero", "cap", "weight")
VETO_REASONS = {"forbidden_adopted": "禁止事实被采信", "path_violated": "路径约束违反",
                "required_wrong": "必要事实判错"}
# 计分条目的全部取值都要有快捷分值；路径"不适用"不计分，不在表内
SCORED_VALUES = (*FACT_SCALE, "满足", "违反", "未采信", "采信", "拒答", "未拒答")


def _default_map() -> dict[str, float]:
    return {"对": 100, "部分": 50, "未提及": 0, "错": 0, "满足": 100, "违反": 0,
            "未采信": 100, "采信": 0, "拒答": 100, "未拒答": 0}


@dataclass
class ScorePolicy:
    pass_mark: float = 60
    default_weight: float = 1
    map: dict = field(default_factory=_default_map)
    veto: dict = field(default_factory=lambda: {k: "zero" for k in VETO_KEYS})
    cap: float = 40
    dimension_unit: str = "case"           # case：用例等权 | question：题目等权
    dimensions: dict = field(default_factory=dict)   # 维度 → 综合分权重；未列出的维度权重 1
    exclude_rewrites: tuple = ("诱导",)    # 不计入维度分的变体改写方式
    compare_delta: float = 10


@dataclass
class Policy:
    undecided: str = "exclude"        # exclude 不计入分母 / fail 记失败
    headline_mode: dict = field(default_factory=lambda: {"default": "natural", "错误知识纠正": "forced"})
    score: ScorePolicy = field(default_factory=ScorePolicy)

    def headline(self, dim: str) -> str:
        return self.headline_mode.get(dim, self.headline_mode.get("default", "natural"))


def _num(v, name: str, lo: float = 0, hi: float | None = None) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < lo or (hi is not None and v > hi):
        rng = f"{lo}–{hi}" if hi is not None else f"≥ {lo}"
        raise PolicyError(f"{name} 应为数值（{rng}），实际 {v!r}")
    return float(v)


def score_policy_from_dict(d: dict) -> ScorePolicy:
    sp = ScorePolicy()
    if not isinstance(d, dict):
        raise PolicyError("score 必须是映射")
    unknown = set(d) - {"pass_mark", "default_weight", "map", "veto", "dimension_unit", "dimensions",
                        "exclude_rewrites", "compare_delta"}
    if unknown:
        raise PolicyError(f"score 含未知参数：{sorted(unknown)}")
    if "pass_mark" in d:
        sp.pass_mark = _num(d["pass_mark"], "score.pass_mark", 0, 100)
    if "default_weight" in d:
        sp.default_weight = _num(d["default_weight"], "score.default_weight")
    if "map" in d:
        m = dict(d["map"] or {})
        bad = set(m) - set(SCORED_VALUES)
        if bad:
            raise PolicyError(f"score.map 含未知结论词：{sorted(bad)}")
        missing = [v for v in SCORED_VALUES if v not in m]
        if missing:
            raise PolicyError(f"score.map 缺少结论词：{missing}")
        sp.map = {k: _num(v, f"score.map.{k}", 0, 100) for k, v in m.items()}
    if "veto" in d:
        v = dict(d["veto"] or {})
        cap = v.pop("cap", None)
        bad = set(v) - set(VETO_KEYS)
        if bad:
            raise PolicyError(f"score.veto 含未知否决项：{sorted(bad)}")
        for k, mode in v.items():
            if mode not in VETO_MODES:
                raise PolicyError(f"score.veto.{k} 只能是 {'/'.join(VETO_MODES)}")
        sp.veto = {**sp.veto, **v}
        if cap is not None:
            sp.cap = _num(cap, "score.veto.cap", 0, 100)
    if "dimension_unit" in d:
        if d["dimension_unit"] not in ("case", "question"):
            raise PolicyError("score.dimension_unit 只能是 case 或 question")
        sp.dimension_unit = d["dimension_unit"]
    if "dimensions" in d:
        sp.dimensions = {str(k): _num(v, f"score.dimensions.{k}") for k, v in (d["dimensions"] or {}).items()}
    if "exclude_rewrites" in d:
        sp.exclude_rewrites = tuple(str(x) for x in (d["exclude_rewrites"] or []))
    if "compare_delta" in d:
        sp.compare_delta = _num(d["compare_delta"], "score.compare_delta", 0, 100)
    return sp


def policy_from_dict(d: dict) -> Policy:
    p = Policy()
    if not isinstance(d, dict):
        raise PolicyError("policy 配置必须是映射")
    retired = {"partial_as", "veto_on_forbidden"} & set(d)
    if retired:
        raise PolicyError(f"{sorted(retired)} 已由 score 节取代（百分制口径，见 doc/10 §4.5）")
    unknown = set(d) - {"undecided", "headline_mode", "score", "schema"}
    if unknown:
        raise PolicyError(f"policy 含未知参数：{sorted(unknown)}")
    if d.get("schema", 2) != 2:
        raise PolicyError("policy schema 应为 2（百分制口径）")
    if "score" in d:
        p.score = score_policy_from_dict(d["score"])
    if "undecided" in d:
        if d["undecided"] not in ("exclude", "fail"):
            raise PolicyError("undecided 只能是 exclude 或 fail")
        p.undecided = d["undecided"]
    if "headline_mode" in d:
        hm = dict(d["headline_mode"] or {})
        if "default" not in hm:
            raise PolicyError("headline_mode 必须含 default")
        p.headline_mode = hm
    return p


def load_policy(path: str | Path) -> tuple[Policy, str]:
    raw = Path(path).read_text(encoding="utf-8")
    try:
        import yaml
    except ImportError as e:  # pragma: no cover
        raise PolicyError("读取 policy.yaml 需要 PyYAML（pip install pyyaml）") from e
    return policy_from_dict(yaml.safe_load(raw) or {}), hashlib.sha256(raw.encode("utf-8")).hexdigest()


def default_policy_path() -> Path:
    return Path(__file__).resolve().parent / "policy.yaml"


# ---------------------------------------------------------------- 会话结论

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
    # 百分制（复核服务改造方案 §4.2）：score 为确定的会话分（区间为一点或人工给定），否则 None
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
    """目标 skill（现行 scoring.py:152-160）：路径"必须"优先，缺省 routing 映射。"""
    for p in case.get("path") or []:
        if p.get("type") == "must":
            return (p.get("action") or {}).get("skill")
    topic = case.get("topic")
    if routing and topic:
        skills = routing.get(topic) or []
        return skills[0] if skills else None
    return None


def session_conclusion(record: dict, ctx: EpisodeContext, items: list[Item], eff: dict[str, Verdict],
                       policy: Policy, routing: dict[str, list[str]] | None = None,
                       statuses: dict[str, str] | None = None,
                       override: dict | None = None) -> EpisodeVerdict:
    """按 §7 顺序得出会话结论（改造方案 §3 两点求值）。

    条目先按实际值分类（确定 / 未决 / 异常）；会话结论由两点求值决定：
    未决与异常条目分别取最有利、最不利值各算一次，两次相同即确定。
    记录字段（facts / forbidden_adopted / 计数）沿现行短路顺序——前提/路径/
    拒答短路后，事实不进结论。
    statuses（R10）：条目 id → 判定行 status（ok/undecided/error，人工覆盖后为 ok），
    是异常的单一判据；缺省回退"取值为空且带算子来源"。
    override：有效人工会话分 {"score", "reason"}（store.total_override），取代自动分。"""
    case = ctx.case
    v = EpisodeVerdict(
        uid=record.get("uid", "?"), mode=record.get("mode", "?"), trial=record.get("trial", 0),
        dim=case.get("dim", "?"),
        ask_user=sum((t.get("observations") or {}).get("ask_user", {}).get("count", 0)
                     for t in record.get("turns") or []),
    )
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
        """条目异常的单一判据（整改清单 R10）：优先按条目 status；调用方未提供
        status（直接构造 eff 的单测路径）时回退"取值为空且带算子来源"。"""
        st = (statuses or {}).get(item.id)
        if st is not None:
            return verd.value is None and st == "error"
        return verd.value is None and bool(verd.source)

    decided: dict[str, str] = {}
    undecided: list[str] = []
    errored: list[str] = []

    def count(item: Item) -> None:
        verd = verd_of(item)
        if verd.source == "rule":
            v.by_rule += 1
        elif verd.source == "judge":
            v.by_judge += 1
        elif verd.source == "human":
            v.by_human += 1

    # 1 前提（前提与路径不计入 by_rule/by_judge，与现行一致）
    precond_fail = False
    for item in preconds:
        verd = verd_of(item)
        if verd.value is None:
            (errored if is_error(item, verd) else undecided).append(item.id)
            continue
        decided[item.id] = verd.value
        v.precondition.append({"id": item.id, "hold": verd.value == "成立"})
        if verd.value != "成立":
            precond_fail = True

    # 2 路径：forced 会话不判路径（条目不参与结论）
    path_vals: list[str] = []
    for item in paths:
        verd = verd_of(item)
        if forced:
            continue
        if verd.value is None:
            (errored if is_error(item, verd) else undecided).append(item.id)
            continue
        decided[item.id] = verd.value
        path_vals.append(verd.value)
    v.path = "n/a" if forced else ("violate" if "违反" in path_vals else "ok")
    path_violate = not forced and "违反" in path_vals

    # 事实条目是否参与结论：前提已判不成立 / 路径已违反 / 拒答型会话时被短路
    # 路径违反只在否决方式为 zero 时短路事实（封顶 / 按权重时事实分仍要计）
    facts_live = (not precond_fail and answerable
                  and not (path_violate and policy.score.veto["path_violated"] == "zero"))

    # 3 拒答（仅 answerable=false 的会话）
    if not answerable and refusal is not None:
        verd = verd_of(refusal)
        if verd.value is None:
            (errored if is_error(refusal, verd) else undecided).append(refusal.id)
        else:
            decided[refusal.id] = verd.value

    # 4/5 事实与禁止事实：分类全部（两点求值要用），记录仍沿短路门
    for item in forbids + requireds:
        verd = verd_of(item)
        if verd.value is None:
            if facts_live:
                (errored if is_error(item, verd) else undecided).append(item.id)
            continue
        decided[item.id] = verd.value
    sp = policy.score
    for item in expecteds:                           # 期望事实按权重计分（权重 0 只记录）
        verd = verd_of(item)
        if verd.value is not None:
            decided[item.id] = verd.value
        elif facts_live and weight_of(item, sp.default_weight) > 0:
            (errored if is_error(item, verd) else undecided).append(item.id)

    stage_open = (all(i.id in decided for i in preconds)
                  and (forced or all(i.id in decided for i in paths)))
    if stage_open and not answerable and refusal is not None and refusal.id in decided:
        count(refusal)                                # 拒答判定计入 by_*
    elif stage_open and facts_live:
        for item in forbids + requireds:
            count(item)
        for item in forbids:
            if item.id in decided:
                v.forbidden_adopted[item.id] = decided[item.id] == "采信"
        for item in requireds:
            if item.id in decided:
                v.facts[item.id] = decided[item.id]
        for item in items:
            if item.kind == "expected" and item.id in decided:
                v.expected_hit[item.id] = decided[item.id] == HIT

    # ---------------------------------------------------------------- 两点求值（§3.2，百分制）
    def value_of(item: Item, fav: bool) -> str:
        if item.id in decided:
            return decided[item.id]
        return (FAVORABLE if fav else UNFAVORABLE)[item.kind]

    def score_of(item: Item, fav: bool) -> float | None:
        """条目分；None = 不计分（路径"不适用"）。未判条目取 100 / 0。"""
        if item.id not in decided:
            return 100.0 if fav else 0.0
        verd = verd_of(item)
        if verd.score is not None:
            return float(verd.score)
        return sp.map.get(decided[item.id])

    def part(item: Item, fav: bool, weight: float | None = None) -> dict:
        w = weight_of(item, sp.default_weight) if weight is None else weight
        return {"id": item.id, "kind": item.kind, "weight": w,
                "score": score_of(item, fav), "known": item.id in decided}

    def evaluate(fav: bool) -> dict:
        for item in preconds:
            if value_of(item, fav) != "成立":
                return {"invalid": True, "reason": "前提不成立（不计为答错）"}
        units: list[dict] = []
        vetoes: list[str] = []

        def veto(key: str, hit: bool, scored_items: list[Item]) -> None:
            if sp.veto[key] == "weight":
                units.extend(part(i, fav) for i in scored_items)
            elif hit:
                vetoes.append(key)

        if not forced:
            veto("path_violated", any(value_of(i, fav) == "违反" for i in paths), paths)
        if not answerable:
            if refusal is not None:
                units.append(part(refusal, fav, weight=1.0))
        else:
            veto("forbidden_adopted", any(value_of(i, fav) == "采信" for i in forbids), forbids)
            # 必要事实判"错"只看实际值：未决条目的假想取值不触发（§3.1）
            if sp.veto["required_wrong"] != "weight" and any(decided.get(i.id) == CONTRA for i in requireds):
                vetoes.append("required_wrong")
            groups: dict[tuple, list[Item]] = defaultdict(list)
            for item in requireds + expecteds:
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
        den = sum(u["weight"] for u in counted)
        auto = _round(sum(u["weight"] * u["score"] for u in counted) / den) if den else 100.0
        score = auto
        for key in vetoes:
            score = 0.0 if sp.veto[key] == "zero" else min(score, sp.cap)
        return {"invalid": False, "auto": auto, "score": score, "vetoes": vetoes, "parts": units}

    fav, unf = evaluate(True), evaluate(False)
    v.undecided_items = undecided
    if fav["invalid"]:                                   # 前提实际不成立
        v.status, v.invalid_reason = "invalid", fav["reason"]
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
    elif policy.undecided == "fail":
        v.status, v.invalid_reason = "fail", "有未决条目"
    else:
        v.status, v.invalid_reason = "undecided", "有未决条目"
    return v
