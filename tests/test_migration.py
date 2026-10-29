"""P1 场景测试（doc/15 §7.1/§7.2 中与 v9 共存的部分 + doc/17 §3/§5 落库行为）。

v8 迁移链与 migrate-verify 已随 policy 删除（doc/17 决定 15：v9 从空库起步，
旧库拒绝打开——见 test_store.py）。本文件保留与迁移无关的场景：逐题偏离、
跨快照同标准复用、operator_score、被测指纹；补计分配置导入落库的验收。"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scoring.items import qkey_of  # noqa: E402
from scoring.store import Store  # noqa: E402
from tests.helpers import mk_record  # noqa: E402


def test_set_current_question_off_snapshot(tmp_path):
    """specs set-current --question：逐题偏离（off_snapshot=1），snapshot use 恢复。"""
    db = str(tmp_path / "s.db")
    store = Store(db)
    case = dict(mk_record()["case"])
    store.import_evalset([case], "v1")
    case2 = json.loads(json.dumps(case))
    case2["facts"][0]["text"] = "改写后的事实文本"
    store.import_evalset([case2], "v2")
    q = store.questions()[0]
    fp_v1 = store.conn.execute(
        "SELECT standard_fp FROM spec_versions WHERE qkey=? AND evalset_version='v1'",
        (q["qkey"],)).fetchone()["standard_fp"]
    assert store.current_snapshot() == "v2"
    store.set_current("v1", qkey=q["qkey"])
    q2 = store.question(q["qkey"])
    assert q2["off_snapshot"] == 1 and q2["current_standard_fp"] == fp_v1
    store.snapshot_use("v2")
    q3 = store.question(q["qkey"])
    assert q3["off_snapshot"] == 0 and q3["current_standard_fp"] != fp_v1
    store.close()


def test_cross_snapshot_same_standard_reuses_conclusions(tmp_path):
    """场景 #15：题面与标准都没变的题，跨快照复用会话与结论。"""
    db = str(tmp_path / "x.db")
    store = Store(db)
    case = dict(mk_record()["case"])
    store.import_evalset([case], "v1")
    other = json.loads(json.dumps(case))
    other["uid"] = "ec-2#0"; other["case_id"] = "ec-2"; other["id"] = "ec-2"
    store.import_evalset([case, other], "v2")     # ec-1 原样 + 新题 ec-2
    fps = [r["standard_fp"] for r in store.conn.execute(
        "SELECT standard_fp FROM spec_versions WHERE qkey=? ORDER BY evalset_version",
        (qkey_of(case),))]
    assert fps[0] == fps[1]                        # 同一标准跨快照
    store.close()


def test_operator_score_ignore():
    """场景 #12：operator_score=ignore 时显式分不采信，只用 map。"""
    from scoring.rules import Rules, session_conclusion
    from scoring.items import build_context, extract_items
    from scoring.operators import Verdict
    from tests.helpers import fact
    rec = mk_record()
    case = dict(rec["case"])
    case["facts"] = [fact("F1", text="甲")]
    ctx = build_context(rec, case)
    items = extract_items(ctx)
    eff = {"F1": Verdict("对", "rule", "keyword@1", score=10)}    # 显式分 10（本应压过 map 的 100）
    plan = {"uses": {"F1": "score", "X1": "ref"}, "weights": {"F1": 1.0}, "unassigned": False}
    v_adopt = session_conclusion(rec, ctx, items, eff, Rules(), scoring=plan)
    rules_ignore = Rules(); rules_ignore.operator_score = "ignore"
    v_ignore = session_conclusion(rec, ctx, items, eff, rules_ignore, scoring=plan)
    assert v_adopt.score == 10 and v_ignore.score == 100


def test_subject_fp_changes_on_profile_content_or_agent():
    """场景 #6/#7：profile 内容或 agent 版本变 → subject_fp 变（同一 id 也不混）。"""
    from scoring.subject import subject_fp_of_manifest
    base = {"profile": {"id": "p", "content": {"id": "p", "model": "glm-5.3"}},
            "snapshot": {"skills_mode": "default-roots", "skills_base": "sb",
                         "agent": {"name": "dsh-cli", "version": "0.2.0-rc.6"}}}
    fp0, _, inc0 = subject_fp_of_manifest(base)
    changed_model = json.loads(json.dumps(base))
    changed_model["profile"]["content"]["model"] = "deepseek-v4-flash"
    fp1, _, _ = subject_fp_of_manifest(changed_model)
    changed_agent = json.loads(json.dumps(base))
    changed_agent["snapshot"]["agent"]["version"] = "0.2.0"
    fp2, _, _ = subject_fp_of_manifest(changed_agent)
    unrelated = json.loads(json.dumps(base))
    unrelated["profile"]["content"]["description"] = "改描述不改指纹"      # C7：无关字段
    fp3, _, _ = subject_fp_of_manifest(unrelated)
    assert len({fp0, fp1, fp2}) == 3 and fp3 == fp0 and inc0 == 0


# ---------------------------------------------------------------- 计分配置导入落库（doc/17 §3/§7）

def test_import_registers_case_scoring(tmp_path):
    """dist 的 scoring 块按用例覆盖写 case_scoring、刷新缓存列；只改计分配置
    （standard_fp 不变）不产生新标准；未分配用例在导入报告里点名。"""
    db = str(tmp_path / "sc.db")
    store = Store(db)
    case = dict(mk_record()["case"])                     # 计分块：F1=100，X1 参考
    fp = store.import_evalset([case], "v1")
    cid = case.get("case_id") or case.get("id") or "ec-1"
    sc = store.case_scoring(cid)
    assert sc["items"]["F1"] == {"use": "score", "weight": 100.0}
    assert sc["items"]["X1"]["use"] == "ref"
    assert fp["unassigned"] == []
    plan, sfp, _ = store.scoring_plan(cid)
    assert plan["weights"] == {"F1": 100.0}
    assert store.questions()[0]["current_scoring_fp"] == sfp

    # 只改计分配置（权重对半、X1 显式计分）：standard_fp 不变 → 无新标准，case_scoring 覆盖
    case2 = json.loads(json.dumps(case))
    case2["scoring"] = {"items": {"F1": {"use": "score", "weight": 50.0},
                                  "X1": {"use": "score", "weight": 50.0}},
                        "unassigned": False}
    store.import_evalset([case2], "v2")
    assert store.current_spec(store.questions()[0]["qkey"])[0] \
        == store.questions()[0]["current_standard_fp"]
    plan2, sfp2, _ = store.scoring_plan(cid)
    assert plan2["weights"] == {"F1": 50.0, "X1": 50.0} and sfp2 != sfp   # 口径指纹变

    # 未分配：计分权重合计 0 → 报告点名
    case3 = json.loads(json.dumps(case))
    case3["scoring"] = {"items": {"F1": {"use": "score", "weight": 0.0},
                                  "X1": {"use": "ref"}}, "unassigned": True}
    rep3 = store.import_evalset([case3], "v3")
    assert rep3["unassigned"] == [cid]
    store.close()
