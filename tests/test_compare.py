"""比较轴与锚点绊线（改造方案 §7、整改清单 R3 验收）。"""
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
        "current_standard_fp": "f1", "active": 1, "c_status": status, "c_reason": "", "detail_json": "{}",
    }


def test_common_multi_profile_case_passes_without_flags():
    """R3 验收：两个 run、两个 profile、各 2 trial、同评测集 → --axis profile 零参数放行。"""
    rows = [
        row(profile="p1", run="r1", trial=1), row(profile="p1", run="r1", trial=2),
        row(profile="p2", run="r2", trial=1, status="fail", models='["glm-4"]'),
        row(profile="p2", run="r2", trial=2, status="fail", models='["glm-4"]'),
    ]
    rep = compare_axis(rows, axis="subject")
    assert rep["blocked"] is None
    assert rep["axis_values"] == ["p1", "p2"]
    assert rep["matrix"]["q1"]["p1"] == {"pass": 2, "n": 2, "pending": 0, "rate": 1.0}
    assert rep["matrix"]["q1"]["p2"]["rate"] == 0.0
    assert rep["dims"]["知识召回"]["p1"]["n"] == 2


def test_mode_inconsistent_still_blocks():
    rows = [row(mode="natural"), row(mode="forced")]
    rep = compare_axis(rows, axis="subject")
    assert rep["blocked"] and "mode" in rep["blocked"]
    ok = compare_axis(rows, axis="subject", fixes={"mode": "natural"})
    assert ok["blocked"] is None
    # skills_base：subject 轴下它是被测指纹的拆字段（随之放行，doc/15 §3.6）；
    # 作 model 拆字段轴时才是处理条件 → 不一致拦截
    sb = [row(), row(skills_base="s2")]
    assert compare_axis(sb, axis="subject")["blocked"] is None
    assert "skills_base" in compare_axis(sb, axis="model")["blocked"]


def test_trial_and_run_are_sampling_attrs():
    """R3：trial / run 是重复采样标识，默认不限，无需声明。"""
    rows = [row(trial=1), row(trial=2)]
    assert compare_axis(rows, axis="subject")["blocked"] is None
    # run 也可直接作比较轴，跨 run 的 trial 差异不拦截
    runs = [row(run="a", trial=1), row(run="b", trial=2)]
    rep = compare_axis(runs, axis="run")
    assert rep["blocked"] is None and rep["axis_values"] == ["a", "b"]


def test_profile_axis_allows_model_with_drift_warning():
    """R3：profile 与 model 互为派生——作轴的派生属性自动放行；同 profile 多模型给漂移提示。"""
    rows = [row(profile="p1", models='["glm-5.3"]', run="r1"),
            row(profile="p1", models='["glm-4"]', run="r2")]
    rep = compare_axis(rows, axis="subject")
    assert rep["blocked"] is None
    assert rep["warnings"] and "p1" in rep["warnings"][0] and "glm-4" in rep["warnings"][0]
    # 干净的派生关系：每个 profile 一个模型 → 无警告
    clean = [row(profile="p1", models='["glm-5.3"]'), row(profile="p2", models='["glm-4"]')]
    assert compare_axis(clean, axis="subject")["warnings"] == []


def test_evalset_version_any_lists_question_counts():
    rows = [row(evalset="v1", run="a"), row(evalset="v2", run="b"),
            row(qkey="q2", uid="kr-2#0", evalset="v1", run="b")]
    rep = compare_axis(rows, axis="run", anys=("evalset_version",))
    assert rep["blocked"] is None
    # 各版本的题目数（进入交集的题：q2 只在 run b → 被排除）
    assert rep["evalset_versions"] == {"v1": 1, "v2": 1}
    # 未声明不限时跨版本拦截
    assert "evalset_version" in compare_axis(rows, axis="run")["blocked"]


def test_skills_mode_never_comparable():
    rows = [row(run="a", skills_mode="default-roots"), row(run="b", skills_mode="isolated")]
    assert compare_axis(rows, axis="skills_mode")["blocked"]
    assert compare_axis(rows, axis="run", anys=("skills_mode",))["blocked"]


def test_pending_answers_excluded():
    rows = [row(profile="p1"), row(profile="p2", status=None)]
    rep = compare_axis(rows, axis="subject")
    assert rep["pending_answers"] == 1
    assert rep["matrix"]["q1"]["p2"] == {"pass": 0, "n": 0, "pending": 1, "rate": None}


def test_question_intersection_excludes_single_sided():
    """§7.2 题目交集：只在单侧有答案的题被排除（取代 diff.md 因降级移出对齐）。"""
    rows = [
        row(qkey="q1", run="a", status="pass"), row(qkey="q1", run="b", status="fail"),
        row(qkey="q2", uid="kr-2#0", run="a", status="pass"),           # b 里没有（降级移出）
        row(qkey="q3", uid="kr-3#0", run="a", status="pass"),           # 输入改版 → qkey 不同
        row(qkey="q3", uid="kr-3#0", run="b", status="pass"),
    ]
    rep = compare_axis(rows, axis="run")
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
    rep = compare_axis(rows, axis="run", run_order={"a": 0, "b": 1})
    assert rep["anchor_tripwires"] == [{"qkey": "q1", "from_run": "a", "to_run": "b"}]
    rows2 = rows[:1] + [row(qkey="q1", run="b", status="fail", anchor=True)]
    rep2 = compare_axis(rows2, axis="run", run_order={"a": 0, "b": 1})
    assert rep2["anchor_tripwires"] == []


def test_unknown_axis_raises():
    with pytest.raises(ValueError):
        compare_axis([row()], axis="nope")
