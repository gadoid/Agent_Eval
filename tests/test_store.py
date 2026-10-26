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
    assert v["value"] == "6"
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


def test_ingest_reads_profile_id_from_content(store, tmp_path):
    """run.json 的 profile 为 {path, sha256, content}：profile_id 取 content.id（不是顶层 id）。"""
    from scoring.cli import ingest_run
    records = [mk_record()]
    run_dir = make_run(tmp_path, "pid", records)
    manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    manifest["profile"] = {"path": "p.yaml", "sha256": "x", "content": {"id": "profile-a"}}
    (run_dir / "run.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    ingest_run(store, run_dir)
    assert store.runs()[0]["profile_id"] == "profile-a"
    assert {a["profile_id"] for a in store.answers()} == {"profile-a"}


def test_ingest_persists_injection_observed(store, tmp_path):
    """runner 的 injection_observed 原样入库；缺失（旧记录）为 NULL。"""
    ingest(store, tmp_path, "inj", [mk_record(uid="ec-1#0", injection_observed={"J1": True, "J2": False}),
                                    mk_record(uid="ec-1#1")])
    by_uid = {json.loads(a["record_json"])["uid"]: a for a in store.answers()}
    assert json.loads(by_uid["ec-1#0"]["injection_observed_json"]) == {"J1": True, "J2": False}
    assert by_uid["ec-1#1"]["injection_observed_json"] is None


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

    store = Store(db_path)                           # 打开即完成 1→2→…→6 迁移
    assert store.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()["value"] == "6"
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


def _topic_record(topic, fact_text="未收尾款不应电放"):
    """同 uid 同输入、仅 topic 不同的两条记录（旧算法 qkey 相同、新算法分叉）。"""
    record = mk_record()
    record["case"] = json.loads(json.dumps(record["case"]))
    record["case"]["topic"] = topic
    record["case"]["turns"] = [{"role": "user", "content": "电放前要收齐尾款吗"}]
    record["case"]["injections"] = []
    if fact_text != "未收尾款不应电放":
        record["case"]["facts"][0]["text"] = fact_text
    return record


def test_v1_upgrade_qkey_fork_expectations_follow_their_topic(tmp_path):
    """M1：旧 qkey 分叉（同 uid 同输入、仅 topic 不同）时，期望跟着自己 spec_json 的
    topic 走，当前期望指针不跑丢；分叉出的另一题继承 first_seen、不带当前期望。"""
    from scoring.items import qkey_of
    rx = _topic_record("尾款与电放")                  # specs 登记过的期望（topic X）
    ry = _topic_record("信用证交单")                  # 另一 run 记录的同题（topic Y）
    old_fp = _old_input_fp(rx["case"])
    assert old_fp == _old_input_fp(ry["case"])        # 旧算法下同 qkey
    q_x, q_y = qkey_of(rx["case"]), qkey_of(ry["case"])
    assert q_x != q_y

    db_path = tmp_path / "v1fork.db"
    old_q = _build_v1_db(db_path, rx)
    assert old_q == f"ec-1#0|{old_fp}"
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    ep_y = f"{ry['uid']}-{ry['mode']}-t{ry['trial']}"
    conn.execute("INSERT INTO runs VALUES(?,?,?,?,?,?)",
                 ("r2", "/y", "v1", "p1", "{}", "2026-10-23T09:30:00"))
    conn.execute("INSERT INTO answers VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("r2/" + ep_y, old_q, "r2", ep_y, judge_fp_of(ry["case"]), "p1", '["glm-5.3"]',
                  ry["mode"], ry["trial"], "default-roots", None, "v1", "ok",
                  json.dumps(ry, ensure_ascii=False)))
    conn.commit()
    conn.close()

    store = Store(db_path)
    fp_x = judge_fp_of(rx["case"])
    by_q = {q["qkey"]: q for q in store.questions()}
    assert set(by_q) == {q_x, q_y}                    # 分叉成两题，旧 qkey 消失
    assert by_q[q_x]["current_judge_fp"] == fp_x and by_q[q_x]["active"] == 1
    assert by_q[q_y]["current_judge_fp"] is None and by_q[q_y]["active"] == 0
    assert by_q[q_y]["first_seen"] == by_q[q_x]["first_seen"] == "2026-10-22T08:00:00"
    assert store.spec_of(q_x, fp_x) is not None       # 期望在自己的 topic 名下
    assert store.spec_of(q_y, fp_x) is None
    assert store.conn.execute("SELECT COUNT(*) c FROM spec_versions WHERE qkey=?",
                              (q_y,)).fetchone()["c"] == 0
    a_by_run = {a["run_id"]: a for a in store.answers()}
    assert a_by_run["r1"]["qkey"] == q_x and a_by_run["r2"]["qkey"] == q_y
    store.close()


def test_v1_upgrade_qkey_fork_current_pointer_follows_its_spec(tmp_path):
    """M1：旧 qkey 下两份期望（judge_fp 不同、topic 不同）各归各的新 qkey；
    当前期望指针指向其期望所在的新 qkey，spec_versions 随期望拆分。"""
    from scoring.items import qkey_of
    rx = _topic_record("尾款与电放")                  # 期望 1：facts 文本 A、topic X
    ry = _topic_record("信用证交单", fact_text="电放必须取得托收指示书")  # 期望 2：文本 B、topic Y
    fp_x, fp_y = judge_fp_of(rx["case"]), judge_fp_of(ry["case"])
    assert fp_x != fp_y

    db_path = tmp_path / "v1fork2.db"
    _build_v1_db(db_path, rx)
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO specs VALUES(?,?,?,?,?)",
                 ("ec-1#0|" + _old_input_fp(ry["case"]), fp_y,
                  json.dumps(ry["case"], ensure_ascii=False), "v2", "2026-10-23T09:00:00"))
    conn.execute("UPDATE questions SET current_judge_fp=?", (fp_y,))   # 当前期望 = 期望 2
    conn.commit()
    conn.close()

    store = Store(db_path)
    q_x, q_y = qkey_of(rx["case"]), qkey_of(ry["case"])
    assert store.spec_of(q_x, fp_x) is not None and store.spec_of(q_y, fp_y) is not None
    assert store.current_spec(q_y)[0] == fp_y        # 指针跟着期望 2 走
    by_q = {q["qkey"]: q for q in store.questions()}
    assert by_q[q_y]["current_judge_fp"] == fp_y and by_q[q_y]["active"] == 1
    assert by_q[q_x]["current_judge_fp"] is None and by_q[q_x]["active"] == 0
    sv = {(r["qkey"], r["judge_fp"]) for r in store.conn.execute(
        "SELECT qkey, judge_fp FROM spec_versions")}
    assert sv == {(q_x, fp_x), (q_y, fp_y)}
    store.close()


def test_run_without_started_at_falls_back_stably(tmp_path):
    """无 run.json started_at 的 run：首次导入以导入时间回填落列，重导不刷新，
    run.json 真实值一旦出现即覆盖（M2）。"""
    from scoring.cli import ingest_run
    d1 = make_run(tmp_path, "r1", [mk_record()], run_id="r1")
    d2 = make_run(tmp_path, "r2", [mk_record(uid="ec-2#0")], run_id="r2")
    store = Store(tmp_path / "s.db")
    ingest_run(store, d1)
    ingest_run(store, d2)
    assert [r["run_id"] for r in store.runs()] == ["r1", "r2"]
    fallback = store.run("r1")["started_at"]
    assert fallback is not None                      # 回退值已落列，不存 NULL
    ingest_run(store, d1)                             # 重导旧 run：回退值不刷新
    assert [r["run_id"] for r in store.runs()] == ["r1", "r2"]
    assert store.run("r1")["started_at"] == fallback
    (d1 / "run.json").write_text(json.dumps(
        {"run_id": "r1", "started_at": "2026-10-20T00:00:00Z", "snapshot": {}, "profile": {}},
        ensure_ascii=False), encoding="utf-8")
    ingest_run(store, d1)                             # run.json 真实值出现：覆盖回退值
    assert store.run("r1")["started_at"] == "2026-10-20T00:00:00Z"
    assert [r["run_id"] for r in store.runs()] == ["r1", "r2"]
    store.close()


# ---------------------------------------------------------------- 百分制（schema v4，doc/10 §5–6）

def _scored_answer(store, tmp_path, facts=None):
    """F1（权重 2）、E1（期望，权重 1）、X1 的一段会话，机器全部判定为未决。"""
    from tests.helpers import fact
    case = mk_record()["case"]
    case["facts"] = facts or [fact("F1", text="甲", weight=2), fact("E1", "expected", "乙"),
                              fact("X1", "forbidden", "禁止")]
    ingest(store, tmp_path, "demo", [mk_record(case=case)])
    a = store.answers()[0]
    fp = a["judge_fp_at_run"]
    _write_verdicts(store, a["answer_id"], fp, "c1", [
        dict(item_id="F1", value=None, status="undecided"),
        dict(item_id="E1", kind="expected", value=None, status="undecided"),
        dict(item_id="X1", kind="forbidden", value="未采信")])
    return a, fp


def test_v4_columns_and_chains_table(store):
    cols = {t: {r["name"] for r in store.conn.execute(f"PRAGMA table_info({t})")}
            for t in ("verdicts", "reviews", "conclusions", "answers", "verdict_cache")}
    assert "score" in cols["verdicts"] and "score" in cols["reviews"] and "score" in cols["verdict_cache"]
    assert {"score", "score_min", "score_max", "auto_score", "score_source"} <= cols["conclusions"]
    assert "injection_observed_json" in cols["answers"]
    store.register_chain("c1", "规则", ["keyword@1"])
    store.register_chain("c1", None, ["keyword@1", "skill-path@1"], ["m-x"])   # 名称保留，算子清单更新
    assert store.chains() == [{"chain_sha": "c1", "name": "规则", "operators": ["keyword@1", "skill-path@1"],
                               "judge_models": ["m-x"], "created_at": store.chains()[0]["created_at"]}]


def test_submit_reviews_scores_and_conclusion(store, tmp_path):
    a, fp = _scored_answer(store, tmp_path)
    store.recompute_conclusion(a["answer_id"], fp, "c1", POLICY, POLICY_SHA)
    c0 = store.conclusion(a["answer_id"], fp, "c1", POLICY_SHA)
    assert c0["status"] == "undecided" and (c0["score_min"], c0["score_max"]) == (0, 100)
    c = store.submit_reviews(a["answer_id"], [
        {"item_id": "F1", "value": "部分", "score": 70, "reason": "自定"},
        {"item_id": "E1", "value": "对"}], None, "codfish", fp, "c1", POLICY, POLICY_SHA)
    assert c["status"] == "pass" and c["score"] == 80 and c["score_source"] == "auto"   # (2×70 + 100) / 3
    rows = store.reviews_for(a["answer_id"])
    assert [(r["item_id"], r["value"], r["score"]) for r in rows] == [("F1", "部分", 70.0), ("E1", "对", None)]


def test_submit_reviews_is_atomic(store, tmp_path):
    a, fp = _scored_answer(store, tmp_path)
    for bad in ([{"item_id": "F1", "value": "对"}, {"item_id": "E1", "value": "满足"}],      # 取值不合法
                [{"item_id": "F1", "value": "对", "score": 101}],                         # 分数越界
                [{"item_id": "F9", "value": "对"}]):                                       # 条目不存在
        with pytest.raises(StoreError):
            store.submit_reviews(a["answer_id"], bad, None, "codfish", fp, "c1", POLICY, POLICY_SHA)
    with pytest.raises(StoreError):                                                         # 会话分缺理由
        store.submit_reviews(a["answer_id"], [], {"score": 70, "reason": " "}, "codfish", fp, "c1",
                             POLICY, POLICY_SHA)
    assert store.reviews_for(a["answer_id"]) == []


def test_human_total_override_and_voiding(store, tmp_path):
    a, fp = _scored_answer(store, tmp_path)
    c = store.submit_reviews(a["answer_id"], [{"item_id": "F1", "value": "未提及"}, {"item_id": "E1", "value": "对"}],
                             {"score": 65, "reason": "表述到位，扣分过重"}, "codfish", fp, "c1", POLICY, POLICY_SHA)
    assert c["status"] == "pass" and c["score"] == 65 and c["score_source"] == "human" and c["auto_score"] == 33
    assert store.voided_reviews(a["answer_id"]) == []
    # 期望修订（judge_fp 变）后人工会话分作废
    store.conn.execute("UPDATE questions SET current_judge_fp='fp-new' WHERE qkey=?", (a["qkey"],))
    store.conn.execute("INSERT INTO specs SELECT qkey, 'fp-new', spec_json, evalset_version, imported_at"
                       " FROM specs WHERE qkey=? AND judge_fp=?", (a["qkey"], fp))
    voided = store.voided_reviews(a["answer_id"])
    assert any(v["review"]["item_id"] == "@total" and "期望已修订" in v["reason"] for v in voided)


def test_preview_does_not_write(store, tmp_path):
    a, fp = _scored_answer(store, tmp_path)
    v = store.preview_conclusion(a["answer_id"], [{"item_id": "F1", "value": "对"}, {"item_id": "E1", "value": "部分"}],
                                 None, fp, "c1", POLICY)
    assert v.status == "pass" and v.score == 83                    # (200 + 50) / 3 = 83.3
    v2 = store.preview_conclusion(a["answer_id"], [], {"score": 40, "reason": "r"}, fp, "c1", POLICY)
    assert v2.status == "fail" and v2.score_source == "human"
    assert store.reviews_for(a["answer_id"]) == []
    assert store.conclusion(a["answer_id"], fp, "c1", POLICY_SHA) is None


def test_undecided_items_include_weighted_expected(store, tmp_path):
    from tests.helpers import fact
    a, fp = _scored_answer(store, tmp_path)
    assert {i["item_id"] for i in store.undecided_items("c1")[0]} == {"F1", "E1"}


def test_undecided_items_skip_zero_weight_expected(store, tmp_path):
    from tests.helpers import fact
    a, fp = _scored_answer(store, tmp_path, facts=[fact("F1", text="甲"), fact("E1", "expected", "乙", weight=0),
                                                   fact("X1", "forbidden", "禁止")])
    assert {i["item_id"] for i in store.undecided_items("c1")[0]} == {"F1"}


def test_cache_round_trips_score(store):
    from scoring.operators import Verdict
    store.cache_put("k", Verdict("对", "judge", "llm-fact@1", reason="r", score=90))
    assert store.cache_get("k").score == 90


def test_spot_sampling_stable_per_answer():
    """P2：抽检抽样按 answer_id 独立哈希——某段退出候选不影响其余段的选中与否
    （顺序消费共享随机流会整体漂移）。"""
    from scoring.store import _spot_picked
    ids = [f"r/a{i}-natural-t1" for i in range(200)]
    picked = {a for a in ids if _spot_picked(a, 0.3)}
    assert 0.1 < len(picked) / len(ids) < 0.6          # 比例大致合理（固定哈希，确定性）
    for a in ids[:50]:
        assert _spot_picked(a, 0.3) == (a in picked)   # 逐段独立、可复现
    assert all(_spot_picked(a, 1.0) for a in ids[:10])
    assert not any(_spot_picked(a, 0.0) for a in ids[:10])


# ---------------------------------------------------------------- 权重在线调整（doc/10 §5.4）

def test_revise_weights_new_spec_and_validation(store, tmp_path):
    """权重调整写成新 judge_fp 的期望并设为当前；≤100% 与可调条目校验；重复保存幂等。"""
    ingest(store, tmp_path, "demo", [mk_record()])
    q = store.questions()[0]
    old_fp = q["current_judge_fp"]
    spec0 = store.spec_of(q["qkey"], old_fp)
    assert all("weight" not in f for f in spec0["facts"])    # 原始未写权重

    res = store.revise_weights(q["qkey"], old_fp, {"F1": 40})
    assert res["changed"] and res["judge_fp"] != old_fp
    assert store.current_spec(q["qkey"])[0] == res["judge_fp"]
    f1 = next(f for f in store.spec_of(q["qkey"], res["judge_fp"])["facts"] if f["id"] == "F1")
    assert f1["weight"] == 40
    assert store.spec_of(q["qkey"], res["judge_fp"])["weight_percent"] is True  # 百分制标记（回显不归一）
    assert "rejudge" in res["rejudge"]

    with pytest.raises(StoreError):                          # 超 100%
        store.revise_weights(q["qkey"], res["judge_fp"], {"F1": 101})
    with pytest.raises(StoreError):                          # 禁止事实不可调（否决项）
        store.revise_weights(q["qkey"], res["judge_fp"], {"X1": 50})
    again = store.revise_weights(q["qkey"], res["judge_fp"], {"F1": 40})
    assert again["changed"] is False                          # 同值幂等


def test_migrate_weight_percent_backfills_marker(store, tmp_path):
    """v6 迁移：早期「权重调整」落库的期望（无标记）补 weight_percent，
    否则用例页把权值重新归一到 100%，未分配丢失、像没保存。"""
    ingest(store, tmp_path, "demo", [mk_record()])
    q = store.questions()[0]
    fp = store.revise_weights(q["qkey"], q["current_judge_fp"], {"F1": 40})["judge_fp"]
    legacy = store.spec_of(q["qkey"], fp)
    assert legacy["weight_percent"] is True                   # 新落库即带标记
    legacy = json.loads(json.dumps(legacy)); legacy.pop("weight_percent")
    with store.lock, store.conn:                             # 模拟修复前落库的旧结构
        store.conn.execute("UPDATE specs SET spec_json=? WHERE qkey=? AND judge_fp=?",
                           (json.dumps(legacy, ensure_ascii=False), q["qkey"], fp))
    assert not store.spec_of(q["qkey"], fp).get("weight_percent")
    from scoring.store import _migrate_weight_percent
    _migrate_weight_percent(store.conn)
    assert store.spec_of(q["qkey"], fp)["weight_percent"] is True
