"""复核服务集成测试（改造方案 §8、整改清单 R2/R7 验收）：评测库取数、
改判后队列即时变化、页面数字与导出报告一致、合成大库下全页面响应时间。"""
import json
import re
import time
from urllib.parse import quote

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from scoring import cli  # noqa: E402
from scoring.chain import default_operators_path, load_operator_config  # noqa: E402
from scoring.policy import default_policy_path, load_policy  # noqa: E402
from scoring.serve import create_app  # noqa: E402
from scoring.store import Store  # noqa: E402
from tests.helpers import make_run, mk_record  # noqa: E402
from tests.test_cli import OPERATORS_YAML, dist_jsonl  # noqa: E402


@pytest.fixture()
def served(tmp_path):
    """全部 run 用同一套判定齐全的算子（矩阵/改判/比较/导出测试）。"""
    db = str(tmp_path / "eval.db")
    ops = tmp_path / "operators.yaml"
    ops.write_text(OPERATORS_YAML, encoding="utf-8")
    records = [mk_record(), mk_record(uid="ec-2#0")]
    run_dir = make_run(tmp_path, "demo", records)
    assert cli.main(["specs", "import", dist_jsonl(tmp_path), "--db", db]) == 0
    assert cli.main(["score", str(run_dir), "--operators", str(ops), "--db", db]) == 0
    app = create_app(db, operators_path=str(ops))
    return TestClient(app), db, tmp_path, str(ops)


@pytest.fixture()
def served_plain(tmp_path):
    """包内默认算子（forbidden/precond 链为空）→ 未决条目进复核队列。"""
    db = str(tmp_path / "eval2.db")
    run_dir = make_run(tmp_path, "plain", [mk_record()], run_id="plain")
    assert cli.main(["specs", "import", dist_jsonl(tmp_path), "--db", db]) == 0
    assert cli.main(["score", str(run_dir), "--db", db]) == 0
    app = create_app(db)
    return TestClient(app), db


def test_question_list_shows_matrix_and_stats(served):
    client, _, _, _ = served
    r = client.get("/")
    assert r.status_code == 200
    assert "ec-1#0" in r.text and "题目列表" in r.text
    assert "错误知识纠正|natural" in r.text            # 顶部矩阵（全部已判定）
    assert "0/2" in r.text                             # demo 两段会话均 fail


def test_question_view_expectation_and_answers(served):
    client, db, _, _ = served
    s = Store(db)
    qkey = s.questions()[0]["qkey"]
    s.close()
    r = client.get(f"/question/{quote(qkey)}")
    assert r.status_code == 200
    assert "题目期望" in r.text and "未收尾款不应电放" in r.text
    assert "未提及" in r.text                            # F1 机器判"未提及"的条目芯片


def test_review_submission_transaction(served):
    """改判：校验失败不落盘；成功后该答案结论行更新、不一致队列出现该条。"""
    client, db, _, _ = served
    s = Store(db)
    a = s.answers()[0]
    fp = s.current_spec(a["qkey"])[0]
    chain = s.conn.execute("SELECT chain_sha FROM verdicts LIMIT 1").fetchone()["chain_sha"]
    s.close()
    bad = client.post("/api/review", json={"answer_id": a["answer_id"], "item_id": "NOPE",
                                           "value": "对", "reviewer": "codfish"})
    assert bad.status_code == 400
    s = Store(db)
    assert s.conn.execute("SELECT COUNT(*) c FROM reviews").fetchone()["c"] == 0
    s.close()
    ok = client.post("/api/review", json={"answer_id": a["answer_id"], "item_id": "F1",
                                          "value": "对", "reason": "回答明确", "reviewer": "codfish"})
    assert ok.status_code == 200 and ok.json()["episode_status"] == "pass"
    s = Store(db)
    policy_sha = load_policy(default_policy_path())[1]
    assert s.conclusion(a["answer_id"], fp, chain, policy_sha)["status"] == "pass"
    s.close()
    qm = client.get("/queue?filter_name=mismatch").text
    assert "F1" in qm
    qf = client.get("/queue?filter_name=fail").text
    assert "ec-2#0" in qf


def test_undecided_queue_shrinks_after_review(served_plain):
    """R2：改判一条未决条目 → 未决队列总数减 1，该条不再出现。"""
    client, db = served_plain
    s = Store(db)
    a = s.answers()[0]
    total0 = s.undecided_items(load_operator_config(default_operators_path()).chain_sha)[1]
    assert total0 == 2                                 # F1（关键词链弃权）+ X1（链为空）
    s.close()
    ok = client.post("/api/review", json={"answer_id": a["answer_id"], "item_id": "F1",
                                          "value": "对", "reason": "回答明确", "reviewer": "codfish"})
    assert ok.status_code == 200
    q = client.get("/queue?filter_name=undecided").text
    assert "共 1 条" in q and "X1" in q and ">F1<" not in q.replace("item-chip", "")
    s = Store(db)
    assert s.undecided_items(load_operator_config(default_operators_path()).chain_sha)[1] == 1
    s.close()


def test_overview_matches_export_report(served, tmp_path):
    """页面数字与 export 产出的 scoring.json 同源一致。"""
    client, db, _, ops = served
    out = tmp_path / "exp"
    assert cli.main(["export", "--db", db, "--operators", ops, "--out", str(out)]) == 0
    report = json.loads((out / "scoring.json").read_text(encoding="utf-8"))
    cell = report["views"]["ability"]["错误知识纠正|natural"]
    page = client.get("/").text
    assert f"{cell['pass']}/{cell['n']}" in page


def test_compare_axis_defaults_and_block(served):
    """R3：模型漂移放行并提示；mode 不一致仍拦截。"""
    client, db, tmp_path, ops = served
    run2 = make_run(tmp_path, "demo2", [mk_record()], run_id="demo2",
                    snapshot={"skills_mode": "default-roots", "evalset": {"sha256": "a"},
                              "models_observed": ["glm-4"]})
    assert cli.main(["score", str(run2), "--operators", ops, "--db", db]) == 0
    drift = client.get("/compare?axis=profile").text    # 同 profile("?") 两个模型 → 漂移提示不拦截
    assert "已拒绝比较" not in drift
    run3 = make_run(tmp_path, "demo3", [mk_record(mode="forced")], run_id="demo3",
                    snapshot={"skills_mode": "default-roots", "evalset": {"sha256": "a"}})
    assert cli.main(["score", str(run3), "--operators", ops, "--db", db]) == 0
    blocked = client.get("/compare?axis=profile&any=model").text
    assert "已拒绝比较" in blocked and "mode" in blocked
    ok = client.get("/compare?axis=profile&any=model&fix=mode=natural").text
    assert "已拒绝比较" not in ok


def test_quality_page(served):
    client, _, _, _ = served
    r = client.get("/quality")
    assert r.status_code == 200
    assert "变体泄露嫌疑" in r.text and "作废的人工改判" in r.text


def test_group_page(served):
    client, db, _, _ = served
    s = Store(db)
    case_id = s.questions()[0]["case_id"]
    s.close()
    r = client.get(f"/group/{quote(case_id)}")
    assert r.status_code == 200 and "ec-1#0" in r.text


# ---------------------------------------------------------------- 大库性能（R7 验收）

def _build_big_db(db_path, n_q=1000, n_per=10):
    """1 万答案 / 10 万判定：三分之一未决、三分之一失败；1000 条人工改判
    （其中约 1000 条与机器不一致）。结论行用真实的 chain_sha / policy_sha。"""
    chain = load_operator_config(default_operators_path()).chain_sha
    policy_sha = load_policy(default_policy_path())[1]
    facts = [{"id": f"F{i}", "kind": "required", "text": f"事实{i}", "keywords": [], "turn": 1,
              "group": None, "evidence": None} for i in range(1, 11)]
    store = Store(db_path)
    conn = store.conn
    with conn:
        conn.execute("INSERT INTO runs VALUES('big','/x','v1','p1','{}','2026-01-01','2026-01-01')")
        questions, answers, verdicts, conclusions, reviews = [], [], [], [], []
        for qi in range(n_q):
            qkey, fp = f"q{qi}|fp{qi}", f"fp{qi}"
            uid, case_id = f"case{qi}#0", f"case{qi}"
            case = {"uid": uid, "id": case_id, "case_id": case_id, "dim": "知识召回", "facts": facts}
            record = {"uid": uid, "mode": "natural", "trial": 1, "status": "ok",
                      "turns": [{"index": 1, "source": "live", "input": "q", "output": "答",
                                 "status": "ok", "observations": {}}], "case": case}
            questions.append((qkey, uid, case_id, 0, None, "知识召回", "t", "[]", fp, fp, 1, "2026-01-01"))
            conn.execute("INSERT OR IGNORE INTO specs VALUES(?,?,?,'v1','2026-01-01')",
                         (qkey, fp, json.dumps(case, ensure_ascii=False)))
            rec_json = json.dumps(record, ensure_ascii=False)
            for ai in range(n_per):
                ep = f"{uid}-natural-t{ai + 1}"
                a_id = f"big/{ep}"
                kind = ai % 3                                  # 0 未决 / 1 失败 / 2 通过
                answers.append((a_id, qkey, "big", ep, fp, "p1", '["glm-5.3"]',
                                "natural", ai + 1, "default-roots", "s1", "v1", "ok", rec_json, 0, "[]"))
                status = "undecided" if kind == 0 else "pass"
                concl_status = {"0": "undecided", "1": "fail", "2": "pass"}[str(kind)]
                conclusions.append((a_id, fp, chain, policy_sha, concl_status, "", "{}"))
                for fi, f in enumerate(facts):                 # 10 条判定/答案 → 10 万条
                    if kind == 0 and fi == 0:
                        v, st, src = None, "undecided", ""
                    elif kind == 1:
                        v, st, src = "错", "ok", "rule"
                    else:
                        v, st, src = "对", "ok", "rule"
                    verdicts.append((a_id, fp, chain, f["id"], "required", 1, f"sha{qi}",
                                     "asha", v, src, "keyword@1", "", "[]", st, "2026-01-01"))
                if kind == 1 and ai == 1 and qi < 1000:        # 人工与机器不一致的改判
                    reviews.append((a_id, "F1", f"sha{qi}", "asha", "对", "回答明确", "codfish",
                                    "2026-01-01", None))
        conn.executemany("INSERT OR REPLACE INTO questions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", questions)
        conn.executemany("INSERT OR REPLACE INTO answers VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", answers)
        conn.executemany("INSERT OR REPLACE INTO verdicts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", verdicts)
        conn.executemany("INSERT OR REPLACE INTO conclusions VALUES(?,?,?,?,?,?,?)", conclusions)
        conn.executemany("INSERT INTO reviews(answer_id, item_id, item_sha, answer_sha, value, reason,"
                         " reviewer, at, legacy_key) VALUES(?,?,?,?,?,?,?,?,?)", reviews)
    store.close()
    return chain, policy_sha


def test_synthetic_scale_performance(tmp_path):
    """1 万答案 / 10 万判定（含未决/失败/改判分布）：全部页面响应 < 1s 且有数据。"""
    db_path = tmp_path / "big.db"
    _build_big_db(db_path)

    client = TestClient(create_app(str(db_path)))
    pages = [
        "/", f"/question/{quote('q0|fp0')}",
        "/queue?filter_name=undecided", "/queue?filter_name=fail",
        "/queue?filter_name=mismatch", "/queue?filter_name=pending",
        "/compare?axis=run", "/quality", "/group/case0",
    ]
    for path in pages:
        t0 = time.time()
        r = client.get(path)
        elapsed = time.time() - t0
        assert r.status_code == 200, path
        assert elapsed < 1.0, f"{path} 响应 {elapsed:.2f}s 超过 1s"
    # 队列确实有数据，不是空跑（R7 验收 4）
    # 分布：每 10 个答案中 ai%3==0 → 4 个未决（各 1 条未决条目）、ai%3==1 → 3 个失败
    qu = client.get("/queue?filter_name=undecided").text
    assert "共 4000 条" in qu
    qm = client.get("/queue?filter_name=mismatch").text
    assert "共 1000 条" in qm
    qf = client.get("/queue?filter_name=fail").text
    assert "共 3000 条" in qf
    quality = client.get("/quality").text
    assert "0/1000" in quality                            # 规则一致率：改判与机器全不一致
