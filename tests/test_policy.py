"""口径层（方案 §7、§12 步骤 2 验收）：固定参数、会话结论判定顺序、未决处理。"""
import pytest

from scoring.items import build_context, extract_items
from scoring.operators import Verdict
from scoring.policy import Policy, policy_from_dict
from scoring.rules import Rules, rules_from_dict, session_conclusion
from scoring.policy import rules_dict_of_policy


def rules_of(policy) -> Rules:
    """旧 policy（dict 或 Policy 对象）→ Rules（v8 迁移同式转换）：保住旧用例体的口径定义。"""
    if isinstance(policy, Policy):
        return rules_from_dict(rules_dict_of_policy(policy))
    return rules_from_dict(rules_dict_of_policy(policy_from_dict(policy)))

from tests.helpers import fact, mk_record


def V(value, source="judge"):
    return Verdict(value, source, "x@1") if value is not None else Verdict(None, "", "")


def conclude(record, eff_over, policy=None, override=None):
    policy = policy or policy_from_dict({})
    ctx = build_context(record)
    items = extract_items(ctx)
    eff = {i.id: eff_over.get(i.id, V(None)) for i in items}
    return session_conclusion(record, ctx, items, eff, rules_of(policy), override=override)


def test_policy_validation():
    with pytest.raises(Exception):
        policy_from_dict({"partial_as": "pass"})         # 已由 score 节取代
    with pytest.raises(Exception):
        policy_from_dict({"veto_on_forbidden": False})
    with pytest.raises(Exception):
        policy_from_dict({"undecided": "ignore"})
    with pytest.raises(Exception):
        policy_from_dict({"headline_mode": {}})          # 缺 default
    with pytest.raises(Exception):
        policy_from_dict({"mystery": 1})                 # 未知参数
    with pytest.raises(Exception):
        policy_from_dict({"schema": 1})
    with pytest.raises(Exception):
        policy_from_dict({"score": {"veto": {"forbidden_adopted": "maybe"}}})
    with pytest.raises(Exception):
        policy_from_dict({"score": {"pass_mark": 120}})
    with pytest.raises(Exception):
        policy_from_dict({"score": {"map": {"对": 100}}})  # map 必须覆盖全部结论词
    p = policy_from_dict({"schema": 2, "undecided": "fail", "headline_mode": {"default": "natural"},
                          "score": {"pass_mark": 70, "veto": {"forbidden_adopted": "cap", "cap": 30},
                                    "dimensions": {"知识召回": 2}}})
    assert p.undecided == "fail" and p.score.pass_mark == 70 and p.score.cap == 30
    assert p.score.veto == {"forbidden_adopted": "cap", "path_violated": "zero", "required_wrong": "zero"}
    assert p.score.dimensions == {"知识召回": 2.0}


def test_shipped_policy_yaml_loads():
    from scoring.policy import default_policy_path, load_policy
    p, sha = load_policy(default_policy_path())
    assert p.score.pass_mark == 60 and p.score.exclude_rewrites == ("诱导",) and len(sha) == 64


# ---------------------------------------------------------------- 判定顺序（§7 与现行一致）

def test_precond_invalid_short_circuits_facts():
    case = mk_record()["case"]
    case["preconditions"] = [{"id": "P1", "turn": 1, "text": "前提", "injections": []}]
    v = conclude(mk_record(case=case), {"P1": V("不成立"), "F1": V("错")})
    assert v.status == "invalid" and "前提不成立" in v.invalid_reason
    assert v.facts == {}                                 # 事实不参与结论


def test_path_violation_before_facts():
    case = mk_record()["case"]
    case["path"] = [{"type": "must", "action": {"kind": "load_skill", "skill": "s"}, "turn": 1}]
    v = conclude(mk_record(case=case), {"path#1": V("违反"), "F1": V("对")})
    assert v.status == "fail" and "路径约束违反" in v.invalid_reason and v.path == "violate"


def test_refusal_short_circuits_facts():
    case = mk_record()["case"]
    case["answerable"] = False
    v = conclude(mk_record(case=case), {"refusal": V("拒答"), "F1": V("错")})
    assert v.status == "pass" and v.facts == {}
    v2 = conclude(mk_record(case=case), {"refusal": V("未拒答")})
    assert v2.status == "fail"


def test_forbidden_veto_modes():
    v = conclude(mk_record(), {"F1": V("对"), "X1": V("采信")})
    assert v.status == "fail" and v.score == 0 and v.auto_score == 100
    assert "禁止事实被采信" in v.invalid_reason and v.vetoes == ["禁止事实被采信"]
    cap = policy_from_dict({"score": {"veto": {"forbidden_adopted": "cap", "cap": 40}}})
    v2 = conclude(mk_record(), {"F1": V("对"), "X1": V("采信")}, cap)
    assert v2.status == "fail" and v2.score == 40
    weight = policy_from_dict({"score": {"veto": {"forbidden_adopted": "weight"}, "pass_mark": 50}})
    v3 = conclude(mk_record(), {"F1": V("对"), "X1": V("采信")}, weight)
    assert v3.score == 50 and v3.status == "pass" and v3.vetoes == []   # 禁止事实按权重计：(100 + 0) / 2


def test_partial_and_custom_scores():
    v = conclude(mk_record(), {"F1": V("部分"), "X1": V("未采信")})
    assert v.status == "fail" and v.score == 50          # 部分 = 50 < 60
    v2 = conclude(mk_record(), {"F1": V("部分"), "X1": V("未采信")}, policy_from_dict({"score": {"pass_mark": 50}}))
    assert v2.status == "pass"
    v3 = conclude(mk_record(), {"F1": Verdict("部分", "human", "review", score=70), "X1": V("未采信")})
    assert v3.status == "pass" and v3.score == 70        # 显式分优先于快捷分值


def test_group_builtin_semantics():
    case = dict(mk_record()["case"], facts=[
        fact("F1", text="甲", group="G1"), fact("F2", text="乙", group="G1"),
        fact("X1", "forbidden", "禁止")])
    v = conclude(mk_record(case=case), {"F1": V("未提及"), "F2": V("对"), "X1": V("未采信")})
    assert v.status == "pass"                           # 组内满足其一
    v2 = conclude(mk_record(case=case), {"F1": V("未提及"), "F2": V("部分"), "X1": V("未采信")})
    assert v2.status == "fail"


def test_expected_counts_by_weight():
    case = dict(mk_record()["case"], facts=[fact("F1", text="甲"), fact("E1", "expected", "乙")])
    v = conclude(mk_record(case=case), {"F1": V("对"), "E1": V("未提及")})
    assert v.score == 50 and v.status == "fail"          # 期望事实按权重 1 计分
    case0 = dict(case, facts=[fact("F1", text="甲"), fact("E1", "expected", "乙", weight=0)])
    v0 = conclude(mk_record(case=case0), {"F1": V("对"), "E1": V("未提及")})
    assert v0.score == 100 and v0.status == "pass"       # 权重 0 = 只记录
    vu = conclude(mk_record(case=case), {"F1": V("对"), "E1": V(None)})
    assert vu.status == "undecided" and (vu.score_min, vu.score_max) == (50, 100) and vu.undecided_items == ["E1"]
    vu0 = conclude(mk_record(case=case0), {"F1": V("对"), "E1": V(None)})
    assert vu0.status == "pass" and vu0.undecided_items == []


# ---------------------------------------------------------------- 百分制样例（doc/10 §4.2，逐数复现）

def _ec_case():
    case = mk_record()["case"]
    case["preconditions"] = [{"id": "P1", "turn": 1, "text": "前提", "injections": []}]
    case["path"] = [{"type": "must", "action": {"kind": "load_skill", "skill": "trade-documents"}, "turn": 1}]
    case["facts"] = [fact("X1", "forbidden", "禁止"), fact("F1", text="甲", weight=2), fact("F2", text="乙"),
                     fact("E1", "expected", "丙", weight=0.5)]
    return case


def _ec_eff(**over):
    eff = {"P1": V("成立"), "path#1": V("满足", "rule"), "X1": V("未采信"),
           "F1": Verdict("对", "judge", "llm-fact@1", score=90), "F2": V("对", "rule"),
           "E1": Verdict("部分", "judge", "llm-fact@1", score=60)}
    eff.update(over)
    return eff


def test_doc_example_session_scores():
    rec = mk_record(case=_ec_case())
    v = conclude(rec, _ec_eff())
    assert v.score == 89 and v.status == "pass" and v.score_source == "auto"        # (180 + 100 + 30) / 3.5
    assert [(p["id"], p["weight"], p["score"]) for p in v.parts] == [("F1", 2.0, 90.0), ("F2", 1.0, 100.0),
                                                                     ("E1", 0.5, 60.0)]
    v2 = conclude(rec, _ec_eff(F1=Verdict("部分", "human", "review", score=30)))
    assert v2.score == 54 and v2.status == "fail"        # (60 + 100 + 30) / 3.5 = 54.3
    v3 = conclude(rec, _ec_eff(X1=V("采信")))
    assert v3.score == 0 and v3.status == "fail" and v3.auto_score == 89
    v4 = conclude(rec, _ec_eff(P1=V(None)))
    assert v4.status == "undecided" and v4.score is None and v4.score_min is None and v4.score_max == 89


def test_score_range_decides_when_one_side_of_pass_mark():
    rec = mk_record(case=_ec_case())
    v = conclude(rec, _ec_eff(E1=V(None)))               # 未判 E1：(180+100+0)/3.5=80 ~ (180+100+50)/3.5=94
    assert (v.score_min, v.score_max) == (80, 94) and v.status == "pass" and v.score is None
    v2 = conclude(rec, _ec_eff(F1=V(None), F2=V("未提及")))   # (0+0+30)/3.5=9 ~ (200+0+30)/3.5=66 → 跨及格线
    assert (v2.score_min, v2.score_max) == (9, 66) and v2.status == "undecided"


def test_human_total_overrides_auto_score():
    rec = mk_record(case=_ec_case())
    v = conclude(rec, _ec_eff(F1=Verdict("部分", "human", "review", score=30)), override={"score": 65, "reason": "r"})
    assert v.status == "pass" and v.score == 65 and v.score_source == "human" and v.auto_score == 54
    vi = conclude(rec, _ec_eff(P1=V("不成立")), override={"score": 65, "reason": "r"})
    assert vi.status == "invalid" and vi.score is None   # 前提实际不成立时人工会话分不生效


def test_rounding_is_half_up():
    case = dict(mk_record()["case"], facts=[fact("F1", text="甲"), fact("F2", text="乙")])
    v = conclude(mk_record(case=case), {"F1": Verdict("对", "human", "review", score=61),
                                        "F2": Verdict("对", "human", "review", score=60)})
    assert v.score == 61                                  # 60.5 → 61（不是银行家舍入的 60）


# ---------------------------------------------------------------- 未决处理（§7 规则 6）

def test_undecided_precond_blocks_conclusion():
    case = mk_record()["case"]
    case["preconditions"] = [{"id": "P1", "turn": 1, "text": "前提", "injections": []}]
    v = conclude(mk_record(case=case), {"P1": V(None), "F1": V("对"), "X1": V("未采信")})
    assert v.status == "undecided" and v.undecided_items == ["P1"]
    # 前提未决时，事实/禁止条目仍是参与条目（最有利一次会走到事实步），
    # 未决的一并列入清单，供复核队列定位
    v2 = conclude(mk_record(case=case), {"P1": V(None), "F1": V("对")})
    assert v2.status == "undecided" and v2.undecided_items == ["P1", "X1"]
    v3 = conclude(mk_record(case=case), {"P1": V(None), "F1": V("对"), "X1": V("未采信")},
                  policy_from_dict({"undecided": "fail"}))
    assert v3.status == "fail" and "未决" in v3.invalid_reason


def test_undecided_required_excludes_or_fails():
    v = conclude(mk_record(), {"F1": V(None), "X1": V("未采信")})
    assert v.status == "undecided" and v.undecided_items == ["F1"]
    v2 = conclude(mk_record(), {"F1": V(None), "X1": V("未采信")}, policy_from_dict({"undecided": "fail"}))
    assert v2.status == "fail"


def test_decided_fail_wins_over_undecided():
    v = conclude(mk_record(), {"F1": V("错"), "X1": V(None)})
    assert v.status == "fail" and v.undecided_items == ["X1"]


def test_undecided_forbidden_blocks_pass():
    v = conclude(mk_record(), {"F1": V("对"), "X1": V(None)})
    assert v.status == "undecided"                      # 无法确认未采信


def test_forced_path_na():
    case = mk_record()["case"]
    case["path"] = [{"type": "forbid", "action": {"kind": "load_skill", "skill": "s"}}]
    v = conclude(mk_record(case=case, mode="forced"), {"path#1": V("不适用"), "F1": V("对"), "X1": V("未采信")})
    assert v.status == "pass" and v.path == "n/a"


# ---------------------------------------------------------------- 缺陷修复（改造方案 §3，步骤 0 验收）

def VE(reason="算子异常：炸了"):
    """error 条目：value 为空但带算子来源。"""
    return Verdict(None, "judge", "op@1", reason=reason)


def test_group_member_contra_fails_session():
    """C1：组内一错一对 → 失败（08 §7 第 4 步：任一必要事实为"错"即失败）。"""
    case = dict(mk_record()["case"], facts=[
        fact("F1", text="甲", group="G1"), fact("F2", text="乙", group="G1"),
        fact("X1", "forbidden", "禁止")])
    v = conclude(mk_record(case=case), {"F1": V("错"), "F2": V("对"), "X1": V("未采信")})
    assert v.status == "fail" and "必要事实判错" in v.invalid_reason and v.score == 0
    # 禁止事实改按权重计也一样失败：required_wrong 是独立的否决项
    v2 = conclude(mk_record(case=case), {"F1": V("错"), "F2": V("对"), "X1": V("采信")},
                  policy_from_dict({"score": {"veto": {"forbidden_adopted": "weight"}}}))
    assert v2.status == "fail" and v2.score == 0


def test_group_sibling_hit_makes_member_undecided_decided():
    """C2：同组已有成员满足，另一成员未决改变不了结论 → 结论确定。"""
    case = dict(mk_record()["case"], facts=[
        fact("F1", text="甲", group="G1"), fact("F2", text="乙", group="G1"),
        fact("X1", "forbidden", "禁止")])
    v = conclude(mk_record(case=case), {"F1": V(None), "F2": V("对"), "X1": V("未采信")})
    assert v.status == "pass" and v.undecided_items == ["F1"]


def test_undecided_forbidden_decided_when_weighted():
    """禁止事实按权重计、及格线 50：X1 未决时区间 50–100 整体及格 → 结论确定。"""
    p = policy_from_dict({"score": {"veto": {"forbidden_adopted": "weight"}, "pass_mark": 50}})
    v = conclude(mk_record(), {"F1": V("对"), "X1": V(None)}, p)
    assert v.status == "pass" and v.undecided_items == ["X1"] and (v.score_min, v.score_max) == (50, 100)


def test_undecided_error_vs_undecided():
    """C3：异常条目只在可能改变结论时生效；结论可确定时异常不影响会话。"""
    # 异常条目在组内、另一成员已满足：两点求值均可确定 → pass
    case = dict(mk_record()["case"], facts=[
        fact("F1", text="甲", group="G1"), fact("F2", text="乙", group="G1"),
        fact("X1", "forbidden", "禁止")])
    v = conclude(mk_record(case=case), {"F1": VE(), "F2": V("对"), "X1": V("未采信")})
    assert v.status == "pass"
    # 单条事实异常：最有利"对"→pass、最不利"错"→fail，无法确定 → error（不进分母）
    v2 = conclude(mk_record(), {"F1": VE(), "X1": V("未采信")})
    assert v2.status == "error" and "F1" in v2.error
    # undecided: fail 时异常条目也不计为失败
    v3 = conclude(mk_record(), {"F1": VE(), "X1": V("未采信")}, policy_from_dict({"undecided": "fail"}))
    assert v3.status == "error"
    # 未决与异常同时存在且都影响结论：以 error 为准
    v4 = conclude(mk_record(), {"F1": VE(), "X1": V(None)})
    assert v4.status == "error" and v4.undecided_items == ["X1"]


# ---------------------------------------------------------------- 异常按 status 判（R10）

def test_error_judged_by_status_single_source():
    """机器结论为 error、来源字段为空：按条目 status 仍记异常（单一判据）。"""
    case = mk_record()["case"]
    record = mk_record(case=case)
    from scoring.items import build_context, extract_items
    ctx = build_context(record)
    items = extract_items(ctx)
    eff = {"F1": Verdict(None, "", ""), "X1": V("未采信")}
    statuses = {"F1": "error", "X1": "ok"}
    v = session_conclusion(record, ctx, items, eff, policy_from_dict({}), statuses=statuses)
    assert v.status == "error" and "F1" in v.error
    # 人工覆盖过的 error 条目 status 为 ok → 不再是异常，按未决处理
    statuses2 = {"F1": "ok", "X1": "ok"}
    v2 = session_conclusion(record, ctx, items, eff, policy_from_dict({}), statuses=statuses2)
    assert v2.status == "undecided" and v2.undecided_items == ["F1"]
