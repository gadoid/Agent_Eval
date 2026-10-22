"""拆条规则（方案 §4.2）：从一行 EpisodeRecord 拆出待判条目。

除注明外全部为现行 scoring.py（v2 单文件版）行为的迁移，出处见各函数；
唯一扩展：会话无效或失败时后续条目仍拆出（仍判定、仍落盘，但不参与会话
结论，见 policy.session_conclusion 的短路顺序）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

FACT_SCALE = ("对", "部分", "未提及", "错")
SCALES: dict[str, tuple[str, ...]] = {
    "required": FACT_SCALE,
    "expected": FACT_SCALE,
    "forbidden": ("采信", "未采信"),
    "precond": ("成立", "不成立"),
    "path": ("满足", "违反", "不适用"),
    "refusal": ("拒答", "未拒答"),
}


@dataclass(frozen=True)
class Item:
    """一个待判条目，由编译行拆出，id 在用例内唯一。attrs 为原样带出的条目字典
    （事实/前提/路径约束本身），keywords、group、evidence、action 从中读取。"""

    kind: str
    id: str
    text: str | None
    turn: int | None
    attrs: dict[str, Any]


@dataclass(frozen=True)
class EpisodeContext:
    record: dict
    case: dict
    outputs: tuple[str, ...]
    skills_by_turn: tuple[frozenset[str], ...]

    def answer_for(self, item: Item) -> str:
        """轮次定位（现行 _pick）：写了轮次且在 1..N 内取该轮；否则取最后一轮。"""
        turn = item.turn
        if turn is not None and 1 <= turn <= len(self.outputs):
            return self.outputs[turn - 1]
        return self.outputs[-1]


def live_outputs(record: dict) -> list[str]:
    """回答来源（现行 scoring.py:171-172）：先取 live 轮，没有时回退全部轮。"""
    turns = record.get("turns") or []
    outs = [t.get("output", "") for t in turns if t.get("source") == "live"]
    return outs or [t.get("output", "") for t in turns]


def loaded_skills_by_turn(record: dict) -> list[set[str]]:
    """逐轮已加载且加载成功（ok）的 skill 名（现行 scoring.py:84-89）。"""
    out: list[set[str]] = []
    for t in record.get("turns") or []:
        names = {s["name"] for s in (t.get("observations") or {}).get("skills", []) if s.get("ok")}
        out.append(names)
    return out


def build_context(record: dict) -> EpisodeContext:
    return EpisodeContext(
        record=record,
        case=record.get("case") or {},
        outputs=tuple(live_outputs(record)),
        skills_by_turn=tuple(frozenset(s) for s in loaded_skills_by_turn(record)),
    )


def episode_error(record: dict, ctx: EpisodeContext) -> str | None:
    """会话前置（现行 scoring.py:168-175）：运行未完成或无回答时不拆条目。"""
    if record.get("status") != "ok":
        return f"运行未完成：{record.get('failed_turn') or '会话'} {record.get('status')}"
    if not ctx.outputs:
        return "没有可判定的回答"
    return None


def extract_items(ctx: EpisodeContext) -> list[Item]:
    """按判定顺序拆条：前提 → 路径 → 拒答（仅 answerable=false）→ F/E/X。"""
    case = ctx.case
    items: list[Item] = []
    for p in case.get("preconditions") or []:
        items.append(Item("precond", p["id"], p.get("text"), p.get("turn"), p))
    for i, p in enumerate(case.get("path") or [], 1):
        items.append(Item("path", f"path#{i}", None, p.get("turn"), p))
    if case.get("answerable") is False:
        items.append(Item("refusal", "refusal", None, None, {}))
    for f in case.get("facts") or []:
        items.append(Item(f.get("kind", "required"), f["id"], f.get("text"), f.get("turn"), f))
    return items
