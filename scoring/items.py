"""拆条规则（方案 §4.2）：从一行 EpisodeRecord 拆出待判条目。

除注明外全部为现行 scoring.py（v2 单文件版）行为的迁移，出处见各函数；
唯一扩展：会话无效或失败时后续条目仍拆出（仍判定、仍落盘，但不参与会话
结论，见 policy.session_conclusion 的短路顺序）。

题目指纹（评测结果管理改造方案 §4.1）也在本模块计算：
- 输入指纹 input_fp：history + 用户消息序列 + 注入（point/option/rev/file/key/value）
- 判定指纹 judge_fp：answerable + 前提 + 事实（id/kind/text/keywords/turn/group）+ 路径约束
- 条目指纹 item_sha：条目对人判断的实质内容（关键词、组、权重不进入）
参考答案、备注、证据、标签、状态、关键条件的改动不影响任何指纹。
权重（【权重：N】，复核服务改造方案 §4.2）进入判定指纹：改权重改变会话分。
"""
from __future__ import annotations

import hashlib
import json
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


def build_context(record: dict, case: dict | None = None) -> EpisodeContext:
    """case 缺省取 record 内嵌编译行；重判（期望修订）时传入 specs 表中的期望编译行。"""
    return EpisodeContext(
        record=record,
        case=case if case is not None else (record.get("case") or {}),
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


# ---------------------------------------------------------------- 题目指纹（改造方案 §4.1）

def _sha16(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def input_fp_of(case: dict) -> str:
    """输入指纹：回答能否直接比较。改用户消息或注入 → 变；改参考答案/备注 → 不变。
    topic 进入指纹：forced 会话的首轮 /skill名 前缀由 topic 经 Profile routing 决定，
    topic 改了 forced 答案的实际输入就变了（整改清单 R11）。"""
    inj = [{k: i.get(k) for k in ("point", "option", "rev", "file", "key", "value")}
           for i in (case.get("injections") or [])]
    return _sha16({
        "history": case.get("history", "live"),
        "topic": case.get("topic"),
        "turns": [t.get("content", "") for t in (case.get("turns") or []) if t.get("role") == "user"],
        "injections": inj,
    })


def judge_fp_of(case: dict) -> str:
    """判定指纹：结论能否直接比较。改事实/前提/路径约束 → 变；改输入 → 不变。"""
    return _sha16({
        "answerable": case.get("answerable", True),
        "preconditions": [{"id": p.get("id"), "text": p.get("text"), "turn": p.get("turn")}
                          for p in (case.get("preconditions") or [])],
        "facts": [{"id": f.get("id"), "kind": f.get("kind"), "text": f.get("text"),
                   "keywords": f.get("keywords"), "turn": f.get("turn"), "group": f.get("group"),
                   **({"weight": f["weight"]} if "weight" in f else {})}
                  for f in (case.get("facts") or [])],
        "path": case.get("path") or [],
    })


def weight_of(item: Item, default: float = 1) -> float:
    """条目权重：用例未写时取 policy 的 default_weight；0 = 只记录不计分。"""
    w = item.attrs.get("weight")
    return default if w is None else float(w)


# 人工会话分（复核服务改造方案 §4.2）挂在这个伪条目 id 上：item_sha 存 judge_fp，
# answer_sha 存整段回答指纹——期望或回答变了即作废。
TOTAL_ITEM = "@total"


def conversation_sha(ctx: EpisodeContext) -> str:
    """整段回答指纹：全部 live 输出按轮拼接。"""
    return hashlib.sha256("\n\u241e\n".join(ctx.outputs).encode("utf-8")).hexdigest()[:16]


def version_label(v: str | None) -> str:
    """评测集版本的显示/分组名：早期 run 的快照只有 file 全路径
    （如 D:\\...\\dist\\evalset-v20261022-1.jsonl），规整为 basename 去掉
    .jsonl 与 evalset- 前缀 → v20261022-1 / adhoc-202610220540-58。"""
    s = str(v or "?")
    if "\\" in s or "/" in s:
        s = s.replace("\\", "/").rsplit("/", 1)[-1]
    if s.endswith(".jsonl"):
        s = s[: -len(".jsonl")]
    return s.removeprefix("evalset-") or "?"


def qkey_of(case: dict) -> str:
    """题目身份 = uid + "|" + 输入指纹。"""
    return f"{case.get('uid', '?')}|{input_fp_of(case)}"


def item_sha_of(item: Item) -> str:
    """条目指纹：人工改判是否仍然有效的判据。关键词与组影响规则判定，
    但不影响人对"这条事实是否成立"的判断，不进入指纹。"""
    if item.kind == "path":
        payload = {"type": item.attrs.get("type"), "action": item.attrs.get("action"), "turn": item.attrs.get("turn")}
    elif item.kind == "refusal":
        payload = {"kind": "refusal"}
    else:
        payload = {"kind": item.kind, "text": item.attrs.get("text"), "turn": item.attrs.get("turn")}
    return _sha16(payload)


# ---------------------------------------------------------------- 变体泄露嫌疑（C5，整改清单 R6）

def variant_leak_suspects(records: list[dict]) -> list[dict]:
    """变体泄露嫌疑清单（规则与 CASE-STRUCTURE §8 提示对齐、整改清单 R6 修订）：

    - 只检查被替换的轮次：variant.scope=="turn" 查 variant.turn（未写视为最后一轮）；
      scope=="whole" 查全部轮；变体未写 variant 时视为整题改写、查全部轮。
    - "缺少本轮条件关键词"：只针对属于被查轮次的条件；没写轮次的条件视为属于最后一轮。
    - "包含更早轮次条件关键词"：只针对写了轮次且早于被查轮次的条件；没写轮次的
      条目不参与（否则单轮条件的任何后续命中都会误报）。种子（#0）不查。
    """
    out: list[dict] = []
    for r in records:
        uid = r.get("uid", "?")
        tail = uid.rsplit("#", 1)[-1]
        if not tail.isdigit() or int(tail) == 0:
            continue                                     # 种子不查
        case = r.get("case") or {}
        conds = [c for c in (case.get("conditions") or []) if c.get("keywords")]
        msgs = [t.get("content", "") for t in (case.get("turns") or []) if t.get("role") == "user"]
        if not msgs:
            continue
        variant = case.get("variant") or {}
        if (variant.get("scope") or "turn") == "turn":
            t = variant.get("turn")
            check_turns = [t] if isinstance(t, int) and 1 <= t <= len(msgs) else [len(msgs)]
        else:
            check_turns = list(range(1, len(msgs) + 1))
        for n in check_turns:
            msg = msgs[n - 1]
            for c in conds:
                turn = c.get("turn")
                cond_turn = turn if isinstance(turn, int) and 1 <= turn <= len(msgs) else len(msgs)
                if cond_turn == n and not any(kw in msg for kw in c["keywords"]):
                    out.append({"uid": uid, "turn": n,
                                "reason": f"第 {n} 轮缺少条件「{c.get('slot')}」关键词：{'、'.join(c['keywords'])}"})
                if isinstance(turn, int) and turn < n and any(kw in msg for kw in c["keywords"]):
                    out.append({"uid": uid, "turn": n,
                                "reason": f"第 {n} 轮含更早轮次条件「{c.get('slot')}」关键词：{'、'.join(c['keywords'])}"})
    return out
