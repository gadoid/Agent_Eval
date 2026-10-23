"""口径层（方案 §7、§12 步骤 2 验收）：固定参数、会话结论判定顺序、未决处理。"""
import pytest

from scoring.items import build_context, extract_items
from scoring.operators import Verdict
from scoring.policy import Policy, policy_from_dict, session_conclusion
from tests.helpers import fact, mk_record


def V(value, source="judge"):
    return Verdict(value, source, "x@1") if value is not None else Verdict(None, "", "")


def conclude(record, eff_over, policy=None):
    policy = policy or policy_from_dict({})
    ctx = build_context(record)
    items = extract_items(ctx)
    eff = {i.id: eff_over.get(i.id, V(None)) for i in items}
    return session_conclusion(record, ctx, items, eff, policy)


def test_policy_validation():
    with pytest.raises(Exception):
        policy_from_dict({"partial_as": "maybe"})
    with pytest.raises(Exception):
        policy_from_dict({"undecided": "ignore"})
    with pytest.raises(Exception):
        policy_from_dict({"headline_mode": {}})          # 缺 default
    with pytest.raises(Exception):
        policy_from_dict({"mystery": 1})                 # 未知参数
    p = policy_from_dict({"partial_as": "pass", "veto_on_forbidden": False,
                          "undecided": "fail", "headline_mode": {"default": "natural"}})
    assert p.partial_as == "pass" and p.veto_on_forbidden is False and p.undecided == "fail"


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


def test_forbidden_veto_and_switch():
    v = conclude(mk_record(), {"F1": V("对"), "X1": V("采信")})
    assert v.status == "fail" and "禁止事实被采信" in v.invalid_reason
    no_veto = policy_from_dict({"veto_on_forbidden": False})
    v2 = conclude(mk_record(), {"F1": V("对"), "X1": V("采信")}, no_veto)
    assert v2.status == "pass"                          # 关掉否决后不影响通过


def test_partial_as_pass_or_fail():
    v = conclude(mk_record(), {"F1": V("部分"), "X1": V("未采信")})
    assert v.status == "fail"                           # 默认：部分不算满足
    v2 = conclude(mk_record(), {"F1": V("部分"), "X1": V("未采信")}, policy_from_dict({"partial_as": "pass"}))
    assert v2.status == "pass"


def test_group_builtin_semantics():
    case = dict(mk_record()["case"], facts=[
        fact("F1", text="甲", group="G1"), fact("F2", text="乙", group="G1"),
        fact("X1", "forbidden", "禁止")])
    v = conclude(mk_record(case=case), {"F1": V("未提及"), "F2": V("对"), "X1": V("未采信")})
    assert v.status == "pass"                           # 组内满足其一
    v2 = conclude(mk_record(case=case), {"F1": V("未提及"), "F2": V("部分"), "X1": V("未采信")})
    assert v2.status == "fail"


def test_expected_never_affects_conclusion():
    case = dict(mk_record()["case"], facts=[fact("F1", text="甲"), fact("E1", "expected", "乙")])
    v = conclude(mk_record(case=case), {"F1": V("对"), "E1": V(None)})
    assert v.status == "pass" and v.expected_hit == {}


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
    assert v.status == "fail" and "必要事实判错" in v.invalid_reason
    # 关掉禁止否决也一样失败：veto_on_forbidden 只管禁止事实
    v2 = conclude(mk_record(case=case), {"F1": V("错"), "F2": V("对"), "X1": V("采信")},
                  policy_from_dict({"veto_on_forbidden": False}))
    assert v2.status == "fail"


def test_group_sibling_hit_makes_member_undecided_decided():
    """C2：同组已有成员满足，另一成员未决改变不了结论 → 结论确定。"""
    case = dict(mk_record()["case"], facts=[
        fact("F1", text="甲", group="G1"), fact("F2", text="乙", group="G1"),
        fact("X1", "forbidden", "禁止")])
    v = conclude(mk_record(case=case), {"F1": V(None), "F2": V("对"), "X1": V("未采信")})
    assert v.status == "pass" and v.undecided_items == ["F1"]


def test_undecided_forbidden_decided_when_veto_off():
    """C2：关闭否决时，禁止事实未决改变不了结论 → 结论确定。"""
    no_veto = policy_from_dict({"veto_on_forbidden": False})
    v = conclude(mk_record(), {"F1": V("对"), "X1": V(None)}, no_veto)
    assert v.status == "pass" and v.undecided_items == ["X1"]


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
