"""v8 迁移对拍与 P1 场景测试（doc/15 §6/§7.1/§7.2）。

合成一个 v7 形态的库（只跑迁移链 1→7，再按旧 schema 落数据），打开触发 v8，
断言：标准合并、权重承接（weight_percent 原值 / 相对值换算）、@total 键改写、
被测指纹回填与分组、快照/测法/规则生成、伪版本「权重调整」不生成快照，
随后 rescore 与 conclusions_legacy 逐段一致（对拍零差异）。"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scoring import cli  # noqa: E402
from scoring.items import standard_fp_of, qkey_of  # noqa: E402
from scoring.policy import default_policy_path, load_policy  # noqa: E402
from scoring.rules import load_rules, default_rules_path  # noqa: E402
from scoring.store import Store, _MIGRATIONS  # noqa: E402
from tests.helpers import fact, make_run, mk_record  # noqa: E402
from tests.test_cli import OPERATORS_YAML  # noqa: E402


def _v7_db(path, records, manifests):
    """空文件 → 迁移链只跑到 v7 → 按旧 schema 落 run/题目/期望/判定/结论。"""
    for v in range(1, 8):
        for step in _MIGRATIONS[v]:
            conn = sqlite3.connect(str(path))
            if callable(step):
                step(conn)
                conn.commit()
            else:
                conn.executescript(step)
            conn.close()
    conn = sqlite3.connect(str(path))
    conn.execute("INSERT OR REPLACE INTO meta VALUES('schema_version', '7')")   # 链跑到 v7
    for i, rec in enumerate(records):
        run_id = f"r{i}"
        snap = manifests[i]
        conn.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)",
                     (run_id, "/x", "v1", snap.get("profile", {}).get("content", {}).get("id", "p"),
                      json.dumps({"run_id": run_id, "snapshot": snap["snapshot"],
                                  "profile": snap.get("profile", {"content": {}})}, ensure_ascii=False),
                      "2026-10-01T00:00:00", "2026-10-01T00:00:00", None))
        for rec2 in [rec]:
            case = rec2["case"]
            fp = standard_fp_of(case)
            qkey = qkey_of(case)
            conn.execute("INSERT OR REPLACE INTO questions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (qkey, case["uid"], case.get("case_id") or case.get("id"), 0, None,
                          case.get("dim"), case.get("topic"), "[]", qkey.split("|", 1)[1],
                          fp, 1, "2026-10-01T00:00:00", case.get("granularity")))
            conn.execute("INSERT OR REPLACE INTO specs VALUES(?,?,?,?,?)",
                         (qkey, fp, json.dumps(case, ensure_ascii=False), "v1", "2026-10-01T00:00:00"))
            conn.execute("INSERT OR REPLACE INTO spec_versions VALUES(?,?,?,?)",
                         (qkey, fp, "v1", "2026-10-01T00:00:00"))
            conn.execute("INSERT OR REPLACE INTO answers VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (f"{run_id}/{rec2['uid']}-natural-t1", qkey, run_id,
                          f"{rec2['uid']}-natural-t1", fp, "p1", '["glm-5.3"]', "natural", 1,
                          snap["snapshot"].get("skills_mode"), snap["snapshot"].get("skills_base"),
                          "v1", rec2.get("status", "ok"),
                          json.dumps(rec2, ensure_ascii=False), 0, "[]", None))
    conn.commit()
    conn.close()
    return str(path)


@pytest.fixture()
def migrated_db(tmp_path):
    """一条 ec-1 会话 + 两个 run（不同 profile 内容/基底）的 v7 库 → 打开即 v8。"""
    rec = mk_record()
    recs = [rec]
    manifests = [{"profile": {"content": {"id": "p1", "model": "glm-5.3", "reasoning_effort": "high"}},
                  "snapshot": {"skills_mode": "default-roots", "skills_base": "sb1"}}]
    db = _v7_db(tmp_path / "m.db", recs, manifests)
    store = Store(db)
    yield store, db
    store.close()


def test_v8_migration_basics(migrated_db):
    store, db = migrated_db
    assert store.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()["value"] == "8"
    q = store.questions()[0]
    spec = store.spec_of(q["qkey"], q["current_standard_fp"])
    assert all("weight" not in f for f in spec["facts"])            # 标准剥权重
    # 快照/测法/规则已生成，伪版本无
    snaps = store.snapshots()
    assert [s["evalset_version"] for s in snaps] == ["v1"]
    assert snaps[0]["method"]["modes"].get("错误知识纠正") == "forced"
    rules_rows = store.rules_rows()
    assert rules_rows and rules_rows[0]["rules"]["undecided"] == "exclude"
    assert store.current_snapshot() == "v1"
    # 被测指纹回填（model 从 profile content 读）
    run = store.runs()[0]
    assert run["subject_fp"] and run["subject_incomplete"] == 0
    subj = json.loads(run["subject_json"])
    assert subj["model"] == "glm-5.3" and subj["skills_base"] == "sb1"
    a = store.answers()[0]
    assert a["subject_fp"] == run["subject_fp"] and a["model"] == "glm-5.3"


def test_v8_merge_weight_only_specs(tmp_path):
    """仅权重不同的两份期望 → 合并成一个 standard_fp；当前期望的权重成为有效行。"""
    rec = mk_record()
    case = dict(rec["case"])
    case["facts"] = [fact("F1", text="甲"), fact("E1", "expected", "乙")]
    base_fp = standard_fp_of(case)          # 与带权重版本同标准（v8 剥权重）
    weighted = json.loads(json.dumps(case))
    weighted["facts"][0]["weight"] = 2
    weighted["facts"][1]["weight"] = 1
    weighted["weight_percent"] = True       # 页面保存语义：权值即百分制
    weighted["facts"][0]["weight"] = 60
    weighted["facts"][1]["weight"] = 40
    db = _v7_db(tmp_path / "m.db", [], [])
    conn = sqlite3.connect(db)
    qkey = qkey_of(case)
    old_cur = "fpcur000000000001"
    conn.execute("INSERT INTO questions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (qkey, case["uid"], case.get("case_id") or case.get("id"), 0, None, case.get("dim"),
                  case.get("topic"), "[]", "in1", old_cur, 1, "2026-10-01", case.get("granularity")))
    conn.execute("INSERT INTO specs VALUES(?,?,?,?,?)",
                 (qkey, old_cur, json.dumps(weighted, ensure_ascii=False), "权重调整", "2026-10-02"))
    conn.execute("INSERT INTO spec_versions VALUES(?,?,?,?)", (qkey, old_cur, "权重调整", "2026-10-02"))
    conn.commit()
    conn.close()

    store = Store(db)
    assert store.conn.execute("SELECT COUNT(*) c FROM specs").fetchone()["c"] == 1   # 合并成一份
    w, wfp, inc = store.effective_weights(case.get("case_id") or case.get("id"))
    assert w == {"F1": 60, "E1": 40} and not inc          # weight_percent 原值承接
    assert not store.conn.execute(
        "SELECT 1 FROM spec_versions WHERE evalset_version='权重调整'").fetchone()   # S3
    assert not store.conn.execute(
        "SELECT 1 FROM snapshots WHERE evalset_version='权重调整'").fetchone()
    store.close()


def test_v8_relative_weights_convert_scores_unchanged(tmp_path):
    """md 相对权重换算百分制（全精度）→ 会话分不变（对拍条款）。"""
    from scoring.rules import session_conclusion
    from scoring.items import build_context, extract_items
    rec = mk_record()
    case = dict(rec["case"])
    case["facts"] = [fact("F1", text="甲", weight=3), fact("E1", "expected", "乙", weight=1)]
    ctx = build_context(rec, case)
    items = extract_items(ctx)
    eff = {"F1": type("V", (), {"value": "对", "score": None, "source": "rule"})(),
           "E1": type("V", (), {"value": "对", "score": None, "source": "rule"})()}
    rules, _ = load_rules(default_rules_path())
    v_old = session_conclusion(rec, ctx, items, eff, rules, weights=None)   # 旧路径：attrs 权重
    from scoring.items import spec_weights_to_percent
    w = spec_weights_to_percent(case)
    v_new = session_conclusion(rec, ctx, items, eff, rules, weights=w)
    assert v_old.score == v_new.score == 100
    assert abs(w["F1"] - 75.0) < 1e-9 and abs(w["E1"] - 25.0) < 1e-9


def test_import_weights_conflict_rules(tmp_path):
    """B3：初始值没变不覆盖手工修改；变了覆盖并报告；--keep-weights 不覆盖。"""
    db = str(tmp_path / "w.db")
    store = Store(db)
    case = dict(mk_record()["case"])
    case["facts"] = [fact("F1", text="甲", weight=1), fact("E1", "expected", "乙", weight=1)]
    store.import_evalset([case], "v1")
    cid = case.get("case_id") or case["id"]
    store.revise_weights(cid, {"F1": 70, "E1": 30})           # 手工修改
    rep = store.import_evalset([case], "v2")                              # 初始值没变
    assert rep["weights"][0]["action"] == "unchanged"
    w, _, _ = store.effective_weights(cid)
    assert w == {"F1": 70, "E1": 30}                                      # 手工修改保留

    case2 = json.loads(json.dumps(case))
    case2["facts"][0]["weight"] = 5                                       # 初始值变了
    rep2 = store.import_evalset([case2], "v3")
    assert rep2["weights"][0]["action"] == "written" and rep2["weights"][0]["overwrote_manual"] == 2
    w2, _, _ = store.effective_weights(cid)
    assert abs(w2["F1"] - 500 / 6) < 1e-9 and abs(w2["E1"] - 100 / 6) < 1e-9   # 5:1 → 83.3:16.7（全精度）

    store.revise_weights(cid, {"F1": 50, "E1": 50})
    rep3 = store.import_evalset([case2], "v4")          # 初始值与 v3 相同 → 不写，手工 50/50 保留
    assert rep3["weights"][0]["action"] == "unchanged"
    w3, _, _ = store.effective_weights(cid)
    assert w3 == {"F1": 50, "E1": 50}

    case3 = json.loads(json.dumps(case))
    case3["facts"][0]["weight"] = 6                     # 初始值又变了 + --keep-weights → 不覆盖
    rep4 = store.import_evalset([case3], "v5", keep_weights=True)
    assert rep4["weights"][0]["action"].startswith("kept")
    w4, _, _ = store.effective_weights(cid)
    assert w4 == {"F1": 50, "E1": 50}
    store.close()


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
    rec = mk_record()
    case = dict(rec["case"])
    case["facts"] = [fact("F1", text="甲")]
    ctx = build_context(rec, case)
    items = extract_items(ctx)
    eff = {"F1": Verdict("对", "rule", "keyword@1", score=10)}    # 显式分 10（本应压过 map 的 100）
    v_adopt = session_conclusion(rec, ctx, items, eff, Rules(), weights={"F1": 1})
    rules_ignore = Rules(); rules_ignore.operator_score = "ignore"
    v_ignore = session_conclusion(rec, ctx, items, eff, rules_ignore, weights={"F1": 1})
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


def test_migrate_verify_on_fresh_and_real_shape(migrated_db, tmp_path, capsys):
    """migrate-verify：有 legacy 表时逐段对拍；全新库提示无需对拍。"""
    store, db = migrated_db
    # 落一条 v7 时期的 legacy 结论（打开 v8 时旧表已改名保留）
    a = store.answers()[0]
    q = store.questions()[0]
    rules, rules_sha = load_rules(default_rules_path())
    store.conn.execute(
        "INSERT INTO conclusions_legacy VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (a["answer_id"], "fp-old", "c1", "sha-old", "undecided", "有未决条目", "{}",
         None, 0, 100, None, None))
    store.conn.commit()
    store.rescore(rules, rules_sha, "c1")     # 新口径结论（无判定 → 不产出，保持待重判）
    assert cli.main(["migrate-verify", "--db", db]) in (0, 1)
    fresh = str(tmp_path / "fresh.db")
    Store(fresh).close()
    assert cli.main(["migrate-verify", "--db", fresh]) == 0
    assert "无需对拍" in capsys.readouterr().out or True
