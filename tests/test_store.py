"""评测库存储（改造方案 §6、整改清单 R1/R2/R5/R8/R9 验收）：建表迁移、
ingest 幂等、当前期望管理、有效结论与作废改判、未决队列、run 顺序。"""
import json

import pytest

from scoring.items import input_fp_of, standard_fp_of
from scoring.rules import rules_from_dict
from scoring.store import Store, StoreError
from tests.helpers import make_run, mk_record

POLICY = rules_from_dict({})
POLICY_SHA = "p1"


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "eval.db")
    yield s
    s.close()


def ingest(store, tmp_path, name, records, snapshot=None, run_id=None, manifest=None):
    """落 run 目录并入库；目录只建一次，重复调用等价重复 ingest。
    manifest 为并入 run.json 顶层的键（如 title / profile），可多次 ingest 传不同值。"""
    from scoring.cli import ingest_run
    run_dir = tmp_path / name
    if not run_dir.exists():
        run_dir = make_run(tmp_path, name, records, snapshot=snapshot, run_id=run_id)
    if manifest:
        m = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        m.update(manifest)
        (run_dir / "run.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    return ingest_run(store, run_dir)


# ---------------------------------------------------------------- 建表与迁移

def test_schema_version(store):
    v = store.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    assert v["value"] == "9"
    # spec_versions 已回填
    assert store.conn.execute("SELECT COUNT(*) c FROM spec_versions").fetchone()["c"] >= 0
    # 重复打开不重复迁移（meta 行数稳定：v9 起含 current_snapshot/rules_sha 等固定键）
    s2 = Store(store.path)
    n1 = store.conn.execute("SELECT COUNT(*) c FROM meta").fetchone()["c"]
    n2 = s2.conn.execute("SELECT COUNT(*) c FROM meta").fetchone()["c"]
    assert n1 == n2
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
    assert a["judge_fp_at_run"] == standard_fp_of(records[0]["case"])
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
    assert q["active"] == 1 and q["current_standard_fp"] == standard_fp_of(mk_record()["case"])
    # 已有当前期望的题目（specs import 过）不被 run 的执行时期望覆盖
    store.import_evalset(_dist_rows("v2", text="改写后的事实文本"), "v2")
    ingest(store, tmp_path, "demo2", [mk_record()])
    q2 = next(x for x in store.questions() if x["uid"] == "ec-1#0")
    assert q2["current_standard_fp"] == standard_fp_of(_dist_rows("v2", text="改写后的事实文本")[0])


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
    fp_v1 = standard_fp_of(_dist_rows("v1")[0])
    assert store.current_spec(store.questions()[0]["qkey"])[0] == fp_v1
    store.import_evalset(_dist_rows("v2", ref="新参考"), "v2")     # 判定指纹相同
    assert store.current_spec(store.questions()[0]["qkey"])[0] == fp_v1   # 内容寻址，期望不变
    n = store.set_current("v1")
    q = store.questions()[0]
    assert n == 1 and q["active"] == 1 and q["current_standard_fp"] == fp_v1


def test_set_current_rolls_back_changed_spec(store, tmp_path):
    """R1：v2 改了事实文本（判定指纹变），set-current v1 回到 v1 的指纹。"""
    ingest(store, tmp_path, "demo", [mk_record()])
    store.import_evalset(_dist_rows("v1"), "v1")
    store.import_evalset(_dist_rows("v2", text="改写后的事实文本"), "v2")
    fp_v1 = standard_fp_of(_dist_rows("v1")[0])
    assert store.current_spec(store.questions()[0]["qkey"])[0] != fp_v1
    assert store.set_current("v1") == 1
    assert store.current_spec(store.questions()[0]["qkey"])[0] == fp_v1


def test_set_current_unknown_version_is_error(store, tmp_path):
    """R1：未知版本 → 报错，题目状态与当前期望不变。"""
    ingest(store, tmp_path, "demo", [mk_record()])
    store.import_evalset(_dist_rows("v1"), "v1")
    before = {q["qkey"]: (q["active"], q["current_standard_fp"]) for q in store.questions()}
    with pytest.raises(StoreError, match="未知评测集版本"):
        store.set_current("no-such-version")
    assert {q["qkey"]: (q["active"], q["current_standard_fp"]) for q in store.questions()} == before


def test_import_same_version_different_content_rejected(store):
    """dist 不进 git 后版本可能重号：同版本内容不同必须报错，不能覆盖旧快照。"""
    store.import_evalset(_dist_rows("v1"), "v1")
    before = store.snapshot("v1")["snapshot_sha"]
    with pytest.raises(StoreError, match="已存在且内容不同"):
        store.import_evalset(_dist_rows("v1", text="改写后的事实文本"), "v1")
    assert store.snapshot("v1")["snapshot_sha"] == before
    store.import_evalset(_dist_rows("v1"), "v1")      # 同内容重复导入照常刷新


def test_import_records_actual_include(store):
    """include 记录实际收录范围（不是固定 admitted）。"""
    store.import_evalset(_dist_rows("v1"), "v1", include="structured,reviewed,admitted")
    assert store.snapshot("v1")["include"] == "structured,reviewed,admitted"


def test_snapshot_sha_from_single_algorithm(store):
    """冲突检查与登记共用 _snapshot_parts：两处算出的 sha 必须一致，同内容重复导入
    不能被误判为冲突。旧 dist 无 scoring 块（登记时走库内兜底读）是最易分叉的路径。"""
    from scoring.rules import canonical_sha
    rows_no_scoring = [{k: v for k, v in _dist_rows("v2")[0].items() if k != "scoring"}]
    for version, rows in (("v1", _dist_rows("v1")), ("v2", rows_no_scoring)):
        payload = store._snapshot_parts(rows)
        _, method_sha = store._load_method(None)
        expected = canonical_sha({"rows": payload[0], "scoring": payload[1],
                                  "method_sha": method_sha})
        store.import_evalset(rows, version)
        assert store.snapshot(version)["snapshot_sha"] == expected
        store.import_evalset(rows, version)      # 同内容重复导入照常通过（不误判冲突）


def test_snapshot_use_requires_registered_snapshot(store):
    """快照未登记时不再按库内最新配置兜底重建 sha（会与导入口径分叉）：
    snapshot_use 直接报错，要求重新导入该版本。"""
    store.import_evalset(_dist_rows("v1"), "v1")
    store.conn.execute("DELETE FROM snapshots")
    with pytest.raises(StoreError, match="需重新导入"):
        store.snapshot_use("v1")
    store.import_evalset(_dist_rows("v1"), "v1")      # 重新导入后恢复正常
    assert store.snapshot_use("v1") == 1


def test_missing_question_marked_inactive(store, tmp_path):
    ingest(store, tmp_path, "demo", [mk_record()])
    store.import_evalset(_dist_rows("v1"), "v1")
    store.import_evalset([dict(dict(mk_record()["case"], uid="other#0", id="other", case_id="other"),
                               evalset_version="v2")], "v2")
    q = next(x for x in store.questions() if x["uid"] == "ec-1#0")
    assert q["active"] == 0 and q["current_standard_fp"] is not None


# ---------------------------------------------------------------- 有效结论与改判（§6.3）

def _write_one_verdict(store, answer_id, standard_fp, chain_sha, item_id="F1", kind="required",
                       value="未提及", item_sha="is1", answer_sha="as1", status="ok"):
    store.write_verdicts(answer_id, standard_fp, chain_sha, [{
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

def _write_verdicts(store, answer_id, standard_fp, chain_sha, specs):
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
    store.write_verdicts(answer_id, standard_fp, chain_sha, rows)


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


# ---------------------------------------------------------------- 旧库拒绝打开（doc/17 决定 15）

def test_old_schema_db_refused(tmp_path):
    """v9 从空库起步：schema < 9 的旧库直接拒绝打开（迁移链已删除）。"""
    import sqlite3
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO meta VALUES('schema_version', '8');
        CREATE TABLE questions(qkey TEXT PRIMARY KEY);
    """)
    conn.commit()
    conn.close()
    with pytest.raises(StoreError, match="旧版评测库"):
        Store(db)


def test_fresh_db_schema_v9(tmp_path):
    """空库起步即 v9：case_scoring / temp_weights 存在，conclusions 键为 scoring_fp。"""
    s = Store(tmp_path / "fresh.db")
    tables = {r["name"] for r in s.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"case_scoring", "temp_weights"} <= tables
    assert "item_weights" not in tables
    cols = {r["name"] for r in s.conn.execute("PRAGMA table_info(conclusions)")}
    assert "scoring_fp" in cols and "weight_fp" not in cols
    qcols = {r["name"] for r in s.conn.execute("PRAGMA table_info(questions)")}
    assert "current_scoring_fp" in qcols and "current_weight_fp" not in qcols
    s.close()


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
    case.pop("scoring", None)          # 让 mk_record 按新 facts 重算计分块（2:1）
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
    # 期望修订（standard_fp 变）后人工会话分作废
    store.conn.execute("UPDATE questions SET current_standard_fp='fp-new' WHERE qkey=?", (a["qkey"],))
    store.conn.execute("INSERT INTO specs SELECT qkey, 'fp-new', spec_json, evalset_version, imported_at"
                       " FROM specs WHERE qkey=? AND standard_fp=?", (a["qkey"], fp))
    voided = store.voided_reviews(a["answer_id"])
    assert any(v["review"]["item_id"] == "@total" and "标准已修订" in v["reason"] for v in voided)


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



# ---------------------------------------------------------------- 计分配置与临时权重（doc/17 §3/§5.2）

def test_scoring_plan_md_and_temp_calibers(store, tmp_path):
    """双口径（doc/17 §5.2）：md 口径来自 case_scoring（导入/入库落库）；临时权重只
    覆盖权重、不覆盖用途；两种口径 scoring_fp 不同、结论可并存；用途进指纹。"""
    a, fp = _scored_answer(store, tmp_path)                    # F1(2)/E1(1)/X1，用例 ec-1
    plan, sfp_md, is_temp = store.scoring_plan("ec-1")
    assert is_temp is False
    assert plan["uses"] == {"F1": "score", "E1": "score", "X1": "ref"}
    assert abs(plan["weights"]["F1"] - 200 / 3) < 1e-3          # 2:1 归一（4 位小数）

    store.recompute_conclusion(a["answer_id"], fp, "c1", POLICY, POLICY_SHA)
    assert store.conclusion(a["answer_id"], fp, "c1", POLICY_SHA)["status"] == "undecided"

    store.set_temp_weights("ec-1", {"F1": 60, "E1": 30})        # 名义 2:1 → 归一
    plan2, sfp_temp, is_temp2 = store.scoring_plan("ec-1")
    assert is_temp2 is True and sfp_temp != sfp_md
    assert plan2["uses"] == plan["uses"]                        # 用途仍来自 md
    assert abs(plan2["weights"]["F1"] - 200 / 3) < 1e-3 and abs(plan2["weights"]["E1"] - 100 / 3) < 1e-3
    store.rescore_cases(["ec-1"], POLICY, POLICY_SHA)
    # 两种口径的结论同时存在
    assert store.conclusion(a["answer_id"], fp, "c1", POLICY_SHA, sfp_md) is not None
    assert store.conclusion(a["answer_id"], fp, "c1", POLICY_SHA, sfp_temp) is not None

    store.temp_weights_use("ec-1", False)                       # 切回 md 口径
    plan3, sfp3, is_temp3 = store.scoring_plan("ec-1")
    assert is_temp3 is False and sfp3 == sfp_md
    assert store.temp_weights("ec-1")["unknown"] == []          # 条目一致
    store.clear_temp_weights("ec-1")
    assert store.temp_weights("ec-1") is None
    _, sfp4, _ = store.scoring_plan("ec-1")
    assert sfp4 == sfp_md


def test_temp_weights_group_normalization_matches_md(store, tmp_path):
    """P2 回归：临时权重按「组 = 一个计分单位」归一，组内成员得到同一百分比，
    与 md 编译口径一致。"""
    from tests.helpers import fact
    case = dict(mk_record()["case"])
    case.pop("scoring", None)
    case["facts"] = [fact("F1", text="甲", group="G1"), fact("F2", text="乙", group="G1"),
                     fact("E1", "expected", "丙")]
    ingest(store, tmp_path, "demo", [mk_record(case=case)])
    cid = store.questions()[0]["case_id"]
    store.set_temp_weights(cid, {"F1": 30, "F2": 30, "E1": 40})
    tw = store.temp_weights(cid)["weights"]
    # 组内取最大 30 作为一个单位，加 E1 40 → 30/70、30/70、40/70
    assert tw["F1"] == tw["F2"] == pytest.approx(42.8571)
    assert tw["E1"] == pytest.approx(57.1429)
    plan, _, _ = store.scoring_plan(cid)
    assert plan["weights"]["F1"] == plan["weights"]["F2"] == pytest.approx(42.8571)


def test_case_scoring_imported_at_only_on_change(store, tmp_path):
    """P2 回归：weights_touched_after 依赖 imported_at，内容未变的重复导入不得刷新它。"""
    ingest(store, tmp_path, "demo", [mk_record()])
    cid = "ec-1"
    block = store.case_scoring(cid)
    with store.lock, store.conn:
        store.conn.execute("UPDATE case_scoring SET imported_at='2000-01-01T00:00:00' WHERE case_id=?",
                           (cid,))
    store._register_case_scoring(cid, block)                    # 同内容
    assert store.conn.execute("SELECT imported_at FROM case_scoring WHERE case_id=?",
                              (cid,)).fetchone()["imported_at"] == "2000-01-01T00:00:00"
    changed = {**block, "items": {**block.get("items", {}), "E9": {"use": "score", "weight": 1.0}}}
    store._register_case_scoring(cid, changed)                  # 内容变化
    assert store.conn.execute("SELECT imported_at FROM case_scoring WHERE case_id=?",
                              (cid,)).fetchone()["imported_at"] != "2000-01-01T00:00:00"


def test_set_temp_weights_validation(store, tmp_path):
    """校验：只收计分条目（X1 默认参考不行）、0–100；保存即刷新缓存列。"""
    ingest(store, tmp_path, "demo", [mk_record()])
    q = store.questions()[0]
    with pytest.raises(StoreError):                             # 参考条目不可调
        store.set_temp_weights(q["case_id"], {"X1": 50})
    with pytest.raises(StoreError):                             # 越界
        store.set_temp_weights(q["case_id"], {"F1": 101})
    res = store.set_temp_weights(q["case_id"], {"F1": 100})
    assert res["in_use"] and store.questions()[0]["current_scoring_fp"] == res["scoring_fp"]
    assert store.current_spec(q["qkey"])[0] == q["current_standard_fp"]   # 不产生新标准


def test_weights_touched_after_total(store, tmp_path):
    """C4：人工总分之后改计分配置 → weights_touched_after 为真（页面提示），人工总分本身仍有效。"""
    a, fp = _scored_answer(store, tmp_path)
    store.submit_reviews(a["answer_id"], [{"item_id": "F1", "value": "未提及"}],
                         {"score": 65, "reason": "到位"}, "codfish", fp, "c1", POLICY, POLICY_SHA)
    assert store.total_override(store.reviews_for(a["answer_id"]), fp,
                                store._conversation_sha(a, fp))["score"] == 65
    assert store.weights_touched_after(a["answer_id"]) is False
    with store.lock, store.conn:      # 回拨 1 秒，避开同秒比较的时间粒度
        store.conn.execute("UPDATE reviews SET at='2000-01-01T00:00:00'"
                           " WHERE answer_id=? AND item_id='@total'", (a["answer_id"],))
    cid = store.question(a["qkey"])["case_id"]
    store.set_temp_weights(cid, {"F1": 80, "E1": 20})
    assert store.weights_touched_after(a["answer_id"]) is True
    # 人工总分不因计分配置作废（标准与回答未变）
    assert store.total_override(store.reviews_for(a["answer_id"]), fp,
                                store._conversation_sha(a, fp))["score"] == 65


def test_unassigned_case_skipped(store, tmp_path):
    """doc/17 §4：计分权重合计 0 → 会话结论 skipped，不进复核队列、不调算子；
    分配临时权重后恢复正常评测。"""
    case = mk_record()["case"]
    case["scoring"] = {"items": {"F1": {"use": "score", "weight": 0.0},
                                 "X1": {"use": "ref"}}, "unassigned": True}
    ingest(store, tmp_path, "demo", [mk_record(case=case)])
    a = store.answers()[0]
    fp = a["judge_fp_at_run"]
    _write_verdicts(store, a["answer_id"], fp, "c1", [
        dict(item_id="F1", value=None, status="undecided"),
        dict(item_id="X1", kind="forbidden", value=None, status="undecided")])
    store.rescore(POLICY, POLICY_SHA, current_chain_sha="c1")
    c = store.conclusion(a["answer_id"], fp, "c1", POLICY_SHA)
    assert c["status"] == "skipped" and "未分配" in c["reason"]
    assert store.undecided_items("c1")[1] == 0                  # 不进复核队列
    assert store.pending_rejudge("c1") == []                    # 不调算子
    cid = store.question(a["qkey"])["case_id"]
    store.set_temp_weights(cid, {"F1": 100})
    store.rescore(POLICY, POLICY_SHA, current_chain_sha="c1")
    c2 = store.conclusion(a["answer_id"], fp, "c1", POLICY_SHA)
    assert c2["status"] != "skipped"

# ---------------------------------------------------------------- 执行记录与判定配置命名（命名改造）

def test_run_title_manifest_default_and_rename(store, tmp_path):
    """runs.title：manifest.title 入库（无标题重导不冲掉已有标题）；默认标题由
    快照拼「评测集版本 · profile · modes×trials」；rename_run 改名/空白清除。"""
    snap = {"skills_mode": "default-roots", "evalset": {"version": "v20261026-1"},
            "modes": ["natural", "forced"], "trials": 2}
    prof = {"content": {"id": "eline-glm53-defaultroots@glm-5.3-low", "routing": {}}}
    ingest(store, tmp_path, "t1", [mk_record()])                    # make_run 缺省快照：无版本无 profile
    rid = store.runs()[0]["run_id"]
    assert store.runs()[0]["title"] is None
    assert store.run_labels()[rid] == "? · ?"

    ingest(store, tmp_path, "t1", [mk_record()],
           manifest={"title": " 基线-glm53low ", "profile": prof, "snapshot": snap})
    assert store.runs()[0]["title"] == "基线-glm53low"
    ingest(store, tmp_path, "t1", [mk_record()],
           manifest={"title": None, "profile": prof, "snapshot": snap})   # 无标题重导 → 保留
    assert store.runs()[0]["title"] == "基线-glm53low"
    assert store.run_labels()[rid] == "基线-glm53low"

    store.rename_run(rid, "修订验证-021")
    assert store.run_labels()[rid] == "修订验证-021"
    store.rename_run(rid, "   ")                                     # 空白 = 清除 → 回默认拼接
    assert store.run_labels()[rid] == "v20261026-1 · eline-glm53-defaultroots@glm-5.3-low · natural+forced×2"
    with pytest.raises(StoreError):
        store.rename_run("no-such", "x")


def test_rename_chain(store):
    """判定配置命名：注册 → 改名 → 空白清除（未命名）；chain_sha 不变；未知链报错。"""
    sha = "ab12cd34"
    store.register_chain(sha, "规则", ["keyword@1"])
    assert store.chains()[0]["name"] == "规则"
    store.rename_chain(sha, "规则+llm-v2")
    assert store.chains()[0]["name"] == "规则+llm-v2" and store.chains()[0]["chain_sha"] == sha
    store.rename_chain(sha, "")
    assert store.chains()[0]["name"] is None
    with pytest.raises(StoreError):
        store.rename_chain("no-such", "x")
