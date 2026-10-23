"""评测库存储（改造方案 §6、整改清单 R1/R2/R5/R8/R9 验收）：建表迁移、
ingest 幂等、当前期望管理、有效结论与作废改判、未决队列、run 顺序。"""
import json

import pytest

from scoring.items import input_fp_of, judge_fp_of
from scoring.policy import policy_from_dict
from scoring.store import Store, StoreError
from tests.helpers import make_run, mk_record

POLICY = policy_from_dict({})
POLICY_SHA = "p1"


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "eval.db")
    yield s
    s.close()


def ingest(store, tmp_path, name, records, snapshot=None, run_id=None, manifest=None):
    """落 run 目录并入库；目录只建一次，重复调用等价重复 ingest。"""
    from scoring.cli import ingest_run
    run_dir = tmp_path / name
    if not run_dir.exists():
        run_dir = make_run(tmp_path, name, records, snapshot=snapshot, run_id=run_id)
    return ingest_run(store, run_dir)


# ---------------------------------------------------------------- 建表与迁移

def test_schema_version(store):
    v = store.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    assert v["value"] == "3"
    # spec_versions 已回填
    assert store.conn.execute("SELECT COUNT(*) c FROM spec_versions").fetchone()["c"] >= 0
    # 重复打开不重复迁移
    s2 = Store(store.path)
    assert s2.conn.execute("SELECT COUNT(*) c FROM meta").fetchone()["c"] == 1
    s2.close()


# ---------------------------------------------------------------- ingest（步骤 1 + R5 验收）

def test_ingest_idempotent(store, tmp_path):
    records = [mk_record(), mk_record(uid="ec-2#0")]
    run_id, n = ingest(store, tmp_path, "demo", records)
    assert (run_id, n) == ("demo", 2)
    assert len(store.answers()) == 2
    first_seen = {q["qkey"]: q["first_seen"] for q in store.questions()}
    ingest(store, tmp_path, "demo", records)
    assert len(store.answers()) == 2
    assert {q["qkey"]: q["first_seen"] for q in store.questions()} == first_seen
    a = store.answers()[0]
    assert a["mode"] == "natural" and a["trial"] == 1 and a["record_json"]
    assert a["judge_fp_at_run"] == judge_fp_of(records[0]["case"])
    assert a["qkey"] == records[0]["case"]["uid"] + "|" + input_fp_of(records[0]["case"])


def test_ingest_sets_current_when_absent(store, tmp_path):
    """R5：空库 ingest 即设当前期望并置活跃 → 首次 score 直接可判定。"""
    ingest(store, tmp_path, "demo", [mk_record()])
    q = store.questions()[0]
    assert q["active"] == 1 and q["current_judge_fp"] == judge_fp_of(mk_record()["case"])
    # 已有当前期望的题目（specs import 过）不被 run 的执行时期望覆盖
    store.import_evalset(_dist_rows("v2", text="改写后的事实文本"), "v2")
    ingest(store, tmp_path, "demo2", [mk_record()])
    q2 = next(x for x in store.questions() if x["uid"] == "ec-1#0")
    assert q2["current_judge_fp"] == judge_fp_of(_dist_rows("v2", text="改写后的事实文本")[0])


def test_ingest_registers_at_run_spec(store, tmp_path):
    records = [mk_record()]
    ingest(store, tmp_path, "demo", records)
    a = store.answers()[0]
    spec = store.spec_of(a["qkey"], a["judge_fp_at_run"])
    assert spec is not None and spec["uid"] == "ec-1#0"


# ---------------------------------------------------------------- 当前期望（§5.1 + R1 验收）

def _dist_rows(version, text="未收尾款不应电放", ref="参考"):
    case = dict(mk_record()["case"])
    return [{**case, "evalset_version": version, "reference_answer": ref, "facts": [
        {"id": "F1", "kind": "required", "text": text, "keywords": [], "turn": None,
         "group": None, "evidence": None},
        case["facts"][1]]}]


def test_set_current_rolls_back_same_fp_spec(store, tmp_path):
    """R1：只改参考答案的 v2（判定指纹不变）后，set-current v1 能回退。"""
    ingest(store, tmp_path, "demo", [mk_record()])
    store.import_evalset(_dist_rows("v1", ref="旧参考"), "v1")
    fp_v1 = judge_fp_of(_dist_rows("v1")[0])
    assert store.current_spec(store.questions()[0]["qkey"])[0] == fp_v1
    store.import_evalset(_dist_rows("v2", ref="新参考"), "v2")     # 判定指纹相同
    assert store.current_spec(store.questions()[0]["qkey"])[0] == fp_v1   # 内容寻址，期望不变
    n = store.set_current("v1")
    q = store.questions()[0]
    assert n == 1 and q["active"] == 1 and q["current_judge_fp"] == fp_v1


def test_set_current_rolls_back_changed_spec(store, tmp_path):
    """R1：v2 改了事实文本（判定指纹变），set-current v1 回到 v1 的指纹。"""
    ingest(store, tmp_path, "demo", [mk_record()])
    store.import_evalset(_dist_rows("v1"), "v1")
    store.import_evalset(_dist_rows("v2", text="改写后的事实文本"), "v2")
    fp_v1 = judge_fp_of(_dist_rows("v1")[0])
    assert store.current_spec(store.questions()[0]["qkey"])[0] != fp_v1
    assert store.set_current("v1") == 1
    assert store.current_spec(store.questions()[0]["qkey"])[0] == fp_v1


def test_set_current_unknown_version_is_error(store, tmp_path):
    """R1：未知版本 → 报错，题目状态与当前期望不变。"""
    ingest(store, tmp_path, "demo", [mk_record()])
    store.import_evalset(_dist_rows("v1"), "v1")
    before = {q["qkey"]: (q["active"], q["current_judge_fp"]) for q in store.questions()}
    with pytest.raises(StoreError, match="未知评测集版本"):
        store.set_current("no-such-version")
    assert {q["qkey"]: (q["active"], q["current_judge_fp"]) for q in store.questions()} == before


def test_missing_question_marked_inactive(store, tmp_path):
    ingest(store, tmp_path, "demo", [mk_record()])
    store.import_evalset(_dist_rows("v1"), "v1")
    store.import_evalset([dict(dict(mk_record()["case"], uid="other#0", id="other", case_id="other"),
                               evalset_version="v2")], "v2")
    q = next(x for x in store.questions() if x["uid"] == "ec-1#0")
    assert q["active"] == 0 and q["current_judge_fp"] is not None


# ---------------------------------------------------------------- 有效结论与改判（§6.3）

def _write_one_verdict(store, answer_id, judge_fp, chain_sha, item_id="F1", kind="required",
                       value="未提及", item_sha="is1", answer_sha="as1", status="ok"):
    store.write_verdicts(answer_id, judge_fp, chain_sha, [{
        "schema": 1, "run_id": "r", "episode_id": "e", "uid": "u",
        "item": {"kind": kind, "id": item_id, "turn": None},
        "machine": {"value": value, "source": "judge" if value else "", "operator": "f@1", "reason": ""},
        "trace": [], "answer_sha": answer_sha, "item_sha": item_sha, "status": status}])


def test_effective_review_requires_sha_match_and_scale(store, tmp_path):
    records = [mk_record()]
    ingest(store, tmp_path, "demo", records)
    a = store.answers()[0]
    fp = a["judge_fp_at_run"]
    _write_one_verdict(store, a["answer_id"], fp, "c1")
    store.add_review(a["answer_id"], "F1", "对", "r", "codfish", "is1", "as1")
    eff = store.effective(store.verdicts(a["answer_id"], fp, "c1")[0], store.reviews_for(a["answer_id"]))
    assert eff.value == "对" and eff.source == "human"
    store.add_review(a["answer_id"], "F1", "满足", "", "codfish", "is1", "as1")   # 取值不合法
    eff3 = store.effective(store.verdicts(a["answer_id"], fp, "c1")[0], store.reviews_for(a["answer_id"]))
    assert eff3.value == "对"


def test_voided_reviews_listing(store, tmp_path):
    records = [mk_record()]
    ingest(store, tmp_path, "demo", records)
    a = store.answers()[0]
    fp = a["judge_fp_at_run"]
    _write_one_verdict(store, a["answer_id"], fp, "c1")
    store.add_review(a["answer_id"], "F1", "对", "", "codfish", "is1", "as1")     # 有效
    store.add_review(a["answer_id"], "F1", "对", "", "codfish", "isX", "as1")     # item_sha 不匹配
    store.add_review(a["answer_id"], "F9", "对", "", "codfish", "is1", "as1")     # 条目不存在
    voided = store.voided_reviews(a["answer_id"])
    assert len(voided) == 2
    reasons = {v["reason"] for v in voided}
    assert any("item_sha" in r for r in reasons) and any("条目不存在" in r for r in reasons)


# ---------------------------------------------------------------- 未决队列（R2 验收）

def _write_verdicts(store, answer_id, judge_fp, chain_sha, specs):
    """一次写一个会话的多条判定（write_verdicts 按 (answer, fp, chain) 整键覆盖）。"""
    rows = []
    for sp in specs:
        rows.append({
            "schema": 1, "run_id": "r", "episode_id": "e", "uid": "u",
            "item": {"kind": sp.get("kind", "required"), "id": sp["item_id"], "turn": None},
            "machine": {"value": sp.get("value"), "source": "judge" if sp.get("value") else "",
                        "operator": "f@1", "reason": ""},
            "trace": [], "answer_sha": sp.get("answer_sha", "as1"),
            "item_sha": sp.get("item_sha", "is1"), "status": sp.get("status", "ok")})
    store.write_verdicts(answer_id, judge_fp, chain_sha, rows)


def test_undecided_items_respect_reviews_and_short_circuit(store, tmp_path):
    records = [mk_record()]
    ingest(store, tmp_path, "demo", records)
    a = store.answers()[0]
    fp = a["judge_fp_at_run"]
    _write_verdicts(store, a["answer_id"], fp, "c1", [
        dict(item_id="F1", value=None, status="undecided"),
        dict(item_id="X1", kind="forbidden", value=None, status="undecided")])
    items, total = store.undecided_items("c1")
    assert total == 2 and {i["item_id"] for i in items} == {"F1", "X1"}
    # 人工改判 F1 后：F1 出队（有效结论已定），X1 仍在
    row = next(v for v in store.verdicts(a["answer_id"], fp, "c1") if v["item_id"] == "F1")
    store.add_review(a["answer_id"], "F1", "对", "", "codfish", row["item_sha"], row["answer_sha"])
    items2, total2 = store.undecided_items("c1")
    assert total2 == 1 and items2[0]["item_id"] == "X1"
    # 回答变化后重判（answer_sha 变化）：旧改判失效 → F1 回到队列
    _write_verdicts(store, a["answer_id"], fp, "c1", [
        dict(item_id="F1", value=None, status="undecided", answer_sha="as2"),
        dict(item_id="X1", kind="forbidden", value=None, status="undecided")])
    items3, total3 = store.undecided_items("c1")
    assert total3 == 2


def test_undecided_items_skip_short_circuited_facts(store, tmp_path):
    """前提判"不成立"的会话：事实条目被短路，不进未决队列（R2 验收 3）。"""
    case = mk_record()["case"]
    case["preconditions"] = [{"id": "P1", "turn": 1, "text": "前提", "injections": []}]
    records = [mk_record(case=case)]
    ingest(store, tmp_path, "demo", records)
    a = store.answers()[0]
    fp = a["judge_fp_at_run"]
    _write_verdicts(store, a["answer_id"], fp, "c1", [
        dict(item_id="P1", kind="precond", value="不成立"),
        dict(item_id="F1", value=None, status="undecided"),
        dict(item_id="X1", kind="forbidden", value=None, status="undecided")])
    items, total = store.undecided_items("c1")
    # P1 已判定（不成立）本就不进队列；被短路的事实/禁止条目也不进
    assert items == [] and total == 0


# ---------------------------------------------------------------- rescore 与 run 顺序

def test_rescore_does_not_touch_verdicts(store, tmp_path):
    records = [mk_record()]
    ingest(store, tmp_path, "demo", records)
    a = store.answers()[0]
    fp = a["judge_fp_at_run"]
    _write_one_verdict(store, a["answer_id"], fp, "c1")
    n = store.rescore(POLICY, POLICY_SHA, current_chain_sha="c1")
    assert n >= 1
    c = store.conclusion(a["answer_id"], fp, "c1", POLICY_SHA)
    assert c["status"] == "fail"                  # F1 未提及 → 失败（未调任何算子）
    records2 = [mk_record(status="error", failed_turn=1, uid="ec-9#0")]
    ingest(store, tmp_path, "demo2", records2)
    a2 = store.answers(run_id="demo2")[0]
    store.rescore(POLICY, POLICY_SHA, current_chain_sha="c1")
    assert store.conclusion(a2["answer_id"], a2["judge_fp_at_run"], "c1", POLICY_SHA)["status"] == "error"


def test_runs_ordered_by_started_at(store, tmp_path):
    """R8：后导入的旧 run 仍排在前面（按运行开始时间）。"""
    late = {"started_at": "2026-10-23T10:00:00Z"}
    early = {"started_at": "2026-10-22T09:00:00Z"}
    from scoring.cli import ingest_run
    d1 = make_run(tmp_path, "r2", [mk_record()], run_id="r2")
    (d1 / "run.json").write_text(json.dumps({"run_id": "r2", "started_at": late["started_at"],
                                             "snapshot": {}, "profile": {}}), encoding="utf-8")
    ingest_run(store, d1)
    d2 = make_run(tmp_path, "r1", [mk_record()], run_id="r1")
    (d2 / "run.json").write_text(json.dumps({"run_id": "r1", "started_at": early["started_at"],
                                             "snapshot": {}, "profile": {}}), encoding="utf-8")
    ingest_run(store, d2)
    assert [r["run_id"] for r in store.runs()] == ["r1", "r2"]
    # 重复导入不刷新 imported_at 语义（started_at 仍为准）
    ingest_run(store, d2)
    assert [r["run_id"] for r in store.runs()] == ["r1", "r2"]


# ---------------------------------------------------------------- legacy 幂等（R9）

def test_add_legacy_review_idempotent(store, tmp_path):
    ingest(store, tmp_path, "demo", [mk_record()])
    a = store.answers()[0]
    key = "demo/e1/F1/t1/对"
    assert store.add_legacy_review(a["answer_id"], "F1", "对", "", "codfish", "is1", "as1", key) is True
    assert store.add_legacy_review(a["answer_id"], "F1", "对", "", "codfish", "is1", "as1", key) is False
    assert len(store.reviews_for(a["answer_id"])) == 1


# ---------------------------------------------------------------- 备份

def test_backup(store, tmp_path):
    ingest(store, tmp_path, "demo", [mk_record()])
    target = store.backup(tmp_path / "backup.db")
    s2 = Store(target)
    assert len(s2.answers()) == 1
    s2.close()


# ---------------------------------------------------------------- v1 库升级（schema v3）

def _old_input_fp(case: dict) -> str:
    """v1（R11 之前）的输入指纹：不含 topic。冻结参照，只在本测试用。"""
    import hashlib
    payload = {
        "history": case.get("history", "live"),
        "turns": [t.get("content", "") for t in (case.get("turns") or []) if t.get("role") == "user"],
        "injections": [{k: i.get(k) for k in ("point", "option", "rev", "file", "key", "value")}
                       for i in (case.get("injections") or [])],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False)
                          .encode("utf-8")).hexdigest()[:16]


def _build_v1_db(path, record, run_id="r1"):
    """按 v1 形状手工建库：questions/specs/answers 用旧算法 qkey，无 spec_versions、
    无 answers 预计算列、无 runs.started_at。"""
    import sqlite3
    from scoring.store import _SCHEMA
    case = record["case"]
    old_q = f"{case['uid']}|{_old_input_fp(case)}"
    fp = judge_fp_of(case)
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    manifest = {"run_id": run_id, "started_at": "2026-10-22T06:48:21Z", "snapshot": {"skills_mode": "default-roots"}}
    conn.execute("INSERT INTO runs VALUES(?,?,?,?,?,?)",
                 (run_id, "/x", "v1", "p1", json.dumps(manifest, ensure_ascii=False), "2026-10-23T09:00:00"))
    conn.execute("INSERT INTO questions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                 (old_q, case["uid"], case.get("case_id"), case.get("variant_index", 0), None,
                  case.get("dim"), case.get("topic"), "[]", _old_input_fp(case), fp, 1, "2026-10-22T08:00:00"))
    conn.execute("INSERT INTO specs VALUES(?,?,?,?,?)",
                 (old_q, fp, json.dumps(case, ensure_ascii=False), "v1", "2026-10-22T08:00:00"))
    ep = record.get("episode_id") or f"{record['uid']}-{record['mode']}-t{record['trial']}"
    conn.execute("INSERT INTO answers VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (f"{run_id}/{ep}", old_q, run_id, ep, fp, "p1", '["glm-5.3"]', record.get("mode", "natural"),
                  record.get("trial", 1), "default-roots", None, "v1", "ok",
                  json.dumps(record, ensure_ascii=False)))
    conn.execute("INSERT INTO meta VALUES('schema_version', '1')")
    conn.commit()
    conn.close()
    return old_q


def test_v1_upgrade_recompute_and_backfill(tmp_path):
    """v1 库升级验收：题目数不变、旧/新答案同 qkey、预计算列与 started_at 回填。"""
    from scoring.items import input_fp_of, qkey_of
    record = mk_record()
    record["injections_applied"] = [{"point": "电放时机", "found": 2}]
    db_path = tmp_path / "v1.db"
    old_q = _build_v1_db(db_path, record)
    assert old_q != qkey_of(record["case"])          # 旧算法 qkey 不含 topic → 必然不同

    store = Store(db_path)                           # 打开即完成 1→2→3 迁移
    assert store.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()["value"] == "3"
    new_q = qkey_of(record["case"])
    qs = store.questions()
    assert len(qs) == 1 and qs[0]["qkey"] == new_q   # 没有拆成两题
    assert qs[0]["input_fp"] == input_fp_of(record["case"])
    old_answer = store.answers()[0]
    assert old_answer["qkey"] == new_q
    assert old_answer["injection_found_gt1"] == 1    # 预计算列回填（found=2）
    assert json.loads(old_answer["variant_leak_json"]) == []
    r = store.run("r1")
    assert r["started_at"] == "2026-10-22T06:48:21Z"  # 从 snapshot_json 回填
    # 期望与版本映射随 qkey 改写
    assert store.spec_of(new_q, old_answer["judge_fp_at_run"]) is not None
    assert store.current_spec(new_q)[0] == old_answer["judge_fp_at_run"]

    # 升级后导入同一题的新 run：落进同一个 qkey
    rec2 = mk_record()
    from scoring.cli import ingest_run
    d2 = make_run(tmp_path, "r2", [rec2], run_id="r2")
    ingest_run(store, d2)
    assert len(store.questions()) == 1
    assert {a["run_id"] for a in store.answers(qkey=new_q)} == {"r1", "r2"}
    store.close()


def test_v1_upgrade_merges_when_new_qkey_already_exists(tmp_path):
    """v2 代码已用新算法建过同题（升级前新旧并存）：合并为一题，first_seen 取更早。"""
    from scoring.items import qkey_of
    record = mk_record()
    db_path = tmp_path / "v1b.db"
    _build_v1_db(db_path, record)
    new_q = qkey_of(record["case"])
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO questions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                 (new_q, record["case"]["uid"], record["case"].get("case_id"), 0, None,
                  record["case"].get("dim"), record["case"].get("topic"), "[]",
                  new_q.split("|", 1)[1], None, 0, "2026-10-23T10:00:00"))
    conn.commit()
    conn.close()

    store = Store(db_path)
    qs = store.questions()
    assert len(qs) == 1 and qs[0]["qkey"] == new_q
    assert qs[0]["first_seen"] == "2026-10-22T08:00:00"      # 合并取更早
    assert qs[0]["current_judge_fp"] == judge_fp_of(record["case"])   # 新行无当前期望 → 取旧行的
    assert qs[0]["active"] == 1
    store.close()


def test_run_without_started_at_falls_back_stably(tmp_path):
    """无 run.json started_at 的 run：排序回退导入时间，重导不刷新顺序。"""
    from scoring.cli import ingest_run
    d1 = make_run(tmp_path, "r1", [mk_record()], run_id="r1")
    d2 = make_run(tmp_path, "r2", [mk_record(uid="ec-2#0")], run_id="r2")
    store = Store(tmp_path / "s.db")
    ingest_run(store, d1)
    ingest_run(store, d2)
    assert [r["run_id"] for r in store.runs()] == ["r1", "r2"]
    ingest_run(store, d1)                             # 重导旧 run：顺序不变
    assert [r["run_id"] for r in store.runs()] == ["r1", "r2"]
    assert store.run("r1")["started_at"] is None      # 无真实值时存 NULL，不伪造
    store.close()
