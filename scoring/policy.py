"""口径层（方案 §7）：固定参数集 + 会话结论判定。

会话结论判定顺序与现行 score_episode 一致（§4.2 拆条规则的短路顺序）：
前提 invalid → 路径 fail → 拒答 → 事实（否决/全满足）→ 未决处理。
组的"满足其一"为内建语义（CASE-STRUCTURE §2.4），不开放配置。

未决与异常按"能否改变结论"处理（评测结果管理改造方案 §3.2/§3.3）：
未决/异常条目分别取最有利与最不利值各算一次会话结论，两次相同即确定；
不同时有异常条目记 error（不进分母，不受 policy.undecided 影响），否则按
policy.undecided。任一必要事实（单条或组成员）实际判"错"即失败（§3.1），
不受 veto_on_forbidden 控制——该开关只管禁止事实。
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .items import FACT_SCALE, EpisodeContext, Item
from .operators import Verdict

HIT, VAGUE, MISSING, CONTRA = FACT_SCALE

# 两点求值的假想取值（改造方案 §3.2）：未决/异常条目各取一次
FAVORABLE = {"required": HIT, "forbidden": "未采信", "precond": "成立", "path": "满足", "refusal": "拒答"}
UNFAVORABLE = {"required": CONTRA, "forbidden": "采信", "precond": "不成立", "path": "违反", "refusal": "未拒答"}


class PolicyError(Exception):
    """policy.yaml 不合法：拒绝运行。"""


@dataclass
class Policy:
    partial_as: str = "fail"          # pass / fail：必要事实判"部分"是否视为满足
    veto_on_forbidden: bool = True    # 禁止事实被采信即会话失败
    undecided: str = "exclude"        # exclude 不计入分母 / fail 记失败
    headline_mode: dict = field(default_factory=lambda: {"default": "natural", "错误知识纠正": "forced"})

    def headline(self, dim: str) -> str:
        return self.headline_mode.get(dim, self.headline_mode.get("default", "natural"))


def policy_from_dict(d: dict) -> Policy:
    p = Policy()
    if not isinstance(d, dict):
        raise PolicyError("policy 配置必须是映射")
    unknown = set(d) - {"partial_as", "veto_on_forbidden", "undecided", "headline_mode", "schema"}
    if unknown:
        raise PolicyError(f"policy 含未知参数：{sorted(unknown)}")
    if "partial_as" in d:
        if d["partial_as"] not in ("pass", "fail"):
            raise PolicyError("partial_as 只能是 pass 或 fail")
        p.partial_as = d["partial_as"]
    if "veto_on_forbidden" in d:
        p.veto_on_forbidden = bool(d["veto_on_forbidden"])
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
                       policy: Policy, routing: dict[str, list[str]] | None = None) -> EpisodeVerdict:
    """按 §7 顺序得出会话结论（改造方案 §3 两点求值）。

    条目先按实际值分类（确定 / 未决 / 异常）；会话结论由两点求值决定：
    未决与异常条目分别取最有利、最不利值各算一次，两次相同即确定。
    记录字段（facts / forbidden_adopted / 计数）沿现行短路顺序——前提/路径/
    拒答短路后，事实不进结论。"""
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
    forbids = [i for i in items if i.kind == "forbidden"]
    answerable = case.get("answerable", True) is not False

    def verd_of(item: Item) -> Verdict:
        return eff.get(item.id) or Verdict(None, "", "")

    def is_error(verd: Verdict) -> bool:
        """value 为空但带算子来源 → 算子异常（§3.3）；空 Verdict（缺行/整链弃权）→ 未决。"""
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
            (errored if is_error(verd) else undecided).append(item.id)
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
            (errored if is_error(verd) else undecided).append(item.id)
            continue
        decided[item.id] = verd.value
        path_vals.append(verd.value)
    v.path = "n/a" if forced else ("violate" if "违反" in path_vals else "ok")
    path_violate = not forced and "违反" in path_vals

    # 事实条目是否参与结论：前提已判不成立 / 路径已违反 / 拒答型会话时被短路
    facts_live = not precond_fail and not path_violate and answerable

    # 3 拒答（仅 answerable=false 的会话）
    if not answerable and refusal is not None:
        verd = verd_of(refusal)
        if verd.value is None:
            (errored if is_error(verd) else undecided).append(refusal.id)
        else:
            decided[refusal.id] = verd.value

    # 4/5 事实与禁止事实：分类全部（两点求值要用），记录仍沿短路门
    for item in forbids + requireds:
        verd = verd_of(item)
        if verd.value is None:
            if facts_live:
                (errored if is_error(verd) else undecided).append(item.id)
            continue
        decided[item.id] = verd.value
    for item in items:
        if item.kind == "expected":
            verd = verd_of(item)
            if verd.value is not None:
                decided[item.id] = verd.value        # 期望事实不进 undecided/errored，不参与结论

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

    # ---------------------------------------------------------------- 两点求值（§3.2）
    def value_of(item: Item, fav: bool) -> str:
        if item.id in decided:
            return decided[item.id]
        return (FAVORABLE if fav else UNFAVORABLE)[item.kind]

    def satisfied(value: str) -> bool:
        return value == HIT or (policy.partial_as == "pass" and value == VAGUE)

    def evaluate(fav: bool) -> tuple[str, str | None]:
        for item in preconds:
            if value_of(item, fav) != "成立":
                return "invalid", "前提不成立（不计为答错）"
        if not forced:
            for item in paths:
                if value_of(item, fav) == "违反":
                    return "fail", "路径约束违反"
        if not answerable:
            if refusal is None:
                return "fail", None
            return ("pass" if value_of(refusal, fav) == "拒答" else "fail"), None
        for item in forbids:
            if policy.veto_on_forbidden and value_of(item, fav) == "采信":
                return "fail", "禁止事实被采信"
        # 必要事实判"错"即失败（§3.1）：只看实际判定值——未决条目经两点求值参与，
        # 其假想取值不触发本条，否则"同组已满足时另一成员未决"永远无法确定。
        if any(decided.get(item.id) == CONTRA for item in requireds):
            return "fail", "必要事实判错"
        groups: dict[str, list[Item]] = defaultdict(list)
        ok = True
        for item in requireds:
            if item.attrs.get("group"):
                groups[item.attrs["group"]].append(item)
            elif not satisfied(value_of(item, fav)):
                ok = False
        for members in groups.values():
            if not any(satisfied(value_of(m, fav)) for m in members):
                ok = False
        return ("pass" if ok else "fail"), None

    # 结论取最有利一次的原因：该次中任何负面结论都来自实际判定值（假想值全取有利），
    # 原因不会引用未发生的事。
    fav_status, fav_reason = evaluate(True)
    unfav_status, _ = evaluate(False)
    v.undecided_items = undecided
    if fav_status == unfav_status:
        v.status, v.invalid_reason = fav_status, fav_reason
    elif errored:
        v.status, v.error = "error", "判定异常，结论无法确定：" + "、".join(errored)
    elif policy.undecided == "fail":
        v.status, v.invalid_reason = "fail", "有未决条目"
    else:
        v.status, v.invalid_reason = "undecided", "有未决条目"
    return v
