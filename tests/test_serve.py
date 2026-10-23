"""复核服务集成测试（doc/10 §10）：新六页面路由、旧路由 301、批量提交与预览接口、
markdown 渲染、页面数字与导出同源、合成大库下全页面响应时间。"""
import json
import time
from urllib.parse import quote

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("bleach")
pytest.importorskip("markdown")
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


def test_cases_page(served, tmp_path):
    """用例页：综合分构成（ec 主口径 forced）、用例表（case_id 一行）。"""
    client, db, _, ops = served
    run_f = make_run(tmp_path, "demof", [mk_record(mode="forced")], run_id="demof",
                     snapshot={"skills_mode": "default-roots", "evalset": {"sha256": "a"}})
    assert cli.main(["score", str(run_f), "--operators", ops, "--db", db]) == 0
    r = client.get("/")
    assert r.status_code == 200
    assert "用例" in r.text and "ec-1" in r.text          # 按用例（case_id）分行
    assert "综合分" in r.text and "n=1" in r.text          # forced 一段（fail → 0 分）


def test_case_detail(served):
    client, db, _, _ = served
    s = Store(db)
    qkey = s.questions()[0]["qkey"]
    case_id = s.questions()[0]["case_id"]
    answer_id = s.answers()[0]["answer_id"]
    s.close()
    r = client.get(f"/case/{quote(case_id)}")
    assert r.status_code == 200
    assert "未收尾款不应电放" in r.text                    # 期望条目
    assert quote(answer_id) in r.text                    # 对话列表一行一段（链接编码）
    assert "参考答案" in r.text or "期望" in r.text


def test_review_page(served_plain):
    """复核页：对话全文（不截断）、评分表分节、机器栏、键盘提示。"""
    client, db = served_plain
    s = Store(db)
    answer_id = s.answers()[0]["answer_id"]
    s.close()
    r = client.get(f"/review/{quote(answer_id)}")
    assert r.status_code == 200
    assert "评分表" in r.text and "禁止事实" in r.text      # 分节标题（该题有 X1）
    assert "答：规则是收齐尾款后才能电放。" in r.text        # 对话全文，不再 [:400] 截断
    assert 'data-review-body' in r.text and answer_id in r.text
    assert "手改总分" in r.text


def test_review_page_markdown_and_static(served_plain):
    """静态资源与 md 渲染：表格不转义丢失、脚本可取。"""
    client, db = served_plain
    assert client.get("/static/bundle.css").status_code == 200
    assert client.get("/static/review.js").status_code == 200
    s = Store(db)
    answer_id = s.answers()[0]["answer_id"]
    rec = json.loads(s.answer(answer_id)["record_json"])
    rec["turns"][0]["output"] = "| a | b |\n|---|---|\n| 1 | 2 |"
    s._exec("UPDATE answers SET record_json=? WHERE answer_id=?",
            (json.dumps(rec, ensure_ascii=False), answer_id))
    s.close()
    r = client.get(f"/review/{quote(answer_id)}")
    assert "<table>" in r.text and "<th>a</th>" in r.text   # markdown 表格渲染


def test_preview_and_batch_submit(served_plain):
    """§9 接口：preview 不落库；批量提交原子（一条不合法整批不写）；total 需理由。"""
    client, db = served_plain
    s = Store(db)
    answer_id = s.answers()[0]["answer_id"]
    fp = s.current_spec(s.answers()[0]["qkey"])[0]
    s.close()
    body = {"answer_id": answer_id, "entries": [{"item_id": "F1", "value": "对"}]}
    p = client.post("/api/review/preview", json=body).json()
    assert p["status"] == "undecided" and p["score_max"] == 100 and p["score_min"] == 0
    # F1 对 + X1 未采信 → 两点收敛，100 分通过
    p2 = client.post("/api/review/preview", json={
        "answer_id": answer_id,
        "entries": [{"item_id": "F1", "value": "对"}, {"item_id": "X1", "value": "未采信"}]}).json()
    assert p2["status"] == "pass" and p2["score"] == 100
    s = Store(db)
    assert s.conn.execute("SELECT COUNT(*) c FROM reviews").fetchone()["c"] == 0   # 预览不落库
    s.close()
    bad = client.post("/api/reviews", json={"answer_id": answer_id, "reviewer": "t",
                                            "entries": [{"item_id": "F1", "value": "对"},
                                                        {"item_id": "X1", "value": "未采信"}],
                                            "total": {"score": 65, "reason": ""}})
    assert bad.status_code == 400 and "理由" in bad.json()["error"]
    s = Store(db)
    assert s.conn.execute("SELECT COUNT(*) c FROM reviews").fetchone()["c"] == 0   # 整批不写
    s.close()
    ok = client.post("/api/reviews", json={"answer_id": answer_id, "reviewer": "焦老师",
                                           "entries": [{"item_id": "F1", "value": "部分", "score": 30,
                                                        "reason": "只答了一半"},
                                                       {"item_id": "X1", "value": "未采信"}],
                                           "total": {"score": 65, "reason": "表述到位，扣分过重"}})
    assert ok.status_code == 200
    j = ok.json()
    assert j["score"] == 65 and j["score_source"] == "human" and j["status"] == "pass"
    assert j["next"].startswith("/review")
    s = Store(db)
    assert len(s.reviews_for(answer_id)) == 3               # 两条条目 + 一条 @total
    s.close()


def test_review_compat_single(served):
    """旧单条接口 /api/review 兼容。"""
    client, db, _, _ = served
    s = Store(db)
    a = s.answers()[0]
    s.close()
    bad = client.post("/api/review", json={"answer_id": a["answer_id"], "item_id": "NOPE",
                                           "value": "对", "reviewer": "codfish"})
    assert bad.status_code == 400
    ok = client.post("/api/review", json={"answer_id": a["answer_id"], "item_id": "F1",
                                          "value": "对", "reason": "回答明确", "reviewer": "codfish"})
    assert ok.status_code == 200 and ok.json()["episode_status"] == "pass"


def test_undecided_queue_shrinks_after_review(served_plain):
    """R2：改判一条未决条目 → 对话仍在队（X1 未决且带否决可能，区间跨及格线），
    待判条目数 2 → 1；两条都判完后出队。"""
    client, db = served_plain
    s = Store(db)
    answer_id = s.answers()[0]["answer_id"]
    s.close()
    q0 = client.get("/review?tab=undecided").text
    assert quote(answer_id) in q0 and "待判 2" in q0
    ok = client.post("/api/review", json={"answer_id": answer_id, "item_id": "F1",
                                          "value": "对", "reason": "回答明确", "reviewer": "codfish"})
    assert ok.status_code == 200
    q1 = client.get("/review?tab=undecided").text
    assert quote(answer_id) in q1 and "待判 1" in q1 and "待判 2" not in q1
    ok2 = client.post("/api/review", json={"answer_id": answer_id, "item_id": "X1",
                                           "value": "未采信", "reason": "明确否定", "reviewer": "codfish"})
    assert ok2.status_code == 200
    q2 = client.get("/review?tab=undecided").text
    assert quote(answer_id) not in q2                       # 两条已判 → 结论确定，出队


def test_mismatch_and_failed_tabs(served):
    client, db, _, _ = served
    s = Store(db)
    a = s.answers()[0]
    s.close()
    client.post("/api/review", json={"answer_id": a["answer_id"], "item_id": "F1",
                                     "value": "对", "reason": "回答明确", "reviewer": "codfish"})
    mm = client.get("/review?tab=mismatch").text
    assert a["qkey"].split("|")[0] in mm and quote(a["answer_id"]) in mm   # 一行一段对话，链接按 answer_id 编码


def test_legacy_redirects(served):
    client, db, _, _ = served
    s = Store(db)
    qkey = s.questions()[0]["qkey"]
    case_id = s.questions()[0]["case_id"]
    s.close()
    assert client.get(f"/question/{quote(qkey)}", follow_redirects=False).status_code == 301
    assert client.get(f"/question/{quote(qkey)}", follow_redirects=False).headers["location"].startswith(f"/case/")
    assert client.get("/group/x", follow_redirects=False).headers["location"] == "/case/x"
    assert client.get("/queue?filter_name=mismatch", follow_redirects=False).headers["location"] == "/review?tab=mismatch"
    assert client.get("/quality", follow_redirects=False).headers["location"] == "/judging"


def test_overview_matches_export_report(served, tmp_path):
    """页面数字与 export 产出的 scoring.json 同源一致（forced 主口径均分）。"""
    client, db, _, ops = served
    run_f = make_run(tmp_path, "demof", [mk_record(mode="forced")], run_id="demof",
                     snapshot={"skills_mode": "default-roots", "evalset": {"sha256": "a"}})
    assert cli.main(["score", str(run_f), "--operators", ops, "--db", db]) == 0
    out = tmp_path / "exp"
    assert cli.main(["export", "--db", db, "--operators", ops, "--out", str(out)]) == 0
    report = json.loads((out / "scoring.json").read_text(encoding="utf-8"))
    cell = report["views"]["ability"]["错误知识纠正|forced"]
    page = client.get("/").text
    assert "综合分" in page
    assert f"{cell['score_mean']}" in page                 # 均分同源


def test_compare_scores_page(served):
    """分数对比：护栏拦截（mode 不一致）、放行后出组件贡献。"""
    client, db, tmp_path, ops = served
    run2 = make_run(tmp_path, "demo2", [mk_record()], run_id="demo2",
                    snapshot={"skills_mode": "default-roots", "evalset": {"sha256": "a"},
                              "models_observed": ["glm-4"]})
    assert cli.main(["score", str(run2), "--operators", ops, "--db", db]) == 0
    run3 = make_run(tmp_path, "demo3", [mk_record(mode="forced")], run_id="demo3",
                    snapshot={"skills_mode": "default-roots", "evalset": {"sha256": "a"}})
    assert cli.main(["score", str(run3), "--operators", ops, "--db", db]) == 0
    ok = client.get("/compare?axis=run&base=demo&target=demo2&any=mode,model")
    assert ok.status_code == 200 and "组件贡献" in ok.text and "综合分" in ok.text
    blocked = client.get("/compare?axis=run&base=demo&target=demo3&any=model")
    assert "已拒绝比较" in blocked.text and "mode" in blocked.text


def test_judging_page(served):
    client, db, _, _ = served
    s = Store(db)
    a = s.answers()[0]
    s.close()
    client.post("/api/review", json={"answer_id": a["answer_id"], "item_id": "F1",
                                     "value": "对", "reason": "r", "reviewer": "codfish"})
    r = client.get("/judging")
    assert r.status_code == 200
    assert "判定质量" in r.text and "数据来源" in r.text
    assert "keyword" in r.text                              # 算子行


# ---------------------------------------------------------------- 大库性能（R7/doc-10 §G6）

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
                                "natural", ai + 1, "default-roots", "s1", "v1", "ok", rec_json, 0, "[]", None))
                status = "undecided" if kind == 0 else "pass"
                concl_status = {"0": "undecided", "1": "fail", "2": "pass"}[str(kind)]
                score = {"0": None, "1": 0.0, "2": 100.0}[str(kind)]
                conclusions.append((a_id, fp, chain, policy_sha, concl_status, "", "{}",
                                    score, score, score, score, "auto" if score is not None else None))
                for fi, f in enumerate(facts):                 # 10 条判定/答案 → 10 万条
                    if kind == 0 and fi == 0:
                        v, st, src = None, "undecided", ""
                    elif kind == 1:
                        v, st, src = "错", "ok", "rule"
                    else:
                        v, st, src = "对", "ok", "rule"
                    verdicts.append((a_id, fp, chain, f["id"], "required", 1, f"sha{qi}",
                                     "asha", v, src, "keyword@1", "", "[]", st, "2026-01-01", None))
                if kind == 1 and ai == 1 and qi < 1000:        # 人工与机器不一致的改判
                    reviews.append((a_id, "F1", f"sha{qi}", "asha", "对", "回答明确", "codfish",
                                    "2026-01-01", None))
        conn.executemany("INSERT OR REPLACE INTO questions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", questions)
        conn.executemany("INSERT OR REPLACE INTO answers VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", answers)
        conn.executemany("INSERT OR REPLACE INTO verdicts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", verdicts)
        conn.executemany("INSERT OR REPLACE INTO conclusions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", conclusions)
        conn.executemany("INSERT INTO reviews(answer_id, item_id, item_sha, answer_sha, value, reason,"
                         " reviewer, at, legacy_key) VALUES(?,?,?,?,?,?,?,?,?)", reviews)
    store.close()
    return chain, policy_sha


def test_synthetic_scale_performance(tmp_path):
    """1 万答案 / 10 万判定（含未决/失败/改判分布）：全部页面响应 < 1s 且有数据。"""
    db_path = tmp_path / "big.db"
    _build_big_db(db_path)

    client = TestClient(create_app(str(db_path)))
    first = "big/case0%230-natural-t1"
    pages = [
        "/", "/case/case0",
        "/review?tab=undecided", "/review?tab=mismatch", "/review?tab=spot",
        f"/review/{first}",
        "/compare?axis=run", "/judging",
    ]
    for path in pages:
        t0 = time.time()
        r = client.get(path)
        elapsed = time.time() - t0
        assert r.status_code == 200, path
        assert elapsed < 1.0, f"{path} 响应 {elapsed:.2f}s 超过 1s"
    # 队列按对话聚合，确实有数据（R7 验收：不是空跑）
    # 分布：每 10 个答案 ai%3==0 → 4 个未决（各 1 条未决条目）、ai%3==1 → 3 个失败
    qu = client.get("/review?tab=undecided").text
    assert ">4000<" in qu                                   # 结论待定 4000 段对话
    qm = client.get("/review?tab=mismatch").text
    assert ">1000<" in qm                                   # 人工≠机器 1000 段
    # 复核页渲染 10 条判定行与对话
    rp = client.get(f"/review/{first}").text
    assert rp.count("rv-row") >= 10 and "评分表" in rp
