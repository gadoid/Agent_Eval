"""新旧对拍（方案 §12 步骤 1 验收）：同一份 results.jsonl 下，旧 score_episode 与
新管线（拆条 → 链 → 有效结论 → 口径）的会话结论与参与计分的条目结论全部一致。
假 judge 算子（tests.helpers）行为等价于旧版测试的 mk_judges 默认值。"""
from scoring.chain import judge_episode
from scoring.cli import conclusions
from scoring.rules import rules_from_dict, Rules
from scoring.rules import EpisodeVerdict
from scoring.views import ability_view, attribution_view, wilson
from tests import legacy_scoring_reference as legacy
from tests.helpers import cfg_fact, cfg_map, fact, fake_config, mk_record, turn


def run_old(record, judges=None):
    j = judges or legacy.Judges(
        fact=lambda t, a: legacy.HIT if t in a else legacy.MISSING,
        forbidden=lambda t, a: t in a,
        refusal=lambda a: "不知道" in a,
        precond=lambda t, a: True,
    )
    return legacy.score_episode(record, j)


def run_new(record, cfg=None, rules: Rules | None = None):
    cfg = cfg or fake_config()
    rules = rules or rules_from_dict({})
    rows = judge_episode(record, cfg, "r-test").rows
    return conclusions([record], rows, [], rules)[0]


def assert_parity(record, cfg=None, judges=None):
    old, new = run_old(record, judges), run_new(record, cfg)
    assert new.status == old.status, (old.status, new.status)
    assert new.facts == old.facts, (old.facts, new.facts)
    assert new.expected_hit == old.expected_hit
    assert new.forbidden_adopted == old.forbidden_adopted
    assert new.precondition == old.precondition
    assert new.path == old.path
    assert (new.by_rule, new.by_judge) == (old.by_rule, old.by_judge)
    return new


def judges_fixed(fact_value, forbidden=False):
    return legacy.Judges(
        fact=lambda t, a: fact_value,
        forbidden=lambda t, a: forbidden,
        refusal=lambda a: "不知道" in a,
        precond=lambda t, a: True,
    )


# ---------------------------------------------------------------- 对拍：七条路径（步骤 1 验收）

def test_parity_keyword_hit():
    rec = mk_record(case=dict(mk_record()["case"], facts=[fact("F1", text="提单", keywords=["正本提单"])]))
    rec["turns"][0]["output"] = "需要正本提单"
    v = assert_parity(rec)
    assert v.status == "pass" and v.facts["F1"] == "对" and v.by_rule == 1


def test_parity_keyword_negation_falls_to_judge():
    rec = mk_record(case=dict(mk_record()["case"], facts=[fact("F1", text="提单", keywords=["正本提单"])]))
    rec["turns"][0]["output"] = "并非正本提单，副本即可"
    v = assert_parity(rec)                      # 否定语境不认 → 交 judge（"提单"在回答中 → 对）
    assert v.facts["F1"] == "对" and v.by_judge == 1
    rec2 = mk_record(case=dict(mk_record()["case"], facts=[fact("F1", text="应当直接电放")]))
    v2 = assert_parity(rec2)
    assert v2.status == "fail" and v2.facts["F1"] == "未提及"


def test_parity_group_any_member():
    case = dict(mk_record()["case"], facts=[
        fact("F1", text="甲说法", group="G1"), fact("F2", text="乙说法", group="G1")])
    rec = mk_record(case=case)
    rec["turns"][0]["output"] = "答：乙说法成立。"
    v = assert_parity(rec)
    assert v.status == "pass"                    # 同组满足其一
    assert v.facts == {"F1": "未提及", "F2": "对"}


def test_parity_forbidden_veto():
    rec = mk_record()
    rec["turns"][0]["output"] = "答：未收尾款不应电放。未收尾款也可以先电放。"
    v = assert_parity(rec)
    assert v.status == "fail" and v.forbidden_adopted == {"X1": True}


def test_parity_precond_invalid():
    case = mk_record()["case"]
    case["preconditions"] = [{"id": "P1", "turn": 1, "text": "答复为可以电放", "injections": []}]
    rec = mk_record(case=case)
    cfg = fake_config(operators={
        "keyword": {"impl": "keyword"}, "skill-path": {"impl": "skill-path"},
        "fake-fact": {"impl": "fake-fact"},
        "fake-forbid": {"impl": "fake-forbid"}, "fake-refusal": {"impl": "fake-refusal"},
        "fake-precond": {"impl": "fake-precond", "hold": False},
    })
    judges = legacy.Judges(fact=lambda t, a: legacy.HIT if t in a else legacy.MISSING,
                           forbidden=lambda t, a: t in a, refusal=lambda a: False, precond=lambda t, a: False)
    v = assert_parity(rec, cfg=cfg, judges=judges)
    assert v.status == "invalid" and "前提不成立" in v.invalid_reason
    assert v.facts == {}                         # 未参与计分的条目不进结论


def test_parity_path_semantics():
    case = mk_record()["case"]
    case["path"] = [{"type": "must", "action": {"kind": "load_skill", "skill": "trade-documents"}, "turn": 1}]
    case["facts"] = [fact("F1", text="未收尾款不应电放", turn=1)]
    rec = mk_record(case=case, turns=[turn(output="答", skills=()), turn(output="答：未收尾款不应电放。", skills=("trade-documents",))])
    v = assert_parity(rec)
    assert v.status == "fail" and v.path == "violate"      # 第 1 轮没加载、第 2 轮才加载 → 违反
    rec2 = mk_record(case=case, turns=[turn(output="答：未收尾款不应电放。", skills=("trade-documents",)), turn(output="好")])
    assert assert_parity(rec2).status == "pass"
    case["path"] = [{"type": "forbid", "action": {"kind": "load_skill", "skill": "trade-finance"}}]
    rec3 = mk_record(case=case, turns=[turn(output="答：未收尾款不应电放。", skills=("trade-finance",))])
    assert assert_parity(rec3).path == "violate"           # 禁止：任何一轮加载即违反
    rec4 = mk_record(case=case, mode="forced", turns=[turn(output="答：未收尾款不应电放。", skills=("trade-finance",))])
    v4 = assert_parity(rec4)
    assert v4.path == "n/a" and v4.status == "pass"        # forced 不判路径


def test_parity_refusal():
    case = mk_record()["case"]
    case["answerable"] = False
    case["facts"] = []
    ok = mk_record(case=case, turns=[turn(output="知识库中没有相关记录，不知道")])
    v = assert_parity(ok)
    assert v.status == "pass" and v.by_judge == 1
    bad = mk_record(case=case)
    assert assert_parity(bad).status == "fail"


def test_parity_run_error_and_no_output():
    rec = mk_record(status="error", failed_turn=1)
    assert assert_parity(rec).status == "error"
    assert assert_parity(mk_record(turns=[])).status == "error"


def test_parity_fact_contra_and_expected():
    rec = mk_record(case=dict(mk_record()["case"], facts=[
        fact("F1", text="未收尾款不应电放"), fact("E1", "expected", "指出规则出处")]))
    v = assert_parity(rec, cfg=cfg_fact("错"), judges=judges_fixed(legacy.CONTRA))
    assert v.status == "fail" and v.facts["F1"] == "错" and v.expected_hit == {"E1": False}
    v2 = assert_parity(rec, cfg=cfg_fact("对"), judges=judges_fixed(legacy.HIT))
    assert v2.status == "pass" and v2.expected_hit == {"E1": True}


def test_parity_multi_turn_pick():
    """轮次定位：写了轮次取该轮，未写取最后一轮。"""
    case = dict(mk_record()["case"], facts=[
        fact("F1", text="甲", turn=1), fact("F2", text="乙")])
    rec = mk_record(case=case, turns=[turn(output="甲"), turn(output="乙")])
    v = assert_parity(rec)
    assert v.facts == {"F1": "对", "F2": "对"} and v.status == "pass"


# ---------------------------------------------------------------- 组内"错"对拍（改造方案 §3.1，步骤 0 验收）

def test_parity_group_contra_with_hit():
    """组内一错一对：旧实现 vetoed→fail，新实现（C1）也 fail。"""
    case = dict(mk_record()["case"], facts=[
        fact("F1", text="甲说法", group="G1"), fact("F2", text="乙说法", group="G1")])
    judges = legacy.Judges(fact=lambda t, a: legacy.CONTRA if t == "甲说法" else legacy.HIT,
                           forbidden=lambda t, a: False, refusal=lambda a: False, precond=lambda t, a: True)
    v = assert_parity(mk_record(case=case), cfg=cfg_map({"甲说法": "错", "乙说法": "对"}), judges=judges)
    assert v.status == "fail" and v.facts == {"F1": "错", "F2": "对"}


def test_parity_group_contra_with_missing():
    """组内一错一未提及：组不满足且含"错"，新旧都 fail。"""
    case = dict(mk_record()["case"], facts=[
        fact("F1", text="甲说法", group="G1"), fact("F2", text="乙说法", group="G1")])
    judges = legacy.Judges(fact=lambda t, a: legacy.CONTRA if t == "甲说法" else legacy.MISSING,
                           forbidden=lambda t, a: False, refusal=lambda a: False, precond=lambda t, a: True)
    v = assert_parity(mk_record(case=case), cfg=cfg_map({"甲说法": "错"}), judges=judges)
    assert v.status == "fail"


def test_parity_single_contra_with_forbidden_adopted():
    """单条为"错"且禁止事实被采信：新旧都 fail（否决优先，均为失败）。"""
    judges = legacy.Judges(fact=lambda t, a: legacy.CONTRA, forbidden=lambda t, a: True,
                           refusal=lambda a: False, precond=lambda t, a: True)
    v = assert_parity(mk_record(), cfg=cfg_map({"未收尾款不应电放": "错"}, adopted=True), judges=judges)
    assert v.status == "fail" and v.facts == {"F1": "错"} and v.forbidden_adopted == {"X1": True}


# ---------------------------------------------------------------- 迁移后的聚合单测

def test_wilson():
    assert wilson(0, 0) == (0.0, 0.0)
    lo, hi = wilson(8, 10)
    assert 0.4 < lo < 0.8 and 0.8 < hi <= 1.0


def test_run_aggregation_headline_and_error():
    cfg = cfg_fact("对")
    records = [mk_record(mode="forced"), mk_record(mode="forced"),
               mk_record(mode="forced", status="error", failed_turn=1)]
    rules = rules_from_dict({})
    rows = []
    for r in records:
        rows.extend(judge_episode(r, cfg, "r").rows)
    verdicts = conclusions(records, rows, [], rules)
    cell = ability_view(verdicts, rules)["错误知识纠正|forced"]
    assert cell["n"] == 2 and cell["pass"] == 2 and cell["headline"] is True
    assert cell["error"] == 1


def test_attribution_three_way():
    verdicts = [EpisodeVerdict(uid="u", mode="natural", trial=1, dim="d", status=st,
                               target_loaded=tgt, other_loaded=other)
                for tgt, other, st in [(False, False, "fail"), (False, True, "fail"),
                                       (True, False, "fail"), (True, False, "pass")]]
    at = attribution_view(verdicts)
    assert at["n"] == 4 and at["not_loaded"] == 0.25 and at["mis_loaded"] == 0.25 and at["loaded_wrong"] == 0.25
