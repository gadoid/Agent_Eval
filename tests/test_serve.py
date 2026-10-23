"""复核服务集成测试（改造方案 §8、§12 步骤 5 验收）：评测库取数、改判事务、
页面数字与导出报告一致、未决队列、合成大库下的响应时间。"""
import json
import time
from urllib.parse import quote

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from scoring import cli  # noqa: E402
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


def test_overview_matches_export_report(served, tmp_path):
    """§12 步骤 5 验收：页面数字与 export 产出的 scoring.json 同源一致。"""
    client, db, _, ops = served
    out = tmp_path / "exp"
    assert cli.main(["export", "--db", db, "--operators", ops, "--out", str(out)]) == 0
    report = json.loads((out / "scoring.json").read_text(encoding="utf-8"))
    cell = report["views"]["ability"]["错误知识纠正|natural"]
    page = client.get("/").text
    assert f"{cell['pass']}/{cell['n']}" in page


def test_queue_undecided(served_plain):
    client, _ = served_plain
    q = client.get("/queue?filter_name=undecided").text
    assert "X1" in q                                     # 禁止事实链为空 → 未决进队列
    qp = client.get("/queue?filter_name=pending").text
    assert "队列为空" in qp                               # 全部已按当前期望判定


def test_compare_pages(served):
    client, db, tmp_path, ops = served
    run2 = make_run(tmp_path, "demo2", [mk_record()], run_id="demo2",
                    snapshot={"skills_mode": "default-roots", "evalset": {"sha256": "a"},
                              "models_observed": ["glm-4"]})
    assert cli.main(["score", str(run2), "--operators", ops, "--db", db]) == 0
    blocked = client.get("/compare?axis=profile").text   # 模型/run 不同且未声明不限 → 拒绝
    assert "已拒绝比较" in blocked
    ok = client.get("/compare?axis=profile&any=model,run").text
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


# ---------------------------------------------------------------- 大库性能（§12 步骤 5 验收）

def test_synthetic_scale_performance(tmp_path):
    """合成的 1 万答案 / 10 万判定下，题目列表、题目视图、复核队列响应 < 1s。"""
    db_path = tmp_path / "big.db"
    store = Store(db_path)
    conn = store.conn
    n_q, n_per = 1000, 10                                # 1000 题 × 10 答案 = 1 万答案
    facts = [{"id": f"F{i}", "kind": "required", "text": f"事实{i}", "keywords": [], "turn": 1,
              "group": None, "evidence": None} for i in range(1, 11)]
    with conn:
        conn.execute("INSERT INTO runs VALUES('big','/x','v1','p1','{}','2026-01-01')")
        questions, answers, verdicts, conclusions = [], [], [], []
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
                answers.append((f"big/{ep}", qkey, "big", ep, fp, "p1", '["glm-5.3"]',
                                "natural", ai + 1, "default-roots", "s1", "v1", "ok", rec_json))
                conclusions.append((f"big/{ep}", fp, "chain1", "pol", "pass", "", "{}"))
                for f in facts:                          # 10 条判定/答案 → 10 万条
                    verdicts.append((f"big/{ep}", fp, "chain1", f["id"], "required", 1, f"sha{qi}",
                                     "asha", "对", "rule", "keyword@1", "", "[]", "ok", "2026-01-01"))
        conn.executemany("INSERT OR REPLACE INTO questions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", questions)
        conn.executemany("INSERT OR REPLACE INTO answers VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", answers)
        conn.executemany("INSERT OR REPLACE INTO verdicts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", verdicts)
        conn.executemany("INSERT OR REPLACE INTO conclusions VALUES(?,?,?,?,?,?,?)", conclusions)
    store.close()

    client = TestClient(create_app(str(db_path)))
    pages = ["/", f"/question/{quote('q0|fp0')}", "/queue?filter_name=undecided"]
    for path in pages:
        t0 = time.time()
        r = client.get(path)
        elapsed = time.time() - t0
        assert r.status_code == 200, path
        assert elapsed < 1.0, f"{path} 响应 {elapsed:.2f}s 超过 1s"
