"""算子契约、注册表与内置规则算子（方案 §4.1、§5.1、§9 首批算子）。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .items import FACT_SCALE, EpisodeContext, Item, SCALES


@dataclass
class Verdict:
    value: str | None    # 取自算子声明的 scale；弃权时为 None
    source: str          # rule / judge；人工覆盖后为 human
    operator: str        # 算子 id@版本，如 keyword@1；人工覆盖为 review
    reason: str = ""     # 命中片段或模型理由，给人看
    abstain: bool = False
    score: float | None = None   # 显式分 0–100（模型算子或人工自定）；None 时按 rules.score.map[value]

    def as_dict(self) -> dict[str, Any]:
        d = {"value": self.value, "source": self.source, "operator": self.operator, "reason": self.reason}
        if self.score is not None:
            d["score"] = self.score
        return d


@runtime_checkable
class Operator(Protocol):
    id: str
    version: str
    source: str                    # rule / judge
    scales: dict[str, tuple]       # 条目类型 → 合法取值
    inputs: tuple[str, ...]        # 读取的输入，决定缓存键（§5.3）
    def judge(self, item: Item, ctx: EpisodeContext) -> Verdict: ...


REGISTRY: dict[str, type] = {}


def operator(cls):
    """内置算子登记（§5.1）。外部算子以 `模块路径:类名` 引用，不必登记。"""
    REGISTRY[cls.id] = cls
    return cls


# ---------------------------------------------------------------- 关键词规则（沿用现行否定词窗口）

NEGATIONS = ("不是", "并非", "没有", "不由", "无需", "不", "非", "无", "未")
NEGATION_WINDOW = 4


def first_hit(keywords, answer: str) -> str | None:
    """返回第一个非否定语境命中的关键词（现行 keyword_hit 的返回值扩展）。"""
    for kw in keywords:
        start = answer.find(kw)
        while start >= 0:
            before = answer[max(0, start - NEGATION_WINDOW):start]
            if not any(neg in before for neg in NEGATIONS):
                return kw
            start = answer.find(kw, start + 1)
    return None


@operator
class KeywordOperator:
    """关键词规则（§9 首批）：命中判"对"，未写关键词或未命中一律弃权，交给链上
    后续算子——规则只负责判"对"，与现行 judge_fact 行为一致。"""

    id, version, source = "keyword", "1", "rule"
    scales = {"required": FACT_SCALE, "expected": FACT_SCALE}
    inputs = ("item.attrs.keywords", "answer")

    def judge(self, item: Item, ctx: EpisodeContext) -> Verdict:
        kws = item.attrs.get("keywords") or []
        if not kws:
            return Verdict(None, self.source, f"{self.id}@{self.version}", abstain=True)
        kw = first_hit(kws, ctx.answer_for(item))
        if kw:
            return Verdict("对", self.source, f"{self.id}@{self.version}", reason=f"命中：{kw}")
        return Verdict(None, self.source, f"{self.id}@{self.version}", abstain=True)


@operator
class SkillPathOperator:
    """路径约束规则（§9 首批，语义同现行 check_path / CASE-STRUCTURE §2.10）：
    forced 会话判"不适用"；必须=截至 N 轮（含）已加载过；禁止=第 N 轮 / 全程。"""

    id, version, source = "skill-path", "1", "rule"
    scales = {"path": SCALES["path"]}
    inputs = ()

    def judge(self, item: Item, ctx: EpisodeContext) -> Verdict:
        p = item.attrs
        opid = f"{self.id}@{self.version}"
        if ctx.record.get("mode") == "forced":
            return Verdict("不适用", self.source, opid, reason="forced 会话不判路径")
        per_turn = [set(s) for s in ctx.skills_by_turn]
        n = len(per_turn)
        skill = (p.get("action") or {}).get("skill")
        if not skill:
            return Verdict("满足", self.source, opid, reason="约束无 skill 动作")
        turn = p.get("turn")
        if p.get("type") == "must":
            upto = per_turn[: turn or n]
            loaded = [i + 1 for i in range(len(upto)) if skill in upto[i]]
            if loaded:
                return Verdict("满足", self.source, opid, reason=f"必须：{skill} 第 {loaded[0]} 轮已加载")
            return Verdict("违反", self.source, opid, reason=f"必须：截至第 {turn or n} 轮未加载 {skill}")
        window = [turn - 1] if turn else list(range(n))
        loaded = [i + 1 for i in window if i < n and skill in per_turn[i]]
        if loaded:
            return Verdict("违反", self.source, opid, reason=f"禁止：{skill} 第 {'、'.join(map(str, loaded))} 轮发生加载")
        return Verdict("满足", self.source, opid, reason=f"禁止：{'第 %d 轮' % turn if turn else '全程'}未加载 {skill}")
