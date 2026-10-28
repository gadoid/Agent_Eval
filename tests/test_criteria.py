"""判据表与推导（doc/16 §4）：结论由判据结果按规则推出，穷举 §4.2 推导表每一行。

CRITERIA 是判定契约（与 SCALES 并列、随代码版本走）；P1 的模型算子/复核导读只产出
判据结果，结论一律由 verdict_from_criteria 得出——本测试就是那份推导规则的唯一权威副本。
"""
from itertools import product

from scoring.items import CRITERIA, CRITERIA_VER, verdict_from_criteria

SAT, UNSAT, UNDEC = "满足", "不满足", "无法判断"


def crit(c1=SAT, c2=SAT, c3=SAT, contra=False):
    """构造判据结果；客观 C1 的「不满足」带 contra 子标记（doc/16 §5.2）。"""
    out = {}
    for cid, r in (("C1", c1), ("C2", c2), ("C3", c3)):
        out[cid] = {"result": r}
    out["C1"]["contra"] = contra
    return out


# ------------------------------------------------- 必要/期望 · 客观（§4.2 第一行）

def test_objective_contra_distinguishes_wrong_from_missing():
    """C1 不满足时：给了矛盾值（contra）→ 错；未涉及 → 未提及。"""
    for kind in ("required", "expected"):
        assert verdict_from_criteria(kind, "objective", crit(c1=UNSAT, contra=True)) == "错"
        assert verdict_from_criteria(kind, "objective", crit(c1=UNSAT, contra=False)) == "未提及"


def test_objective_exhaustive_all_combinations():
    """穷举 (C1 × contra × C2 × C3) 16 种组合，按推导表从上往下命中即止。"""
    for kind in ("required", "expected"):
        for c1, contra, c2, c3 in product((SAT, UNSAT), (True, False), (SAT, UNSAT), (SAT, UNSAT)):
            v = verdict_from_criteria(kind, "objective", crit(c1=c1, c2=c2, c3=c3, contra=contra))
            if c1 == UNSAT:
                assert v == ("错" if contra else "未提及")
            elif UNSAT in (c2, c3):
                assert v == "部分"
            else:
                assert v == "对"


# ------------------------------------------------- 必要/期望 · 主观（§4.2 第二行）

def test_subjective_exhaustive_all_combinations():
    """穷举 (C1 × C2 × C3) 8 种组合：C3 → 错 优先于 C1 → 未提及 优先于 C2 → 部分。"""
    for kind in ("required", "expected"):
        for c1, c2, c3 in product((SAT, UNSAT), repeat=3):
            v = verdict_from_criteria(kind, "subjective", crit(c1=c1, c2=c2, c3=c3))
            if c3 == UNSAT:
                assert v == "错"
            elif c1 == UNSAT:
                assert v == "未提及"
            elif c2 == UNSAT:
                assert v == "部分"
            else:
                assert v == "对"


# ------------------------------------------------- 禁止（§4.2 第三行，两种性质）

def test_forbidden_adopted_only_when_both_satisfied():
    for nature in ("objective", "subjective", None):    # None = 未标（客观缺省）
        assert verdict_from_criteria("forbidden", nature, crit()) == "采信"
        assert verdict_from_criteria("forbidden", nature, crit(c1=UNSAT)) == "未采信"
        assert verdict_from_criteria("forbidden", nature, crit(c2=UNSAT)) == "未采信"


# ------------------------------------------------- 无法判断 → undecided，送人工

def test_any_undecidable_short_circuits():
    for (kind, nature), cs in CRITERIA.items():
        for c in cs:
            criteria = {x["id"]: {"result": SAT} for x in cs}
            criteria[c["id"]] = {"result": UNDEC}
            assert verdict_from_criteria(kind, nature, criteria) == "undecided"


def test_missing_or_illegal_result_counts_as_undecidable():
    assert verdict_from_criteria("required", "objective", {}) == "undecided"
    assert verdict_from_criteria("forbidden", "objective",
                                 {"C1": {"result": "对"}, "C2": {"result": SAT}}) == "undecided"


# ------------------------------------------------- 判据表结构（契约本身）

def test_criteria_table_shape_and_version():
    assert CRITERIA_VER == "criteria@1"
    assert set(CRITERIA) == {(k, n) for k in ("required", "expected", "forbidden")
                             for n in ("objective", "subjective")}
    for (kind, nature), cs in CRITERIA.items():
        assert [c["id"] for c in cs] == [f"C{i}" for i in range(1, len(cs) + 1)]
        assert all({"id", "name", "rule"} <= set(c) for c in cs)
    # 必要/期望同性质共用同一组判据；禁止只有 C1/C2
    assert CRITERIA[("required", "objective")] == CRITERIA[("expected", "objective")]
    assert CRITERIA[("required", "subjective")] == CRITERIA[("expected", "subjective")]
    assert len(CRITERIA[("forbidden", "objective")]) == 2
    assert len(CRITERIA[("forbidden", "subjective")]) == 2
