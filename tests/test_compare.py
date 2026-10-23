"""比较轴与锚点绊线（改造方案 §7、§12 步骤 4 验收）。"""
import json

import pytest

from scoring.views import compare_axis


def row(qkey="q1", uid="kr-1#0", case="kr-1", vi=0, rewrite=None, dim="知识召回", mode="natural",
        trial=1, profile="p1", models='["glm-5.3"]', skills_mode="default-roots", skills_base="s1",
        evalset="v1", run="a", status="pass", answer=None, anchor=False, topic="t1"):
    return {
        "answer_id": answer or f"{run}/{uid}-{mode}-t{trial}", "qkey": qkey, "run_id": run,
        "episode_id": f"{uid}-{mode}-t{trial}", "judge_fp_at_run": "f1", "profile_id": profile,
        "models_json": models, "mode": mode, "trial": trial, "skills_mode": skills_mode,
        "skills_base": skills_base, "evalset_version": evalset, "run_status": "ok",
        "record_json": json.dumps({"anchor": anchor}), "uid": uid, "case_id": case,
        "variant_index": vi, "rewrite": rewrite, "dim": dim, "topic": topic,
        "current_judge_fp": "f1", "active": 1, "c_status": status, "c_reason": "", "detail_json": "{}",
    }


def test_axis_profile_matrix_and_dims():
    rows = [
        row(qkey="q1", profile="p1", status="pass"), row(qkey="q1", profile="p1", trial=2, status="fail"),
        row(qkey="q1", profile="p2", status="pass"),
        row(qkey="q2", uid="kr-2#0", profile="p1", status="fail"),
        row(qkey="q2", uid="kr-2#0", profile="p2", status="pass"),
    ]
    rep = compare_axis(rows, axis="profile", anys=("trial",))
    assert rep["blocked"] is None
    assert rep["axis_values"] == ["p1", "p2"]
    assert rep["matrix"]["q1"]["p1"] == {"pass": 1, "n": 2, "pending": 0, "rate": 0.5}
    assert rep["matrix"]["q1"]["p2"]["rate"] == 1.0
    assert rep["dims"]["知识召回"]["p1"]["pass"] == 1 and rep["dims"]["知识召回"]["p1"]["n"] == 3


def test_unfixed_inconsistent_attribute_blocks():
    rows = [row(mode="natural"), row(mode="forced")]
    rep = compare_axis(rows, axis="profile")
    assert rep["blocked"] and "mode" in rep["blocked"]
    # 固定后放行
    ok = compare_axis(rows, axis="profile", fixes={"mode": "natural"})
    assert ok["blocked"] is None
    # trial 不一致：不限则拦、声明不限则放行
    t = [row(trial=1), row(trial=2)]
    assert "trial" in compare_axis(t, axis="profile")["blocked"]
    assert compare_axis(t, axis="profile", anys=("trial",))["blocked"] is None


def test_skills_mode_never_comparable():
    rows = [row(run="a", skills_mode="default-roots"), row(run="b", skills_mode="isolated")]
    assert compare_axis(rows, axis="skills_mode")["blocked"]
    assert compare_axis(rows, axis="run", anys=("skills_mode",))["blocked"]


def test_pending_answers_excluded():
    rows = [row(profile="p1"), row(profile="p2", status=None)]
    rep = compare_axis(rows, axis="profile")
    assert rep["pending_answers"] == 1
    assert rep["matrix"]["q1"]["p2"] == {"pass": 0, "n": 0, "pending": 1, "rate": None}


def test_question_intersection_excludes_single_sided():
    """§7.2 题目交集：只在单侧有判定的题被排除（取代 diff.md 因降级移出对齐）。"""
    rows = [
        row(qkey="q1", run="a", status="pass"), row(qkey="q1", run="b", status="fail"),
        row(qkey="q2", uid="kr-2#0", run="a", status="pass"),           # b 里没有（降级移出）
        row(qkey="q3", uid="kr-3#0", run="a", status="pass"),           # 输入改版 → qkey 不同
        row(qkey="q3", uid="kr-3#0", run="b", status="pass"),
    ]
    rep = compare_axis(rows, axis="run", anys=("trial",))
    assert rep["blocked"] is None
    assert set(rep["matrix"]) == {"q1", "q3"}
    assert [e["qkey"] for e in rep["excluded_qkeys"]] == ["q2"]


def test_anchor_tripwire_by_qkey():
    rows = [
        row(qkey="q1", run="a", trial=1, status="pass", anchor=True),
        row(qkey="q1", run="a", trial=2, status="pass", anchor=True),
        row(qkey="q1", run="b", trial=1, status="fail", anchor=True),
        row(qkey="q1", run="b", trial=2, status="fail", anchor=True),
    ]
    rep = compare_axis(rows, axis="run", anys=("trial",), run_order={"a": 0, "b": 1})
    assert rep["anchor_tripwires"] == [{"qkey": "q1", "from_run": "a", "to_run": "b"}]
    # trials 不足 2 → 不触发
    rows2 = rows[:1] + [row(qkey="q1", run="b", status="fail", anchor=True)]
    rep2 = compare_axis(rows2, axis="run", anys=("trial",), run_order={"a": 0, "b": 1})
    assert rep2["anchor_tripwires"] == []


def test_unknown_axis_raises():
    with pytest.raises(ValueError):
        compare_axis([row()], axis="nope")
