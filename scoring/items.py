"""拆条规则（方案 §4.2）：从一行 EpisodeRecord 拆出待判条目。

除注明外全部为现行 scoring.py（v2 单文件版）行为的迁移，出处见各函数；
唯一扩展：会话无效或失败时后续条目仍拆出（仍判定、仍落盘，但不参与会话
结论，见 rules.session_conclusion 的短路顺序）。

题目指纹（评测结果管理改造方案 §4.1）也在本模块计算：
- 输入指纹 input_fp：history + 用户消息序列 + 注入（point/option/rev/file/key/value）
- 标准指纹 standard_fp：answerable + 前提 + 事实（id/kind/text/keywords/turn/group/nature/partial_rule）+ 路径约束
- 条目指纹 item_sha：条目对人判断的实质内容（关键词、组、用途、权重不进入；性质与部分判据
  仅在主观条目上写键——doc/16 §3.6，保证存量指纹与人工改判零变化）
参考答案、备注、证据、标签、状态、关键条件的改动不影响任何指纹。

条目计分配置（doc/17 §2/§3）：用途（计分/参考）与权重都不进标准，md 编译进 dist
顶层 scoring 块，导入后落 case_scoring 表；改用途或权重只改计分（rescore），
不产生新标准、不触发重判。节点缺省与默认规则见表 NODES（全仓唯一来源）。

判据表 CRITERIA 与推导 verdict_from_criteria（doc/16 §4）是判定契约的一部分：
P1 的模型算子/复核导读只产出判据结果，结论一律由推导函数得出，不在 prompt 里复述逻辑。
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

# ---------------------------------------------------------------- 节点表（doc/17 §2.1）
# 全仓唯一来源：节点缺省用途、默认规则与展示文案都从这里生成（复核队列与会话结论
# 的门控口径也只有一份实现，rules.live_items）。
#   use   缺省用途（score 计分 / ref 参考），条目可显式改写（拒答固定计分）
#   veto  默认规则：该结论词触发否决（None = 无否决规则）；与用途、性质均无关
#   gate  门控：该结论词 → 会话无效（仅前提）
NODES: dict[str, dict[str, Any]] = {
    "precond":   {"label": "前提",     "use": "ref",   "veto": None,    "gate": "不成立"},
    "path":      {"label": "路径",     "use": "ref",   "veto": "违反",  "gate": None},
    "refusal":   {"label": "拒答",     "use": "score", "veto": None,    "gate": None},
    "required":  {"label": "必要事实", "use": "score", "veto": "错",    "gate": None},
    "expected":  {"label": "期望事实", "use": "score", "veto": None,    "gate": None},
    "forbidden": {"label": "禁止事实", "use": "ref",   "veto": "采信",  "gate": None},
}


def node_hint(kind: str) -> str:
    """节点说明文案（页面/复核页共用，替代原先写死的多处字符串）。"""
    n = NODES[kind]
    if n["gate"]:
        return "不成立 → 会话无效"
    if n["veto"]:
        return f"「{n['veto']}」→ 否决"
    return "无默认规则"

# ---------------------------------------------------------------- 判据表（doc/16 §4）
# 判定契约：按 类型 × 性质 固定要看的几个角度，与 SCALES 一样随代码版本走，不做配置文件。
# 改动判据（措辞或结构）必须升 CRITERIA_VER（criteria@N），模型算子随之升版本、缓存失效。
CRITERIA_VER = "criteria@1"

_FACT_CRITERIA_OBJECTIVE = (
    {"id": "C1", "name": "值一致", "rule": "关键值、对象、条件与条目一致"},
    {"id": "C2", "name": "完整", "rule": "条目要求的内容都覆盖到了"},
    {"id": "C3", "name": "无误导", "rule": "没有改变含义的限定、混淆或附加错误"},
)
_FACT_CRITERIA_SUBJECTIVE = (
    {"id": "C1", "name": "覆盖", "rule": "回答提出了该做法或等价做法"},
    {"id": "C2", "name": "充分", "rule": "给出理由、适用条件或执行要点（有部分判据时以它为准）"},
    {"id": "C3", "name": "不相悖", "rule": "没有给出与该做法相反的建议"},
)
CRITERIA: dict[tuple[str, str], tuple[dict[str, str], ...]] = {
    ("required", "objective"): _FACT_CRITERIA_OBJECTIVE,
    ("expected", "objective"): _FACT_CRITERIA_OBJECTIVE,
    ("required", "subjective"): _FACT_CRITERIA_SUBJECTIVE,
    ("expected", "subjective"): _FACT_CRITERIA_SUBJECTIVE,
    ("forbidden", "objective"): (
        {"id": "C1", "name": "陈述", "rule": "回答中出现了该错误内容"},
        {"id": "C2", "name": "采信", "rule": "作为结论或依据使用，而不是在纠正时复述"},
    ),
    ("forbidden", "subjective"): (
        {"id": "C1", "name": "提及", "rule": "回答中出现了该做法"},
        {"id": "C2", "name": "推荐", "rule": "作为建议提出，而不是作为反例提及"},
    ),
}

CRIT_SAT, CRIT_UNSAT, CRIT_UNDECIDABLE = "满足", "不满足", "无法判断"


def _crit_result(criteria: dict[str, dict], cid: str) -> str:
    """单个判据结果；缺失或取值非法按「无法判断」（P1 自洽校验据此送人工）。"""
    r = (criteria.get(cid) or {}).get("result")
    return r if r in (CRIT_SAT, CRIT_UNSAT, CRIT_UNDECIDABLE) else CRIT_UNDECIDABLE


def verdict_from_criteria(kind: str, nature: str | None, criteria: dict[str, dict]) -> str:
    """§4.2 推导：结论由判据结果按规则推出（从上往下命中即止），不由模型自由给出。

    criteria 形如 {"C1": {"result": "不满足", "contra": true}, ...}；客观条目 C1 的
    「不满足」需带 contra 子标记区分「给了矛盾值」（错）与「未涉及」（未提及）。
    返回 SCALES 内的结论词，或 "undecided"（任一判据无法判断 → 送人工）。
    """
    nature = nature or "objective"
    ids = [c["id"] for c in CRITERIA[(kind, nature)]]
    if any(_crit_result(criteria, c) == CRIT_UNDECIDABLE for c in ids):
        return "undecided"
    if kind in ("required", "expected"):
        if nature == "objective":
            if _crit_result(criteria, "C1") == CRIT_UNSAT:
                return "错" if (criteria.get("C1") or {}).get("contra") else "未提及"
            if any(_crit_result(criteria, c) == CRIT_UNSAT for c in ("C2", "C3")):
                return "部分"
            return "对"
        if _crit_result(criteria, "C3") == CRIT_UNSAT:      # 主观：给出相反建议 → 错
            return "错"
        if _crit_result(criteria, "C1") == CRIT_UNSAT:
            return "未提及"
        if _crit_result(criteria, "C2") == CRIT_UNSAT:
            return "部分"
        return "对"
    return "采信" if all(_crit_result(criteria, c) == CRIT_SAT for c in ids) else "未采信"


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
    tools_by_turn: tuple[frozenset[str], ...] = ()   # 各轮调用过的 tool 名（doc/18 §4.2）
    capabilities: dict | None = None                 # 适配器观测能力（doc/18 §6）；None = 缺省可观测

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


def called_tools_by_turn(record: dict) -> list[set[str]]:
    """逐轮调用过的 tool 名（doc/18 §4.2）：observations.tools 的 name 集合。"""
    out: list[set[str]] = []
    for t in record.get("turns") or []:
        names = {o["name"] for o in (t.get("observations") or {}).get("tools", []) if o.get("name")}
        out.append(names)
    return out


def build_context(record: dict, case: dict | None = None,
                  capabilities: dict | None = None) -> EpisodeContext:
    """case 缺省取 record 内嵌编译行；重判（期望修订）时传入 specs 表中的期望编译行。
    capabilities 为适配器观测能力声明（doc/18 §6，来自 run 的 capabilities_json）；
    缺省 None 视为全部可观测（DSH 家族与旧记录）。"""
    return EpisodeContext(
        record=record,
        case=case if case is not None else (record.get("case") or {}),
        outputs=tuple(live_outputs(record)),
        skills_by_turn=tuple(frozenset(s) for s in loaded_skills_by_turn(record)),
        tools_by_turn=tuple(frozenset(s) for s in called_tools_by_turn(record)),
        capabilities=capabilities,
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


def _fact_fp(f: dict) -> dict:
    """事实进标准指纹的载荷（doc/16 §3.6）：性质/部分判据仅在主观条目上写键，
    客观条目载荷与旧版逐字节相同 → 存量 standard_fp、item_sha 零变化。
    "objective" 与缺省等价（外部结构化输入可能显式写客观），同样不落键。"""
    row = {"id": f.get("id"), "kind": f.get("kind"), "text": f.get("text"),
           "keywords": f.get("keywords"), "turn": f.get("turn"), "group": f.get("group")}
    if f.get("nature") and f.get("nature") != "objective":
        row["nature"] = f["nature"]
    if f.get("partial_rule"):
        row["partial_rule"] = f["partial_rule"]
    return row


def standard_fp_of(case: dict) -> str:
    """标准指纹：结论能否直接比较。改事实/前提/路径约束 → 变；改输入/权重 → 不变。
    （v8 前名 judge_fp 且计入权重；权重移出后改权重不再产生新标准，doc/15 §3.2）"""
    return _sha16({
        "answerable": case.get("answerable", True),
        "preconditions": [{"id": p.get("id"), "text": p.get("text"), "turn": p.get("turn")}
                          for p in (case.get("preconditions") or [])],
        "facts": [_fact_fp(f) for f in (case.get("facts") or [])],
        "path": case.get("path") or [],
    })


def strip_scoring(spec: dict) -> dict:
    """spec 去计分配置（doc/17 §3.2）：顶层 scoring 键剥除，返回新 dict。
    specs 表落库与标准指纹计算前调用——standard_fp 不含计分，同指纹不同计分
    配置的 spec 行不可变，计分配置只能从 case_scoring 表读。"""
    out = dict(spec)
    out.pop("scoring", None)
    return out


def spec_items(spec: dict) -> list[tuple[str, str]]:
    """spec 的 (条目 id, kind) 清单，与 extract_items 的拆条一致（含拒答伪条目）。
    复核队列等不需要 record 的路径用它构造轻量条目。"""
    out = [(p.get("id"), "precond") for p in spec.get("preconditions") or [] if p.get("id")]
    out += [(f"path#{i}", "path") for i in range(1, len(spec.get("path") or []) + 1)]
    out += [(f.get("id"), f.get("kind", "required")) for f in spec.get("facts") or [] if f.get("id")]
    if spec.get("answerable") is False:
        out.append(("refusal", "refusal"))
    return out


def resolve_plan(spec: dict, scoring: dict | None = None) -> dict:
    """scoring 块 → 计分方案（doc/17 §2/§3）：按 spec 条目解析，显式用途优先、
    缺省取节点表；权重只落计分条目（缺 0）。返回 {"uses": {id: 用途},
    "weights": {id: 权重}, "unassigned": 计分权重合计是否为 0}。
    已不存在的条目忽略；新增条目按缺省用途、权重 0（标准变化后的过渡态）。"""
    entries = (scoring or {}).get("items") or {}
    uses: dict[str, str] = {}
    weights: dict[str, float] = {}
    for item_id, kind in spec_items(spec):
        e = entries.get(item_id) or {}
        use = e.get("use") if e.get("use") in ("score", "ref") else NODES[kind]["use"]
        uses[item_id] = use
        if use == "score":
            weights[item_id] = round(float(e.get("weight") or 0), 4)
    return {"uses": uses, "weights": weights, "unassigned": sum(weights.values()) <= 0}


# 人工会话分（doc/10 §4.2）挂在这个伪条目 id 上：item_sha 存 standard_fp，
# answer_sha 存整段回答指纹——标准或回答变了即作废（权重不再使其作废，doc/15 C4）。
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
    但不影响人对"这条事实是否成立"的判断，不进入指纹。性质与部分判据计入
    （doc/16 D1）：客观「部分 = 不精确」、主观「部分 = 不充分」，尺度变了旧改判作废；
    同样仅在主观条目上写键，客观条目 sha 与旧版一致。"""
    if item.kind == "path":
        payload = {"type": item.attrs.get("type"), "action": item.attrs.get("action"), "turn": item.attrs.get("turn")}
    elif item.kind == "refusal":
        payload = {"kind": "refusal"}
    else:
        payload = {"kind": item.kind, "text": item.attrs.get("text"), "turn": item.attrs.get("turn")}
        if item.attrs.get("nature") and item.attrs.get("nature") != "objective":
            payload["nature"] = item.attrs["nature"]
        if item.attrs.get("partial_rule"):
            payload["partial_rule"] = item.attrs["partial_rule"]
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
