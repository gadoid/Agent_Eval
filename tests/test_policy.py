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
    v = conclude(mk_record(case=case), {"P1": V(None), "F1": V("对")})
    assert v.status == "undecided" and v.undecided_items == ["P1"]
    v2 = conclude(mk_record(case=case), {"P1": V(None), "F1": V("对")}, policy_from_dict({"undecided": "fail"}))
    assert v2.status == "fail" and "未决" in v2.invalid_reason


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
