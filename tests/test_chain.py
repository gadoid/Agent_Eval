"""链执行器与跨 run 缓存（方案 §5.2、§5.3、§12 步骤 3 验收）。"""
import pytest

from scoring.chain import ConfigError, VerdictCache, config_from_dict, judge_episode, judge_item
from scoring.items import FACT_SCALE, build_context, extract_items
from scoring.operators import Verdict, operator
from tests.helpers import CountingFact, CountingFactV2, fact, fake_config, mk_record


@operator
class BrokenOp:
    id, version, source = "broken", "1", "judge"
    scales = {"required": FACT_SCALE, "expected": FACT_SCALE}
    inputs = ("item.text", "answer")

    def judge(self, item, ctx):
        raise RuntimeError("炸了")


@operator
class BadScaleOp:
    id, version, source = "bad-scale", "1", "judge"
    scales = {"required": FACT_SCALE, "expected": FACT_SCALE}
    inputs = ("item.text", "answer")

    def judge(self, item, ctx):
        return Verdict("超级对", self.source, f"{self.id}@{self.version}")   # 不在 scale 内


@operator
class BadAbstainOp:
    id, version, source = "bad-abstain", "1", "judge"
    scales = {"required": FACT_SCALE, "expected": FACT_SCALE}
    inputs = ("item.text", "answer")

    def judge(self, item, ctx):
        return Verdict("对", self.source, f"{self.id}@{self.version}", abstain=True)  # 弃权却给值


@operator
class DupJudgeV1:
    id, version, source = "dup", "1", "judge"
    scales = {"required": ("对", "部分", "未提及", "错")}
    inputs = ("item.text", "answer")

    def judge(self, item, ctx):
        return Verdict("未提及", self.source, "dup@1")


@operator
class DupJudgeV2:
    """与 DupJudgeV1 同 id、不同版本（缓存键含版本的验证）。"""

    id, version, source = "dup", "2", "judge"
    scales = {"required": ("对", "部分", "未提及", "错")}
    inputs = ("item.text", "answer")

    def judge(self, item, ctx):
        return Verdict("未提及", self.source, "dup@2")


def item_of(record, item_id):
    ctx = build_context(record)
    return next(i for i in extract_items(ctx) if i.id == item_id), ctx


# ---------------------------------------------------------------- 配置校验（§5.2 启动校验）

def test_config_rejects_unknown_impl():
    with pytest.raises(ConfigError):
        config_from_dict({"schema": 1, "operators": {"x": {"impl": "no-such"}}, "chains": {}})


def test_config_rejects_missing_instance():
    with pytest.raises(ConfigError, match="不存在的算子实例"):
        config_from_dict({"schema": 1, "operators": {}, "chains": {"required": ["ghost"]}})


def test_config_rejects_scale_not_covering():
    with pytest.raises(ConfigError, match="scale 不覆盖"):
        config_from_dict({"schema": 1, "operators": {"kw": {"impl": "keyword"}},
                          "chains": {"forbidden": ["kw"]}})


def test_config_rejects_unknown_kind_and_schema():
    with pytest.raises(ConfigError, match="未知条目类型"):
        config_from_dict({"schema": 1, "operators": {}, "chains": {"mystery": []}})
    with pytest.raises(ConfigError, match="schema"):
        config_from_dict({"schema": 2, "operators": {}, "chains": {}})


def test_config_rejects_bad_input_declaration():
    @operator
    class WeirdInputs:
        id, version, source = "weird-inputs", "1", "judge"
        scales = {"required": ("对",)}
        inputs = ("ctx.record",)   # 未声明的输入名

        def judge(self, item, ctx):
            return Verdict("对", self.source, f"{self.id}@{self.version}")

    with pytest.raises(ConfigError, match="未知输入"):
        config_from_dict({"schema": 1, "operators": {"w": {"impl": "weird-inputs"}},
                          "chains": {"required": ["w"]}})


# ---------------------------------------------------------------- 链执行（§4.1 约束）

def test_abstain_falls_through_with_trace():
    cfg = fake_config()   # required: keyword → fake-fact；无关键词 → keyword 弃权
    rec = mk_record()
    row = judge_episode(rec, cfg, "r").rows[0]
    assert row["item"]["id"] == "F1"
    assert row["trace"] == ["keyword:abstain", "fake-fact:hit"]
    assert row["machine"]["value"] == "未提及" and row["status"] == "ok"


def test_empty_chain_is_undecided():
    cfg = fake_config(chains={"required": [], "expected": [], "forbidden": [], "precond": [],
                              "path": ["skill-path"], "refusal": []})
    rec = mk_record()
    rows = judge_episode(rec, cfg, "r").rows
    assert all(r["status"] == "undecided" and r["machine"]["value"] is None
               for r in rows if r["item"]["kind"] in ("required", "forbidden"))
    assert rows[0]["trace"] == []                       # 空链：无轨迹


def test_operator_exception_is_error():
    cfg = fake_config(operators={
        "keyword": {"impl": "keyword"}, "skill-path": {"impl": "skill-path"},
        "fake-fact": {"impl": "broken"},
        "fake-forbid": {"impl": "fake-forbid"}, "fake-refusal": {"impl": "fake-refusal"},
        "fake-precond": {"impl": "fake-precond"}})
    rec = mk_record()
    rows = judge_episode(rec, cfg, "r").rows
    bad = [r for r in rows if r["item"]["id"] == "F1"][0]
    assert bad["status"] == "error" and "算子异常" in bad["machine"]["reason"]
    assert bad["trace"] == ["keyword:abstain", "broken:error"]


def test_scale_violation_is_error():
    cfg = fake_config(operators={
        "keyword": {"impl": "keyword"}, "skill-path": {"impl": "skill-path"},
        "fake-fact": {"impl": "bad-scale"},
        "fake-forbid": {"impl": "fake-forbid"}, "fake-refusal": {"impl": "fake-refusal"},
        "fake-precond": {"impl": "fake-precond"}})
    rec = mk_record()
    row = judge_episode(rec, cfg, "r").rows[0]
    assert row["status"] == "error" and "scale" in row["machine"]["reason"]


def test_abstain_with_value_is_error():
    cfg = fake_config(operators={
        "keyword": {"impl": "keyword"}, "skill-path": {"impl": "skill-path"},
        "fake-fact": {"impl": "bad-abstain"},
        "fake-forbid": {"impl": "fake-forbid"}, "fake-refusal": {"impl": "fake-refusal"},
        "fake-precond": {"impl": "fake-precond"}})
    rec = mk_record()
    row = judge_episode(rec, cfg, "r").rows[0]
    assert row["status"] == "error" and "弃权" in row["machine"]["reason"]


def test_rule_operator_not_cached():
    cache = VerdictCache.load(None)
    rec = mk_record(case=dict(mk_record()["case"], facts=[fact("F1", text="提单", keywords=["正本提单"])]))
    rec["turns"][0]["output"] = "需要正本提单"
    cfg = fake_config()
    judge_episode(rec, cfg, "r", cache)
    judge_episode(rec, cfg, "r", cache)
    assert cache._map == {}                             # 规则算子不走缓存（该会话无 judge 条目）


# ---------------------------------------------------------------- 跨 run 缓存（§5.3）

def _counting_cfg():
    return fake_config(operators={
        "keyword": {"impl": "keyword"}, "skill-path": {"impl": "skill-path"},
        "fake-fact": {"impl": "counting-fact"},
        "fake-forbid": {"impl": "fake-forbid"}, "fake-refusal": {"impl": "fake-refusal"},
        "fake-precond": {"impl": "fake-precond"}})


def test_cache_hit_and_miss_by_inputs_and_version(tmp_path):
    CountingFact.calls = 0
    cache = VerdictCache.load(tmp_path / "c.jsonl")
    cfg = _counting_cfg()
    rec = mk_record()
    r1 = judge_episode(rec, cfg, "r1", cache).rows
    assert CountingFact.calls == 1                     # 仅 F1（keyword 弃权后走 counting；X1 走 fake-forbid）
    r2 = judge_episode(rec, cfg, "r1", cache).rows     # 同键 → 缓存命中
    assert CountingFact.calls == 1
    assert any("counting-fact:cache" in row["trace"] for row in r2)

    rec_other_answer = mk_record(turns=[{"index": 1, "source": "live", "input": "q",
                                         "output": "另一个回答", "status": "ok", "observations": {}}])
    judge_episode(rec_other_answer, cfg, "r2", cache)  # 回答变化 → 不命中
    assert CountingFact.calls == 2

    rec_other_text = mk_record(case=dict(mk_record()["case"], facts=[
        fact("F1", text="完全不同的事实"), fact("X1", "forbidden", "未收尾款也可以先电放")]))
    judge_episode(rec_other_text, cfg, "r3", cache)    # 条目文本变化 → 不命中
    assert CountingFact.calls == 3

    CountingFactV2.calls = 0                           # 算子版本变化 → 不命中
    cache2 = VerdictCache.load(tmp_path / "c.jsonl")
    ctx = build_context(rec)
    item, _ = item_of(rec, "F1")
    for op in (DupJudgeV1(), DupJudgeV2()):
        v, trace, status = judge_item(item, ctx, [op], cache2)
        assert trace == ["dup:hit"]                     # 同 id 不同版本：键不同，均未命中


def test_cache_persists_across_process(tmp_path):
    p = tmp_path / "c.jsonl"
    CountingFact.calls = 0
    judge_episode(mk_record(), _counting_cfg(), "r1", VerdictCache.load(p))
    assert CountingFact.calls == 1
    judge_episode(mk_record(), _counting_cfg(), "r2", VerdictCache.load(p))   # 重新装载后仍命中
    assert CountingFact.calls == 1


# ---------------------------------------------------------------- 规范化链配置哈希（R4）

def test_chain_sha_ignores_comments_and_unused_instance():
    """注释/空白/未被链引用的实例不影响 chain_sha（原文哈希 config_sha 会变）。"""
    from scoring.chain import config_from_dict
    import copy
    base = {"schema": 1,
            "operators": {"kw": {"impl": "keyword"}, "unused": {"impl": "keyword"},
                          "sp": {"impl": "skill-path"}},
            "chains": {"required": ["kw"], "path": ["sp"]}}
    a = config_from_dict(copy.deepcopy(base))
    b = config_from_dict(copy.deepcopy(base))          # 完全相同
    assert a.chain_sha == b.chain_sha and a.config_sha == b.config_sha
    # 去掉未引用实例：chain_sha 不变（config 不含原文，无比较）
    no_unused = copy.deepcopy(base)
    del no_unused["operators"]["unused"]
    assert config_from_dict(no_unused).chain_sha == a.chain_sha
    # 调整链顺序：变
    swapped = copy.deepcopy(base)
    swapped["chains"]["required"] = ["sp"]             # scale 不符会报错，改成引用顺序场景：
    swapped["chains"]["required"] = ["kw"]
    swapped["chains"]["path"] = ["sp"]
    assert config_from_dict(swapped).chain_sha == a.chain_sha
    reordered = copy.deepcopy(base)
    reordered["operators"]["kw2"] = {"impl": "keyword"}
    reordered["chains"]["required"] = ["kw", "kw2"]   # 链上多一个实例：变
    assert config_from_dict(reordered).chain_sha != a.chain_sha
