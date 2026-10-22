"""多 run 对比与锚点绊线（方案 §9.5、§12 步骤 5 验收）。"""
from scoring.chain import judge_episode, write_verdicts
from scoring.cli import load_bundle
from scoring.policy import policy_from_dict
from scoring.views import compare_runs
from tests.helpers import cfg_fact, fact, fake_config, make_run, mk_record

SNAP = {"skills_mode": "default-roots", "evalset": {"file": "e.jsonl", "sha256": "a"},
        "runner_profile": "p1", "eline_profile": {"method": "files", "sha256": "e1"},
        "skills_base": "s1", "agent": "eline rc.6", "models_observed": ["glm-5.3"]}


def anchor_records():
    """kr-1#0（锚点，2 次 trial）+ kr-2#0。"""
    case = {"id": "kr-1", "uid": "kr-1#0", "dim": "知识召回", "answerable": True,
            "facts": [fact("F1", text="尾号 400")], "preconditions": [], "path": []}
    case2 = {"id": "kr-2", "uid": "kr-2#0", "dim": "知识召回", "answerable": True,
             "facts": [fact("F1", text="尾号 500")], "preconditions": [], "path": []}
    return [
        dict(mk_record(uid="kr-1#0", trial=1, case=case), anchor=True),
        dict(mk_record(uid="kr-1#0", trial=2, case=case), anchor=True),
        mk_record(uid="kr-2#0", trial=1, case=case2),
    ]


def build_bundle(tmp_path, name, snapshot=None, cfg=None, records=None):
    run_dir = make_run(tmp_path, name, records or anchor_records(), snapshot=snapshot or dict(SNAP))
    cfg = cfg or cfg_fact("对")
    rows = []
    for r in (records or anchor_records()):
        rows.extend(judge_episode(r, cfg, name).rows)
    write_verdicts(run_dir, rows)
    return load_bundle(run_dir, policy_from_dict({}))


def test_intended_model_allows_profile_diff_and_tripwire(tmp_path):
    a = build_bundle(tmp_path, "a")
    snap_b = dict(SNAP, eline_profile={"method": "files", "sha256": "e2"})
    b = build_bundle(tmp_path, "b", snapshot=snap_b, cfg=fake_config())   # 默认假 judge：事实未提及 → 全败
    rep = compare_runs(a, b, intended="model")
    assert rep["blocked"] is None
    assert rep["allowed_diff_keys"] == ["eline_profile"]
    assert rep["anchor_tripwires"] == ["kr-1#0"]          # 上 run 锚点全过、本 run 全败（trials≥2）
    assert rep["paired"]["kr-1#0"]["delta"] == -1.0
    assert rep["paired"]["kr-2#0"]["significant"] is True


def test_intended_model_rejects_other_key(tmp_path):
    a = build_bundle(tmp_path, "a")
    b = build_bundle(tmp_path, "b", snapshot=dict(SNAP, skills_base="s2"), cfg=fake_config())
    rep = compare_runs(a, b, intended="model")
    assert rep["blocked"] and "skills_base" in rep["blocked"]


def test_intended_skill_allows_skills_base_diff(tmp_path):
    a = build_bundle(tmp_path, "a")
    b = build_bundle(tmp_path, "b", snapshot=dict(SNAP, skills_base="s2"), cfg=cfg_fact("对"))
    rep = compare_runs(a, b, intended="skill")            # 知识修订后的验证对比（01 §8 闭环第 4 条）
    assert rep["blocked"] is None
    assert rep["anchor_tripwires"] == []                  # 两 run 都通过


def test_skills_mode_diff_blocks(tmp_path):
    a = build_bundle(tmp_path, "a")
    b = build_bundle(tmp_path, "b", snapshot=dict(SNAP, skills_mode="isolated"), cfg=cfg_fact("对"))
    assert "skills_mode" in compare_runs(a, b, intended="model")["blocked"]


def test_operator_version_diff_blocks(tmp_path):
    a = build_bundle(tmp_path, "a")
    b = build_bundle(tmp_path, "b", cfg=cfg_fact("对"))
    b.rows[0]["machine"]["operator"] = "other-op@9"
    rep = compare_runs(a, b, intended="model")
    assert rep["blocked"] and "算子版本" in rep["blocked"]


DIFF_DEMOTE = """# evalset v1 → v2 变更

## 新增（0）

## 退役（0）

## 因降级移出（1）
- kr-2#0（收汇保单尾号/反转 rev 1→2，需复核）

## 内容修订（0）

## 注入 rev 变化（0）
"""

DIFF_REVISE = DIFF_DEMOTE.replace("## 内容修订（0）", "## 内容修订（1）\n- kr-9#0").replace(
    "## 因降级移出（1）", "## 因降级移出（0）").replace("- kr-2#0（收汇保单尾号/反转 rev 1→2，需复核）", "")


def test_evalset_diff_alignment(tmp_path):
    a = build_bundle(tmp_path, "a")
    snap_b = dict(SNAP, evalset={"file": "e2.jsonl", "sha256": "b"})
    b = build_bundle(tmp_path, "b", snapshot=snap_b, cfg=fake_config())
    # 未提供 diff.md → 拒绝
    assert "evalset" in compare_runs(a, b, intended="model")["blocked"]
    # diff 只有因降级移出 → 对齐分母放行，且 demoted 不进配对
    diff = tmp_path / "diff.md"
    diff.write_text(DIFF_DEMOTE, encoding="utf-8")
    rep = compare_runs(a, b, intended="model", diff_md=str(diff))
    assert rep["blocked"] is None
    assert rep["demoted_excluded"] == ["kr-2#0"]
    assert "kr-2#0" not in rep["paired"]
    # diff 含内容修订 → 拒绝（题变了不该对比）
    diff2 = tmp_path / "diff2.md"
    diff2.write_text(DIFF_REVISE, encoding="utf-8")
    assert "内容修订" in compare_runs(a, b, intended="model", diff_md=str(diff2))["blocked"]


def test_compare_needs_verdicts(tmp_path):
    from scoring.views import RunBundle
    a = build_bundle(tmp_path, "a")
    empty = RunBundle(run_id="b", run_dir="x", records=[], manifest={"snapshot": SNAP},
                      rows=[], reviews=[], verdicts=[])
    assert "算子版本" in compare_runs(a, empty, intended="model")["blocked"]
