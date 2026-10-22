"""口径层（方案 §7）：固定参数集 + 会话结论判定。

会话结论判定顺序与现行 score_episode 一致（§4.2 拆条规则的短路顺序）：
前提 invalid → 路径 fail → 拒答 → 事实（否决/全满足）→ 未决处理。
组的"满足其一"为内建语义（CASE-STRUCTURE §2.4），不开放配置。
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .items import FACT_SCALE, EpisodeContext, Item
from .operators import Verdict

HIT, VAGUE, MISSING, CONTRA = FACT_SCALE


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
    """按 §7 顺序得出会话结论。items 全部已判定（§4.2 扩展），但短路步骤之后的
    条目不参与结论（与现行一致）；参与条目中有未决时按 policy.undecided 处理。"""
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

    undecided: list[str] = []

    def count(verdict: Verdict) -> None:
        if verdict.source == "rule":
            v.by_rule += 1
        elif verdict.source == "judge":
            v.by_judge += 1
        elif verdict.source == "human":
            v.by_human += 1

    def finish(status: str, reason: str | None = None) -> EpisodeVerdict:
        """已由确定条目得出结论；未决条目只在"本可通过"时改写结论（§7 规则 6）。"""
        if status == "pass" and undecided:
            v.undecided_items = undecided
            if policy.undecided == "fail":
                v.status, v.invalid_reason = "fail", "有未决条目"
            else:
                v.status, v.invalid_reason = "undecided", "有未决条目"
            return v
        v.status, v.invalid_reason = status, reason
        v.undecided_items = undecided
        return v

    def finish_undecided() -> EpisodeVerdict:
        """结论无法确定（如前提未决：无法区分 invalid 与后续路径）。"""
        v.undecided_items = undecided
        if policy.undecided == "fail":
            v.status, v.invalid_reason = "fail", "有未决条目"
        else:
            v.status, v.invalid_reason = "undecided", "有未决条目"
        return v

    def of_kind(kind: str) -> list[Item]:
        return [i for i in items if i.kind == kind]

    # 2 路径归并：先算出 path 状态（现行 scoring.py:177 在前提检查前即计算，
    # invalid/拒答的会话也带 path），"违反即败"的决策顺序仍排在前提之后。
    path_vals: list[str] = []
    for item in of_kind("path"):
        verd = eff.get(item.id) or Verdict(None, "", "")
        if verd.value is None:
            undecided.append(item.id)
        else:
            path_vals.append(verd.value)
    if record.get("mode") == "forced":
        v.path = "n/a"
    elif "违反" in path_vals:
        v.path = "violate"
    else:
        v.path = "ok"

    # 1 前提：任一不成立 → invalid（现行 scoring.py:183-186）。
    # 前提与路径不计入 by_rule/by_judge（现行只对事实/禁止事实/拒答计数）。
    precond_fail = False
    for item in of_kind("precond"):
        verd = eff.get(item.id) or Verdict(None, "", "")
        if verd.value is None:
            undecided.append(item.id)
        else:
            v.precondition.append({"id": item.id, "hold": verd.value == "成立"})
            if verd.value != "成立":
                precond_fail = True
    if precond_fail:
        return finish("invalid", "前提不成立（不计为答错）")
    if any(i in undecided for i in (x.id for x in of_kind("precond"))):
        return finish_undecided()

    if v.path == "violate":
        return finish("fail", "路径约束违反")
    if any(i in undecided for i in (x.id for x in of_kind("path"))):
        return finish_undecided()

    # 3 拒答（现行 scoring.py:191-195）：仅 answerable=false 的会话
    if case.get("answerable", True) is False:
        verd = eff.get("refusal") or Verdict(None, "", "")
        if verd.value is None:
            undecided.append("refusal")
            return finish_undecided()
        count(verd)
        return finish("pass" if verd.value == "拒答" else "fail")

    # 4/5 事实与禁止事实（现行 scoring.py:197-247）：先判定全部事实，再应用否决——
    # 否决的会话其事实结论仍记录（与现行一致）。
    req = of_kind("required")
    groups: dict[str, list[Item]] = defaultdict(list)
    singles = [i for i in req if not i.attrs.get("group")]
    for i in req:
        if i.attrs.get("group"):
            groups[i.attrs["group"]].append(i)

    for item in of_kind("forbidden"):
        verd = eff.get(item.id) or Verdict(None, "", "")
        count(verd)
        if verd.value is None:
            undecided.append(item.id)
        else:
            v.forbidden_adopted[item.id] = verd.value == "采信"

    def satisfied(value: str) -> bool:
        return value == HIT or (policy.partial_as == "pass" and value == VAGUE)

    all_pass = True
    for item in singles:
        verd = eff.get(item.id) or Verdict(None, "", "")
        count(verd)
        if verd.value is None:
            undecided.append(item.id)          # 未决不直接判败：由 finish 映射为 undecided
            continue
        v.facts[item.id] = verd.value
        if not satisfied(verd.value):
            all_pass = False
    for members in groups.values():
        group_ok = False
        group_undecided = False
        for item in members:
            verd = eff.get(item.id) or Verdict(None, "", "")
            count(verd)
            if verd.value is None:
                undecided.append(item.id)
                group_undecided = True
                continue
            v.facts[item.id] = verd.value
            if satisfied(verd.value):
                group_ok = True
        if not group_ok and not group_undecided:
            all_pass = False
    for item in of_kind("expected"):
        verd = eff.get(item.id) or Verdict(None, "", "")
        if verd.value is not None:
            v.expected_hit[item.id] = verd.value == HIT

    if policy.veto_on_forbidden and any(v.forbidden_adopted.values()):
        return finish("fail", "禁止事实被采信")
    return finish("pass" if all_pass else "fail")
