"""测试辅助：确定性假 judge 算子（行为等价于旧版测试的 mk_judges 默认值，供新旧对拍）
与会话/运行目录构造器。假算子经 tests.helpers:类名 以外部算子方式引用（方案 §5.1）。"""
from __future__ import annotations

import json
from pathlib import Path

from scoring.chain import config_from_dict
from scoring.items import FACT_SCALE, NODES as _NODES, SCALES
from scoring.operators import Verdict, operator


# ---------------------------------------------------------------- 假 judge 算子

@operator
class FakeFact:
    """默认：text in answer → 对，否则 未提及（等价旧 mk_judges.fact）。
    value 参数可强制固定输出（对拍"错/部分"路径）。"""

    id, version, source = "fake-fact", "1", "judge"
    scales = {"required": FACT_SCALE, "expected": FACT_SCALE}
    inputs = ("item.text", "answer")

    def __init__(self, value: str | None = None):
        self._value = value

    def judge(self, item, ctx):
        if self._value:
            return Verdict(self._value, self.source, f"{self.id}@{self.version}")
        t = item.text or ""
        return Verdict("对" if t in ctx.answer_for(item) else "未提及",
                       self.source, f"{self.id}@{self.version}")


@operator
class FakeForbidden:
    """text in answer → 采信（等价旧 mk_judges.forbidden）。adopted 参数可强制。"""

    id, version, source = "fake-forbid", "1", "judge"
    scales = {"forbidden": SCALES["forbidden"]}
    inputs = ("item.text", "answer")

    def __init__(self, adopted: bool | None = None):
        self._adopted = adopted

    def judge(self, item, ctx):
        if self._adopted is not None:
            v = "采信" if self._adopted else "未采信"
            return Verdict(v, self.source, f"{self.id}@{self.version}")
        t = item.text or ""
        return Verdict("采信" if t and t in ctx.answer_for(item) else "未采信",
                       self.source, f"{self.id}@{self.version}")


@operator
class FakeRefusal:
    """"不知道" in answer → 拒答（等价旧 mk_judges.refusal）。"""

    id, version, source = "fake-refusal", "1", "judge"
    scales = {"refusal": SCALES["refusal"]}
    inputs = ("answer",)

    def judge(self, item, ctx):
        return Verdict("拒答" if "不知道" in ctx.answer_for(item) else "未拒答",
                       self.source, f"{self.id}@{self.version}")


@operator
class FakePrecond:
    """hold 参数：True → 成立（等价旧 mk_judges.precond 默认），False → 不成立。"""

    id, version, source = "fake-precond", "1", "judge"
    scales = {"precond": SCALES["precond"]}
    inputs = ("item.text", "answer")

    def __init__(self, hold: bool = True):
        self._hold = hold

    def judge(self, item, ctx):
        return Verdict("成立" if self._hold else "不成立",
                       self.source, f"{self.id}@{self.version}")


@operator
class CountingFact:
    """总是"未提及"并累计调用次数（缓存与 rescore 不触发判定的验证）。"""

    id, version, source = "counting-fact", "1", "judge"
    scales = {"required": FACT_SCALE, "expected": FACT_SCALE}
    inputs = ("item.text", "answer")
    calls = 0

    def judge(self, item, ctx):
        CountingFact.calls += 1
        return Verdict("未提及", self.source, f"{self.id}@{self.version}")


@operator
class CountingFactV2:
    """与 CountingFact 同逻辑、id 不同（缓存键含 id@版本 的验证素材）。"""

    id, version, source = "counting-fact-v2", "1", "judge"
    scales = {"required": FACT_SCALE, "expected": FACT_SCALE}
    inputs = ("item.text", "answer")
    calls = 0

    def judge(self, item, ctx):
        CountingFactV2.calls += 1
        return Verdict("未提及", self.source, f"{self.id}@{self.version}")


def fake_config(**over) -> "config_from_dict":
    """默认链配置：required/expected 先 keyword 后 fake-fact（等价旧 judge_fact 顺序）。"""
    d = {
        "schema": 1,
        "operators": {
            "keyword": {"impl": "keyword"},
            "skill-path": {"impl": "skill-path"},
            "fake-fact": {"impl": "fake-fact"},
            "fake-forbid": {"impl": "fake-forbid"},
            "fake-refusal": {"impl": "fake-refusal"},
            "fake-precond": {"impl": "fake-precond"},
        },
        "chains": {
            "required": ["keyword", "fake-fact"],
            "expected": ["keyword", "fake-fact"],
            "forbidden": ["fake-forbid"],
            "precond": ["fake-precond"],
            "path": ["skill-path"],
            "refusal": ["fake-refusal"],
        },
    }
    d.update(over)
    return config_from_dict(d)


def cfg_fact(value, adopted=False):
    """固定取值的假 judge 配置：全部事实判 value、禁止事实按 adopted。"""
    return fake_config(operators={
        "keyword": {"impl": "keyword"}, "skill-path": {"impl": "skill-path"},
        "fake-fact": {"impl": "fake-fact", "value": value},
        "fake-forbid": {"impl": "fake-forbid", "adopted": adopted},
        "fake-refusal": {"impl": "fake-refusal"},
        "fake-precond": {"impl": "fake-precond"},
    })


@operator
class MapFact:
    """按条目文本查表的假 judge：mapping 未命中判"未提及"（组内一错一对等对拍场景）。"""

    id, version, source = "map-fact", "1", "judge"
    scales = {"required": FACT_SCALE, "expected": FACT_SCALE}
    inputs = ("item.text", "answer")

    def __init__(self, mapping: dict | None = None):
        self._map = dict(mapping or {})

    def judge(self, item, ctx):
        return Verdict(self._map.get(item.text or "", "未提及"), self.source, f"{self.id}@{self.version}")


def cfg_map(mapping, adopted=False):
    """文本查表假 judge 配置：事实按 mapping（文本→取值），禁止事实按 adopted。"""
    return fake_config(operators={
        "keyword": {"impl": "keyword"}, "skill-path": {"impl": "skill-path"},
        "fake-fact": {"impl": "map-fact", "mapping": mapping},
        "fake-forbid": {"impl": "fake-forbid", "adopted": adopted},
        "fake-refusal": {"impl": "fake-refusal"},
        "fake-precond": {"impl": "fake-precond"},
    })


# ---------------------------------------------------------------- 会话构造

def fact(id, kind="required", text="", keywords=(), turn=None, group=None, evidence=None, weight=None,
         use=None):
    f = {"id": id, "kind": kind, "text": text, "keywords": list(keywords), "turn": turn,
         "group": group, "evidence": evidence}
    if weight is not None:
        f["weight"] = weight
    if use is not None:
        f["use"] = use
    return f


def scoring_block_for(case: dict) -> dict:
    """给内嵌编译行补计分块（doc/17 §3.2）：按节点缺省用途；名义权重取条目上的
    weight（缺省 1 份，等价旧 default_weight=1 的相对权重语义），归一为百分比；
    没有计分条目时 unassigned（测试要计分就显式给用途/权重）。"""
    nodes = _NODES
    rows = []
    for p in case.get("preconditions") or []:
        rows.append((p["id"], "precond", p.get("use"),
                     float(p["weight"]) if p.get("weight") is not None else None))
    for i, p in enumerate(case.get("path") or [], 1):
        rows.append((f"path#{i}", "path", p.get("use"),
                     float(p["weight"]) if p.get("weight") is not None else None))
    for f in case.get("facts") or []:
        rows.append((f["id"], f.get("kind", "required"), f.get("use"),
                     float(f["weight"]) if f.get("weight") is not None else None))
    if case.get("answerable") is False:
        rw = case.get("拒答权重")
        rows.append(("refusal", "refusal", None, float(rw) if rw is not None else 1.0))
    entries: dict[str, dict] = {}
    units: dict[str, float] = {}
    group_w: dict[tuple, float] = {}
    group_of: dict[str, tuple] = {}
    for iid, kind, use, nominal in rows:
        use = use if use in ("score", "ref") else nodes[kind]["use"]
        entries[iid] = {"use": use}
        if use != "score":
            continue
        w = nominal if nominal is not None else 1.0
        g = None
        for f in case.get("facts") or []:
            if f.get("id") == iid and f.get("group"):
                g = (f.get("kind"), f["group"])
        if g:
            group_of[iid] = g
            group_w[g] = max(group_w.get(g, 0), w)
        else:
            units[iid] = w
    for iid, g in group_of.items():
        units[iid] = group_w[g]
    total = sum(units.values())
    for iid, e in entries.items():
        if e["use"] == "score":
            e["weight"] = round(units.get(iid, 0) / total * 100, 4) if total > 0 else 0.0
    return {"items": entries, "unassigned": total <= 0}


def turn(output="答", skills=(), ask_user=0, status="ok", index=None, source="live"):
    return {"index": index, "source": source, "input": "q", "output": output, "status": status,
            "observations": {"skills": [{"name": s, "ok": True} for s in skills], "tools": [],
                             "tool_errors": [], "usage": {"steps": 1, "tokens": {}},
                             "ask_user": {"count": ask_user, "auto": False}, "model": None}}


def mk_record(**over):
    base = {
        "schema": 3, "run_id": "r-test", "uid": "ec-1#0", "mode": "natural", "trial": 1, "status": "ok",
        "failed_turn": None, "evalset_version": "v1",
        "turns": [turn(output="答：规则是收齐尾款后才能电放。", skills=("trade-documents",))],
        "case": {
            "id": "ec-1", "uid": "ec-1#0", "dim": "错误知识纠正", "answerable": True,
            "granularity": "环节",
            "facts": [
                fact("F1", "required", "未收尾款不应电放"),
                fact("X1", "forbidden", "未收尾款也可以先电放", evidence="R1"),
            ],
            "preconditions": [], "path": [],
        },
    }
    base.update(over)
    for i, t in enumerate(base["turns"], 1):
        if t.get("index") is None:
            t["index"] = i
    case = base.get("case") or {}
    if isinstance(case, dict) and "scoring" not in case:
        case["scoring"] = scoring_block_for(case)   # doc/17 §3.2：无计分块 → 未分配 → skipped
    return base


def make_run(tmp: Path, name: str, records: list[dict], snapshot: dict | None = None,
             run_id: str | None = None, routing: dict | None = None) -> Path:
    """落一个 run 目录：results.jsonl + run.json。"""
    d = tmp / name
    d.mkdir(parents=True)
    (d / "results.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
                                     encoding="utf-8")
    run_json = {"run_id": run_id or name,
                "snapshot": snapshot or {"skills_mode": "default-roots", "evalset": {"sha256": "a"}},
                "profile": {"content": {"routing": routing or {}}}}
    (d / "run.json").write_text(json.dumps(run_json, ensure_ascii=False), encoding="utf-8")
    return d
