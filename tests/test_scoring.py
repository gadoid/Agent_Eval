"""scoring v2 测试：前提有效性、F/E/X 四级、组、路径约束、归因、Wilson、对比与绊线。"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scoring"))

import scoring as sc  # noqa: E402
from scoring import Judges, JudgesMeta, score_episode, score_run, compare_runs, wilson  # noqa: E402


def mk_judges(fact=None, forbidden=None, refusal=None, precond=None):
    return Judges(
        fact=fact or (lambda t, a: sc.HIT if t in a else sc.MISSING),
        forbidden=forbidden or (lambda t, a: t in a),
        refusal=refusal or (lambda a: "不知道" in a),
        precond=precond or (lambda t, a: True),
    )


def record(**over):
    base = {
        "schema": 3, "uid": "ec-1#0", "mode": "natural", "trial": 1, "status": "ok",
        "failed_turn": None, "evalset_version": "v1",
        "turns": [
            {"index": 1, "source": "live", "input": "q1", "output": "答：规则是收齐尾款后电放。", "status": "ok",
             "observations": {"skills": [{"name": "trade-documents", "ok": True}], "tools": [], "tool_errors": [],
                              "usage": {"steps": 1, "tokens": {}}, "ask_user": {"count": 0, "auto": False}, "model": "glm-5.3"}},
        ],
        "case": {
            "id": "ec-1", "uid": "ec-1#0", "dim": "错误知识纠正", "answerable": True,
            "facts": [
                {"id": "F1", "kind": "required", "text": "未收尾款不应电放", "keywords": [], "turn": None, "group": None, "evidence": None},
                {"id": "X1", "kind": "forbidden", "text": "未收尾款也可以先电放", "keywords": [], "turn": None, "group": None, "evidence": "R1"},
            ],
            "preconditions": [], "path": [],
        },
    }
    base.update(over)
    return base


# ---------------------------------------------------------------- 前提与事实

def test_precondition_invalid_not_scored_as_fail():
    case = record()["case"]
    case["preconditions"] = [{"id": "P1", "turn": 1, "text": "答复为可以电放", "injections": []}]
    rec = record(case=case)
    j = mk_judges(precond=lambda t, a: False)
    v = score_episode(rec, j)
    assert v.status == "invalid" and "前提不成立" in (v.invalid_reason or "")


def test_fact_levels_and_veto():
    j = mk_judges(fact=lambda t, a: sc.HIT)
    assert score_episode(record(), j).status == "pass"
    # X 被采信 → 一票否决
    j = mk_judges(fact=lambda t, a: sc.HIT, forbidden=lambda t, a: True)
    v = score_episode(record(), j)
    assert v.status == "fail" and v.vetoed
    # F 判"错" → fail
    j = mk_judges(fact=lambda t, a: sc.CONTRA)
    v = score_episode(record(), j)
    assert v.status == "fail" and sc.CONTRA in v.facts.values()


def test_keyword_rule_with_negation_window():
    rec = record(case=dict(record()["case"], facts=[
        {"id": "F1", "kind": "required", "text": "提单", "keywords": ["正本提单"], "turn": None, "group": None, "evidence": None}]))
    rec["turns"][0]["output"] = "需要正本提单"
    assert score_episode(rec, mk_judges()).facts["F1"] == sc.HIT          # 规则直接判对
    rec["turns"][0]["output"] = "并非正本提单，副本即可"
    v = score_episode(rec, mk_judges(fact=lambda t, a: sc.MISSING))
    assert v.facts["F1"] == sc.MISSING                                    # 否定语境不认，交 judge


def test_group_satisfied_by_any_member():
    case = record()["case"]
    case["facts"] = [
        {"id": "F1", "kind": "required", "text": "甲说法", "keywords": [], "turn": None, "group": "G1", "evidence": None},
        {"id": "F2", "kind": "required", "text": "乙说法", "keywords": [], "turn": None, "group": "G1", "evidence": None},
    ]
    j = mk_judges(fact=lambda t, a: sc.HIT if t == "乙说法" else sc.MISSING)
    v = score_episode(record(case=case), j)
    assert v.status == "pass"                                              # 同组满足其一


def test_answerable_false_refusal():
    case = record()["case"]
    case["answerable"] = False
    v = score_episode(record(case=case, turns=[dict(record()["turns"][0], output="知识库中没有相关记录，不知道")]), mk_judges())
    assert v.status == "pass"
    v = score_episode(record(case=case), mk_judges())
    assert v.status == "fail"


# ---------------------------------------------------------------- 路径约束（CASE-STRUCTURE §2.10）

def rec_with_skills(skills_per_turn, case=None):
    rec = record(case=case) if case is not None else record()
    rec["turns"] = [
        {"index": i + 1, "source": "live", "input": "q", "output": "答", "status": "ok",
         "observations": {"skills": [{"name": s, "ok": True} for s in ss], "tools": [], "tool_errors": [],
                          "usage": {"steps": 1, "tokens": {}}, "ask_user": {"count": 0, "auto": False}, "model": None}}
        for i, ss in enumerate(skills_per_turn)
    ]
    return rec


def test_path_must_loaded_by_turn_n_and_forbid():
    case = record()["case"]
    case["path"] = [{"type": "must", "action": {"kind": "load_skill", "skill": "trade-documents"}, "turn": 1}]
    assert score_episode(rec_with_skills([["trade-documents"], []], case), mk_judges(), {}).path == "ok"
    # 第 1 轮没加载、第 2 轮才加载：截至第 1 轮未加载 → 违反
    v = score_episode(rec_with_skills([[], ["trade-documents"]], case), mk_judges(), {})
    assert v.path == "violate" and v.status == "fail"
    # 未写轮次 = 截至最后一轮已加载过即可
    case["path"] = [{"type": "must", "action": {"kind": "load_skill", "skill": "trade-documents"}}]
    assert score_episode(rec_with_skills([[], ["trade-documents"]], case), mk_judges(), {}).path == "ok"
    # 禁止：任何一轮加载即违反
    case["path"] = [{"type": "forbid", "action": {"kind": "load_skill", "skill": "trade-finance"}}]
    assert score_episode(rec_with_skills([[], ["trade-finance"]], case), mk_judges(), {}).path == "violate"
    # forced 不判定
    forced = rec_with_skills([[]], case)
    forced["mode"] = "forced"
    assert score_episode(forced, mk_judges(), {}).path == "n/a"


# ---------------------------------------------------------------- 归因与聚合

def test_attribution_three_way():
    mk = lambda loaded, other, status: type("V", (), {})()
    verdicts = []
    # 未加载 / 误加载 / 加载但答错 / 加载且通过
    for tgt, other, st in [(False, False, "fail"), (False, True, "fail"), (True, False, "fail"), (True, False, "pass")]:
        verdicts.append(type("V", (), {"mode": "natural", "status": st, "target_loaded": tgt, "other_loaded": other,
                                       "uid": "u", "trial": 1, "by_rule": 0, "ask_user": 0})())
    at = sc.attribution_view(verdicts)
    assert at["n"] == 4 and at["not_loaded"] == 0.25 and at["mis_loaded"] == 0.25
    assert at["loaded_wrong"] == 0.25


def test_wilson_and_run_aggregation():
    assert wilson(0, 0) == (0.0, 0.0)
    lo, hi = wilson(8, 10)
    assert 0.4 < lo < 0.8 and 0.8 < hi <= 1.0
    records = [record(mode="forced"), record(mode="forced"), record(mode="forced", status="error", failed_turn=1)]
    report = score_run(records, mk_judges(), None, JudgesMeta(model="test-judge"))
    assert report["judgment_snapshot"]["rule_version"] == sc.RULE_VERSION
    cell = report["views"]["ability"]["错误知识纠正|forced"]
    assert cell["n"] == 2 and cell["headline"] is True
    assert report["views"]["ability"]["错误知识纠正|forced"]["error"] == 1


# ---------------------------------------------------------------- 对比与绊线

def _mk_run(tmp: Path, name: str, rows: list[dict], snapshot: dict, anchors_pass: bool):
    d = tmp / name
    d.mkdir(parents=True)
    (d / "results.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    (d / "run.json").write_text(json.dumps({"snapshot": snapshot, "profile": {"content": {"routing": {}}}}, ensure_ascii=False), encoding="utf-8")
    return str(d)


def test_compare_and_tripwire(tmp_path):
    snap = {"skills_mode": "isolated", "skills_base": "x", "evalset": {"sha256": "a"}}
    def rows(anchor_pass: bool):
        out = []
        for uid in ["kr-1#0", "kr-2#0"]:
            for t in (1, 2):
                out.append({"uid": uid, "mode": "natural", "trial": t, "status": "ok" if (uid != "kr-1#0" or anchor_pass) else "error", "anchor": uid == "kr-1#0"})
        return out
    a = _mk_run(tmp_path, "a", rows(True), snap, True)
    b = _mk_run(tmp_path, "b", rows(False), snap, False)
    rep = compare_runs(a, b)
    assert rep["blocked"] is None
    assert rep["anchor_tripwires"] == ["kr-1#0"]          # 上 run 全过、本 run 全败，trials≥2
    assert "kr-1#0" in rep["paired"]
    # skills_mode 不同 → 拒绝比较
    snap2 = dict(snap, skills_mode="default-roots")
    c = _mk_run(tmp_path, "c", rows(True), snap2, True)
    assert compare_runs(a, c)["blocked"] is not None
