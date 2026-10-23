"""评测库存储（改造方案 §6、§12 步骤 1/2 验收）：建表迁移、ingest 幂等、
题目指纹语义、当前期望管理、有效结论与作废改判。"""
import json

import pytest

from scoring.items import input_fp_of, judge_fp_of, qkey_of
from scoring.policy import policy_from_dict
from scoring.store import Store
from tests.helpers import make_run, mk_record

POLICY = policy_from_dict({})
POLICY_SHA = "p1"


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "eval.db")
    yield s
    s.close()


def ingest(store, tmp_path, name, records, snapshot=None, run_id=None):
    """落 run 目录并入库；目录只建一次，重复调用等价重复 ingest。"""
    from scoring.cli import ingest_run
    run_dir = tmp_path / name
    if not run_dir.exists():
        run_dir = make_run(tmp_path, name, records, snapshot=snapshot, run_id=run_id)
    return ingest_run(store, run_dir)


# ---------------------------------------------------------------- 建表与迁移

def test_schema_version(store):
    v = store.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    assert v["value"] == "1"
    # 重复打开不重复迁移
    s2 = Store(store.path)
    assert s2.conn.execute("SELECT COUNT(*) c FROM meta").fetchone()["c"] == 1
    s2.close()


# ---------------------------------------------------------------- ingest（步骤 1 验收）

def test_ingest_idempotent(store, tmp_path):
    records = [mk_record(), mk_record(uid="ec-2#0")]
    run_id, n = ingest(store, tmp_path, "demo", records)
    assert (run_id, n) == ("demo", 2)
    assert len(store.answers()) == 2
    first_seen = {q["qkey"]: q["first_seen"] for q in store.questions()}
    # 重复 ingest：答案数不变，题目 first_seen 不变
    ingest(store, tmp_path, "demo", records)
    assert len(store.answers()) == 2
    assert {q["qkey"]: q["first_seen"] for q in store.questions()} == first_seen
    # 答案字段齐备
    a = store.answers()[0]
    assert a["mode"] == "natural" and a["trial"] == 1 and a["record_json"]
    assert a["judge_fp_at_run"] == judge_fp_of(records[0]["case"])
    assert a["qkey"] == qkey_of(records[0]["case"])


def test_ingest_registers_at_run_spec(store, tmp_path):
    """答案入库时登记执行时所用的期望（§5.1）。"""
    records = [mk_record()]
    ingest(store, tmp_path, "demo", records)
    a = store.answers()[0]
    spec = store.spec_of(a["qkey"], a["judge_fp_at_run"])
    assert spec is not None and spec["uid"] == "ec-1#0"


# ---------------------------------------------------------------- 指纹语义（步骤 2 验收）

def case_of(**over):
    base = dict(mk_record()["case"])
    base.update(over)
    return base


def test_fingerprint_reference_note_and_tag_changes_do_not_matter():
    c1 = case_of(reference_answer="旧参考", note="旧备注", tags=["a"], status="structured")
    c2 = case_of(reference_answer="新参考", note="新备注", tags=["b"], status="review")
    assert qkey_of(c1) == qkey_of(c2)
    assert judge_fp_of(c1) == judge_fp_of(c2)


def test_fingerprint_fact_text_changes_judge_fp_only():
    c1 = case_of()
    c2 = case_of(facts=[dict(c1["facts"][0], text="改写后的事实文本"),
                        dict(c1["facts"][1], text="未收尾款也可以先电放")])
    assert judge_fp_of(c1) != judge_fp_of(c2)
    assert qkey_of(c1) == qkey_of(c2)            # 输入没变 → 同一题


def test_fingerprint_user_message_changes_qkey():
    c1 = case_of(turns=[{"role": "user", "content": "问题一"}])
    c2 = case_of(turns=[{"role": "user", "content": "问题一改写"}])
    assert qkey_of(c1) != qkey_of(c2)
    assert input_fp_of(c1) != input_fp_of(c2)
    assert judge_fp_of(c1) == judge_fp_of(c2)


def test_fingerprint_injection_changes_qkey():
    c1 = case_of(injections=[{"id": "J1", "point": "电放时机", "option": "反转", "rev": 1,
                              "file": "a.md", "key": "k", "value": "v"}])
    c2 = case_of(injections=[{"id": "J1", "point": "电放时机", "option": "反转", "rev": 2,
                              "file": "a.md", "key": "k", "value": "v2"}])
    assert qkey_of(c1) != qkey_of(c2)


# ---------------------------------------------------------------- 当前期望（§5.1）

def dist_rows(version, text="未收尾款不应电放"):
    case = case_of()
    return [{**case, "evalset_version": version, "facts": [
        {"id": "F1", "kind": "required", "text": text, "keywords": [], "turn": None,
         "group": None, "evidence": None},
        case["facts"][1]]}]


def test_specs_import_sets_current_and_active(store, tmp_path):
    ingest(store, tmp_path, "demo", [mk_record()])
    store.import_evalset(dist_rows("v1"), "v1")
    q = store.questions()[0]
    assert q["active"] == 1
    cur = store.current_spec(q["qkey"])
    assert cur is not None and cur[1]["uid"] == "ec-1#0"
    # v2 改了事实文本 → 新 judge_fp 成为当前期望；旧期望保留
    #（同 judge_fp 的期望以先登记者为准：v1 与执行时期望判定字段相同，不另存一份）
    store.import_evalset(dist_rows("v2", text="改写后的事实"), "v2")
    cur2 = store.current_spec(q["qkey"])
    assert cur2[0] != cur[0]
    assert cur2[1]["evalset_version"] == "v2"
    assert store.spec_of(q["qkey"], cur[0]) is not None
    # set-current 切回 v1
    assert store.set_current("v1") == 1
    assert store.current_spec(q["qkey"])[0] == cur[0]


def test_missing_question_marked_inactive(store, tmp_path):
    ingest(store, tmp_path, "demo", [mk_record()])
    store.import_evalset(dist_rows("v1"), "v1")
    # 新版本里没有这道题 → 保留最后期望但标记非活跃
    store.import_evalset([dict(case_of(uid="other#0", id="other", case_id="other"),
                               evalset_version="v2")], "v2")
    q = next(x for x in store.questions() if x["uid"] == "ec-1#0")
    assert q["active"] == 0 and q["current_judge_fp"] is not None


# ---------------------------------------------------------------- 有效结论与改判（§6.3）

def _write_one_verdict(store, answer_id, judge_fp, chain_sha, item_id="F1", kind="required",
                       value="未提及", item_sha="is1", answer_sha="as1"):
    store.write_verdicts(answer_id, judge_fp, chain_sha, [{
        "schema": 1, "run_id": "r", "episode_id": "e", "uid": "u",
        "item": {"kind": kind, "id": item_id, "turn": None},
        "machine": {"value": value, "source": "judge", "operator": "f@1", "reason": ""},
        "trace": [], "answer_sha": answer_sha, "item_sha": item_sha, "status": "ok"}])


def test_effective_review_requires_sha_match_and_scale(store, tmp_path):
    records = [mk_record()]
    ingest(store, tmp_path, "demo", records)
    a = store.answers()[0]
    fp = a["judge_fp_at_run"]
    _write_one_verdict(store, a["answer_id"], fp, "c1")
    # item_sha 匹配 + 取值合法 → 生效
    store.add_review(a["answer_id"], "F1", "对", "r", "codfish", "is1", "as1")
    eff = store.effective(store.verdicts(a["answer_id"], fp, "c1")[0], store.reviews_for(a["answer_id"]))
    assert eff.value == "对" and eff.source == "human"
    # item_sha 不匹配（期望修订改了条目文本）→ 作废
    store.add_review(a["answer_id"], "F1", "对", "", "codfish", "is2", "as1")
    vrow = store.verdicts(a["answer_id"], fp, "c1")[0]
    vrow = dict(vrow, item_sha="is2")
    # 原条目（is1）视角：is2 的改判不生效
    vrow_is1 = dict(vrow, item_sha="is1")
    eff2 = store.effective(vrow_is1, store.reviews_for(a["answer_id"]))
    assert eff2.source == "human" and eff2.value == "对"      # is1 仍被第一条改判覆盖
    # 取值不在 scale → 作废
    store.add_review(a["answer_id"], "F1", "满足", "", "codfish", "is1", "as1")
    eff3 = store.effective(vrow_is1, store.reviews_for(a["answer_id"]))
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


# ---------------------------------------------------------------- rescore 不调算子（步骤 3 验收之一）

def test_rescore_does_not_touch_verdicts(store, tmp_path):
    records = [mk_record()]
    ingest(store, tmp_path, "demo", records)
    a = store.answers()[0]
    fp = a["judge_fp_at_run"]
    store.import_evalset([{**records[0]["case"], "evalset_version": "v1"}], "v1")
    _write_one_verdict(store, a["answer_id"], store.current_spec(a["qkey"])[0], "c1")
    n = store.rescore(POLICY, POLICY_SHA, current_chain_sha="c1")
    assert n >= 1
    c = store.conclusion(a["answer_id"], store.current_spec(a["qkey"])[0], "c1", POLICY_SHA)
    assert c["status"] == "fail"                  # F1 未提及 → 失败（未调任何算子）
    # 运行错误的答案（无判定行）也物化结论
    records2 = [mk_record(status="error", failed_turn=1, uid="ec-9#0")]
    ingest(store, tmp_path, "demo2", records2)
    a2 = store.answers(run_id="demo2")[0]
    store.import_evalset([{**records2[0]["case"], "evalset_version": "v1"}], "v1")
    store.rescore(POLICY, POLICY_SHA, current_chain_sha="c1")
    fp2 = store.current_spec(a2["qkey"])[0]
    assert store.conclusion(a2["answer_id"], fp2, "c1", POLICY_SHA)["status"] == "error"


# ---------------------------------------------------------------- 备份

def test_backup(store, tmp_path):
    ingest(store, tmp_path, "demo", [mk_record()])
    target = store.backup(tmp_path / "backup.db")
    s2 = Store(target)
    assert len(s2.answers()) == 1
    s2.close()
