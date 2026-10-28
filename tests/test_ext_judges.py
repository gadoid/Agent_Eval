"""模型判定算子（scoring/ext/judges）：判据契约推导、缓存短路、批量预填与解析容忍。

全程不调用真实 dsh：传输层用桩替换；本测试锁的是「模型输出判据 → 结论按推导表
得出 → verdict_cache 键与链执行一致」这条契约。
"""
import json

import pytest

from scoring.chain import VerdictCache, config_from_dict, judge_item
from scoring.ext.judges import LlmFact, LlmForbidden, batch_judge, extract_json
from scoring.items import build_context, extract_items

OPS = {
    "schema": 1,
    "operators": {
        "keyword": {"impl": "keyword"},
        "skill-path": {"impl": "skill-path"},
        "llm-fact": {"impl": "scoring.ext.judges:LlmFact",
                     "model": "stub", "dsh_bin": ["dsh"]},
        "llm-forbid": {"impl": "scoring.ext.judges:LlmForbidden",
                       "model": "stub", "dsh_bin": ["dsh"]},
    },
    "chains": {"required": ["keyword", "llm-fact"], "expected": ["keyword", "llm-fact"],
               "forbidden": ["llm-forbid"], "path": ["skill-path"]},
}


def fact(**crit):
    """构造模型返回的判据结果；未提及的判据默认满足。"""
    out = {"criteria": {"C1": {"result": "满足"}, "C2": {"result": "满足"},
                        "C3": {"result": "满足"}}, "reason": "stub"}
    for cid, r in crit.items():
        out["criteria"][cid] = {"result": r[0], **({"contra": r[1]} if len(r) > 1 else {})}
    return out


class StubTransport:
    def __init__(self):
        self.replies: list[str] = []
        self.calls: list[str] = []

    def ask(self, prompt):
        self.calls.append(prompt)
        return self.replies.pop(0)


@pytest.fixture()
def cfg():
    return config_from_dict(json.loads(json.dumps(OPS)))


def _record():
    from tests.helpers import mk_record
    return mk_record()


# ------------------------------------------------- 判据 → 结论推导（客观必要/期望）

def test_verdict_map_objective(cfg):
    op = cfg.instances["llm-fact"]
    op.sync = True
    ctx = build_context(_record())
    item = next(i for i in extract_items(ctx) if i.kind == "required")
    stub = StubTransport()

    def reply(payload):
        stub.replies = [json.dumps(payload, ensure_ascii=False)]
        return stub

    original = op.transport
    op.transport = lambda: reply(fact(C1=("满足",))) or stub
    assert op.judge(item, ctx).value == "对"

    op.transport = lambda: reply(fact(C1=("不满足", True)))
    assert op.judge(item, ctx).value == "错"                      # 给了矛盾值

    op.transport = lambda: reply(fact(C1=("不满足", False)))
    assert op.judge(item, ctx).value == "未提及"                   # 未涉及

    op.transport = lambda: reply(fact(C1=("满足",), C2=("不满足",)))
    assert op.judge(item, ctx).value == "部分"

    op.transport = lambda: reply(fact(C1=("满足",), C3=("无法判断",)))
    v = op.judge(item, ctx)
    assert v.value is None and v.abstain                          # 判据无法判断 → 送人工
    op.transport = original


def test_verdict_map_forbidden(cfg):
    op = cfg.instances["llm-forbid"]
    op.sync = True
    ctx = build_context(_record())
    item = next((i for i in extract_items(ctx) if i.kind == "forbidden"), None)
    if item is None:                                              # 桩用例无禁止条目时补一个
        return
    stub = StubTransport()

    def reply(payload):
        stub.replies = [json.dumps(payload, ensure_ascii=False)]
        return stub

    original = op.transport
    op.transport = lambda: reply(fact(C1=("满足",), C2=("满足",)))
    assert op.judge(item, ctx).value == "采信"
    op.transport = lambda: reply(fact(C1=("满足",), C2=("不满足",)))
    assert op.judge(item, ctx).value == "未采信"                   # 纠正时复述不算采信
    op.transport = original


# ------------------------------------------------- 未预填缓存 → 弃权送人工

def test_abstain_without_prefill(cfg):
    ctx = build_context(_record())
    item = next(i for i in extract_items(ctx) if i.kind == "required")
    v, trace, status = judge_item(item, ctx, cfg.chains["required"])
    assert v.abstain and status == "undecided"
    assert "judge" in trace[-1] or "llm-fact" in trace[-1]


# ------------------------------------------------- 批量预填的键与链执行一致

def test_batch_prefill_keys_match_chain(cfg):
    record = _record()
    ctx = build_context(record)
    items = [i for i in extract_items(ctx)
             if i.kind in ("required", "expected", "forbidden")]
    payload = []
    for it in items:
        if it.kind == "forbidden":
            payload.append({"id": it.id, "criteria": {"C1": {"result": "满足"},
                                                      "C2": {"result": "不满足"}}, "reason": "r"})
        else:
            payload.append({"id": it.id, "criteria": {"C1": {"result": "满足"}}, "reason": "r"})
    stub = StubTransport()
    stub.replies = [json.dumps(payload, ensure_ascii=False)]

    cache = VerdictCache()
    stats = batch_judge([record], cfg, cache, transport_of=lambda op: stub)
    assert stats["episodes"] >= 1 and stats["items"] == len(items) and stats["filled"] == len(items)
    assert len(stub.calls) == 1                                   # 一段会话一次调用

    # 链执行逐条命中缓存，不再需要模型
    for it in items:
        _v, trace, status = judge_item(it, ctx, cfg.chains[it.kind], cache)
        assert status == "ok" and any(t.endswith(":cache") for t in trace), trace


# ------------------------------------------------- 输出解析容忍围栏/前后缀

def test_extract_json_tolerates_fences():
    obj = {"id": "F1", "criteria": {"C1": {"result": "满足"}}}
    assert extract_json(json.dumps(obj)) == obj
    assert extract_json("```json\n" + json.dumps(obj) + "\n```") == obj
    assert extract_json("好的，以下是判定结果：\n" + json.dumps(obj)) == obj
    arr = [obj, {"id": "X1", "criteria": {}}]
    assert extract_json("前缀 " + json.dumps(arr) + " 后缀") == arr
    # dsh stdout 常带 [llm-…] 日志前缀：数组与单对象都要跳过它
    log = "[llm-deepseek-account] model listing loaded 2 model(s), skipped 0\n"
    assert extract_json(log + json.dumps(arr)) == arr
    assert extract_json(log + json.dumps(obj)) == obj
    # reason 里出现合法 JSON 数组时，位置在后的它不能抢在外层对象之前
    inner = {"id": "F1", "reason": "见清单 [\"a\"]", "criteria": {"C1": {"result": "满足"}}}
    assert extract_json(json.dumps(inner)) == inner
    with pytest.raises(ValueError):
        extract_json("完全不是 JSON")
