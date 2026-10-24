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


def test_rescore_notice(tmp_path):
    """当前口径下无结论（只导入了期望）时，页面提示执行 rescore。"""
    db = str(tmp_path / "fresh.db")
    assert cli.main(["specs", "import", dist_jsonl(tmp_path), "--db", db]) == 0
    client = TestClient(create_app(db))
    assert "还没有会话结论" in client.get("/").text
    assert "python -m scoring rescore" in client.get("/").text


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
    assert "还没有会话结论" not in r.text


def test_skills_base_stale_badge(served, tmp_path):
    """快照标 skills_base_stale 的 run，其对话卡片带「技能基底过期」提示。"""
    client, db, _, ops = served
    stale = make_run(tmp_path, "stale", [mk_record()], run_id="stale",
                     snapshot={"skills_mode": "bundled-copy", "skills_base_stale": True,
                               "evalset": {"sha256": "a"}})
    assert cli.main(["score", str(stale), "--operators", ops, "--db", db]) == 0
    s = Store(db)
    case_id = s.questions()[0]["case_id"]
    s.close()
    r = client.get(f"/case/{quote(case_id)}")
    assert r.status_code == 200 and "技能基底过期" in r.text


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


def test_case_detail_health_variant_and_pair(served, tmp_path):
    """用例详情：体检行与粒度、变体下钻只留该变体、勾选两段并排逐轮对照。"""
    client, db, _, _ = served
    s = Store(db)
    q = s.questions()[0]
    case_id = q["case_id"]
    aids = [a["answer_id"] for a in s.answers()]
    s.close()
    page = client.get(f"/case/{quote(case_id)}").text
    assert "用例体检" in page and "环节" in page

    variant = mk_record()
    variant["uid"] = "ec-1#1"
    variant["case"] = {**variant["case"], "uid": "ec-1#1", "variant_index": 1,
                       "variant": {"id": "V1", "rewrite": "口语"}, "granularity": "细节"}
    run_dir = make_run(tmp_path, "var", [mk_record(), variant], run_id="var")
    assert cli.main(["score", str(run_dir), "--db", db]) == 0
    s = Store(db)
    by_uid = {a["record_json"]: a["answer_id"] for a in s.answers()}
    aid1 = next(aid for rec, aid in by_uid.items() if json.loads(rec)["uid"] == "ec-1#1")
    other = next(aid for rec, aid in by_uid.items() if json.loads(rec)["uid"] == "ec-1#0")
    s.close()
    drill = client.get(f"/case/{quote(case_id)}", params={"variant": "ec-1#1"}).text
    assert "只看 ec-1#1" in drill and quote(aid1) in drill and quote(other) not in drill

    pair = client.get(f"/case/{quote(case_id)}", params=[("pair", aids[0]), ("pair", aids[1])]).text
    assert "并排对照" in pair and "答：规则是收齐尾款后才能电放。" in pair


def test_cases_granularity_filter(served):
    client, db, _, _ = served
    assert 'href="/case/ec-1"' in client.get("/?granularity=环节").text
    assert 'href="/case/ec-1"' not in client.get("/?granularity=细节").text


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
    # tojson 不转义双引号（Jinja2 明确不支持双引号属性）：data-* 必须单引号，否则
    # 属性被截断、评分表脚本启动即崩（data-concl 变成裸 "{"）
    assert "data-concl='{\"" in r.text and "data-scale='[\"" in r.text
    assert "data-map='{}'" in r.text                        # 非计分行 map 为空对象
    assert 'data-concl="{' not in r.text


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


def test_queue_spot_ratio_and_operator_filter(served):
    """模型抽检比例可选；分歧链接带算子筛选，只列该算子的分歧对话。"""
    client, db, _, _ = served
    s = Store(db)
    a = s.answers()[0]
    op = s.conn.execute("SELECT DISTINCT operator FROM verdicts WHERE operator IS NOT NULL").fetchone()[0]
    s.close()
    spot = client.get("/review?tab=spot&spot=20").text
    assert "抽检比例" in spot and 'value="20" selected' in spot

    client.post("/api/review", json={"answer_id": a["answer_id"], "item_id": "F1",
                                     "value": "对", "reason": "回答明确", "reviewer": "codfish"})
    matched = client.get(f"/review?tab=mismatch&op={quote(op)}").text
    assert quote(a["answer_id"]) in matched
    assert "全部算子" in matched
    other = client.get("/review?tab=mismatch&op=no-such-operator").text
    assert quote(a["answer_id"]) not in other


def test_review_urls_escape_hash(tmp_path):
    """P0：uid 带 #（如 ec-1#0）→ 复核页 prev/next 与提交返回的 next 都必须转义，
    否则 # 后被当锚点截断，跳到「答案不存在」。"""
    db = str(tmp_path / "hash.db")
    records = [mk_record(uid="ec-1#0"), mk_record(uid="ec-2#0")]
    run_dir = make_run(tmp_path, "hx", records, run_id="hx")
    assert cli.main(["specs", "import", dist_jsonl(tmp_path), "--db", db]) == 0
    assert cli.main(["score", str(run_dir), "--db", db]) == 0
    client = TestClient(create_app(db))
    s = Store(db)
    aids = sorted(a["answer_id"] for a in s.answers())
    s.close()
    assert all("#" in a for a in aids)
    page = client.get(f"/review/{quote(aids[0])}").text
    assert "%23" in page and "data-next=" in page              # 跳过链接已编码
    ok = client.post("/api/reviews", json={"answer_id": aids[0], "reviewer": "t",
                                           "entries": [{"item_id": "F1", "value": "对"},
                                                       {"item_id": "X1", "value": "未采信"}]})
    assert ok.status_code == 200
    nxt = ok.json()["next"]
    assert "%23" in nxt and nxt != "/review?tab=undecided"     # 落到下一段，且 # 已转义
    assert client.get(nxt).status_code == 200                  # 该 URL 可直接访问


def test_submit_total_only(served_plain):
    """P1：只手改人工总分（无条目草稿）也能提交；后端本就支持 entries=[] + total。"""
    client, db = served_plain
    s = Store(db)
    answer_id = s.answers()[0]["answer_id"]
    s.close()
    ok = client.post("/api/reviews", json={"answer_id": answer_id, "reviewer": "t",
                                           "entries": [], "total": {"score": 70, "reason": "总体到位"}})
    assert ok.status_code == 200
    j = ok.json()
    assert j["score"] == 70 and j["score_source"] == "human" and j["status"] == "pass"


def test_review_rejects_score_on_veto_kind(served_plain):
    """P2：前提/路径/禁止条目是门槛与否决项，不记条目分，人工打分带分数应拒绝。"""
    client, db = served_plain
    s = Store(db)
    answer_id = s.answers()[0]["answer_id"]
    s.close()
    bad = client.post("/api/reviews", json={"answer_id": answer_id, "reviewer": "t",
                                            "entries": [{"item_id": "X1", "value": "未采信", "score": 50}]})
    assert bad.status_code == 400 and "不记条目分" in bad.json()["error"]


def test_review_page_keeps_saved_human_values(served_plain):
    """P1：复核页加载后已存人工值仍在服务端渲染里（radio 预选 / 分数 / 理由回填），
    前端 refreshRows 不再清掉（review.js 以 initial 兜底）。"""
    client, db = served_plain
    s = Store(db)
    answer_id = s.answers()[0]["answer_id"]
    s.close()
    ok = client.post("/api/reviews", json={"answer_id": answer_id, "reviewer": "焦老师",
                                           "entries": [{"item_id": "F1", "value": "部分", "score": 30,
                                                        "reason": "只答了一半"}]})
    assert ok.status_code == 200
    page = client.get(f"/review/{quote(answer_id)}").text
    assert 'value="30.0"' in page and "只答了一半" in page    # 分数（浮点渲染）与理由回填
    assert "焦老师" in page                                      # 人工记录展示


def test_review_queue_first_input_skips_prefill(served_plain):
    """P1：runner 写 source='prefill'（非 'preset'）——队列首句取真实首问，
    复核页对话里预置轮仍展示（data-role=preset 样式）。"""
    client, db = served_plain
    s = Store(db)
    answer_id = s.answers()[0]["answer_id"]
    rec = json.loads(s.answer(answer_id)["record_json"])
    rec["turns"] = [{"index": 0, "source": "prefill", "input": "预置的历史提问", "output": "旧回答",
                     "status": "ok", "observations": {}}] + rec["turns"]
    s._exec("UPDATE answers SET record_json=? WHERE answer_id=?",
            (json.dumps(rec, ensure_ascii=False), answer_id))
    s.close()
    q = client.get("/review?tab=undecided").text
    assert "预置的历史提问" not in q                              # 队列首句跳过预置轮
    page = client.get(f"/review/{quote(answer_id)}").text
    assert 'data-role="preset"' in page and "预置的历史提问" in page   # 对话区仍显示（标预置）


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


def test_compare_scores_page(served, tmp_path):
    """分数对比：放行后出组件贡献；mode 混合不再拦截（按 headline_mode 逐维度取数，
    无主口径会话的一侧按单侧排除）；其他处理条件不一致仍拦截。"""
    client, db, tmp_path, ops = served
    run2 = make_run(tmp_path, "demo2", [mk_record(mode="forced")], run_id="demo2",
                    snapshot={"skills_mode": "default-roots", "evalset": {"sha256": "a"},
                              "models_observed": ["glm-4"]})
    assert cli.main(["score", str(run2), "--operators", ops, "--db", db]) == 0
    run3 = make_run(tmp_path, "demo3", [mk_record(mode="forced")], run_id="demo3",
                    snapshot={"skills_mode": "default-roots", "evalset": {"sha256": "a"}})
    assert cli.main(["score", str(run3), "--operators", ops, "--db", db]) == 0
    ok = client.get("/compare?axis=run&base=demo3&target=demo2&any=model")
    assert ok.status_code == 200 and "组件贡献" in ok.text and "综合分" in ok.text
    # demo 全 natural、demo3 全 forced：mode 放行后按主口径逐题取数——
    # ec 主口径 forced 只在 demo3 侧 → 该题按「两侧干预模式不一致」排除，不再拦截整页
    mixed = client.get("/compare?axis=run&base=demo&target=demo3&any=model")
    assert mixed.status_code == 200 and "已拒绝比较" not in mixed.text
    assert "两侧干预模式不一致" in mixed.text
    blocked = client.get("/compare?axis=run&base=demo3&target=demo2")
    assert "已拒绝比较" in blocked.text and "model" in blocked.text


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
            questions.append((qkey, uid, case_id, 0, None, "知识召回", "t", "环节", "[]", fp, fp, 1, "2026-01-01"))
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
        conn.executemany("INSERT OR REPLACE INTO questions(qkey, uid, case_id, variant_index, rewrite, dim,"
                         " topic, granularity, tags_json, input_fp, current_judge_fp, active, first_seen)"
                         " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", questions)
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
    # 提交接口 < 1s：追加 + 同事务重算 + 下一段定位（免全量 record 解析）
    t0 = time.time()
    r = client.post("/api/reviews", json={"answer_id": "big/case0#0-natural-t1", "reviewer": "t",
                                          "entries": [{"item_id": "F1", "value": "对"}]})
    elapsed = time.time() - t0
    assert r.status_code == 200 and elapsed < 1.0, f"提交响应 {elapsed:.2f}s 超过 1s"
    assert "%23" in r.json()["next"]

    # 带 base/target 的对比页（含组件贡献与自助法区间）< 1s：用较小合成库
    # （共有题 × B=1000 自助法的开销与题目数成正比，1 万答案库不在页面 1s 线内考核）
    small_db = tmp_path / "mid.db"
    _build_big_db(small_db, n_q=120, n_per=10)
    mid = TestClient(create_app(str(small_db)))
    t0 = time.time()
    r = mid.get("/compare?axis=trial&base=1&target=2")
    elapsed = time.time() - t0
    assert r.status_code == 200 and "组件贡献" in r.text
    assert elapsed < 1.0, f"对比页（base/target）响应 {elapsed:.2f}s 超过 1s"
