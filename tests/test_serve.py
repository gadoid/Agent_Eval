from scoring.rules import load_rules as _lr, default_rules_path as _drp
RULES, RULES_SHA = _lr(_drp())
"""复核服务集成测试（doc/10 §10）：新六页面路由、旧路由 301、批量提交与预览接口、
markdown 渲染、页面数字与导出同源、合成大库下全页面响应时间。"""
import json
import re
import time
from pathlib import Path
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


def test_create_app_rules_path_accepts_both_formats(tmp_path):
    """--rules 给 rules.yaml 直接装载；旧 policy.yaml（无 schema:1）转换装载，
    等价内容登记同一 rules_sha。修复前 rules.yaml 被无条件当 policy 装载 → PolicyError。"""
    db = str(tmp_path / "eval.db")
    c1 = TestClient(create_app(db, rules_path=_drp()))
    assert c1.get("/").status_code == 200
    c2 = TestClient(create_app(db, rules_path=default_policy_path()))
    assert c2.get("/").status_code == 200
    shas = {r["rules_sha"] for r in Store(db).rules_rows()}
    assert len(shas) == 1


def test_config_detail_pages(served):
    """三件套详情页（doc/15 §4 配置可解释）：rules 全文+diff、chain 链组成、
    snapshot 测法与题目清单；侧边栏 🔍 与帮助浮层渲染。"""
    client, db, tmp_path, ops = served
    st = Store(db)
    sha = RULES_SHA
    page = client.get(f"/detail/rules/{sha}").text
    assert "pass_mark" in page and "operator_score" in page
    assert "没有计分规则" in client.get("/detail/rules/none").text

    base = st.rules_rows()[-1]["rules"]                       # 与库内另一条规则 diff
    mod = {**base, "score": {**base["score"], "pass_mark": 75}}
    from scoring.rules import canonical_sha
    mod_sha = canonical_sha(mod)
    st.register_rules(mod_sha, mod, name="v2")
    diff = client.get(f"/detail/rules/{sha}?diff={mod_sha}").text
    assert "pass_mark" in diff and "75" in diff

    chain_sha = st.chains()[0]["chain_sha"]
    cpage = client.get(f"/detail/chain/{chain_sha}").text
    assert ("逐条目类型链组成" in cpage) or ("历史链" in cpage)

    ver = st.snapshots()[0]["evalset_version"]
    spage = client.get(f"/detail/snapshot/{ver}").text
    assert "测法" in spage and "题目清单" in spage

    home = client.get("/").text                                # 侧边栏 🔍 + 帮助浮层
    assert home.count("🔍") >= 3 and "judge-help" in home and "snapshot use" in home


def test_weight_history_view_and_restore(served):
    """用例页权重历史：历次配置可查（含旧权重下现存结论）、可切回（写成新有效
    配置 + 重算本用例）；切回「当前」被拒。"""
    client, db, tmp_path, ops = served
    st = Store(db)
    r1 = st.revise_weights("ec-1", {"F1": 40})
    st.rescore_cases(["ec-1"], RULES, RULES_SHA)
    old = st.weight_history("ec-1")[0]["set_at"]
    st.revise_weights("ec-1", {"F1": 70}, label="提高F1")
    st.rescore_cases(["ec-1"], RULES, RULES_SHA)

    page = client.get("/case/ec-1").text
    assert "权重历史" in page and "切回" in page and old[:10] in page

    resp = client.post("/api/case/weights-restore",
                       json={"case_id": "ec-1", "set_at": old, "label": ""})
    assert resp.status_code == 200 and resp.json()["rescored"] >= 1
    w, wfp, _ = st.effective_weights("ec-1")
    assert w == {"F1": 40} and wfp == r1["weight_fp"]        # 切回后指纹复原

    hist = st.weight_history("ec-1")
    assert hist[0]["active"] and hist[0]["label"].startswith("切回")
    assert any(g["weight_fp"] == r1["weight_fp"] and not g["active"] for g in hist[1:])
    bad = client.post("/api/case/weights-restore",
                      json={"case_id": "ec-1", "set_at": hist[0]["set_at"], "label": hist[0]["label"]})
    assert bad.status_code == 400 and "已是当前" in bad.json()["error"]
    assert client.post("/api/case/weights-restore",
                       json={"case_id": "ec-1", "set_at": "1999-01-01"}).status_code == 400


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
    # 变体选择只决定初始聚焦；全部对话仍渲染，前端按变体收敛（可随时切「全部」）
    drill = client.get(f"/case/{quote(case_id)}", params={"variant": "ec-1#1"}).text
    assert 'data-focus-variant="ec-1#1"' in drill and "只看 ec-1#1" in drill
    assert 'data-vtab="ec-1#1"' in drill and 'data-vpanel="ec-1#1"' in drill
    assert quote(aid1) in drill and quote(other) in drill
    assert "显示 1 / 1" in drill   # 服务端按初始聚焦渲染计数，无 JS 也自洽
    assert re.search(r'data-uid="ec-1#0"[^>]*hidden', drill)
    all_page = client.get(f"/case/{quote(case_id)}", params={"variant": "__all"}).text
    assert 'data-show-all="1"' in all_page and "全部变体" in all_page

    pair = client.get(f"/case/{quote(case_id)}", params=[("pair", aids[0]), ("pair", aids[1])]).text
    assert "并排对照" in pair and "答：规则是收齐尾款后才能电放。" in pair


def test_case_detail_variant_questions(served, tmp_path):
    """变体表直接列出该变体的提问（不必先点进对话）。"""
    client, db, _, _ = served
    case = dict(mk_record()["case"])
    row = {**case, "uid": "ec-1#1", "variant_index": 1,
           "variant": {"id": "V1", "rewrite": "口语"},
           "turns": [{"role": "user", "content": "口语变体：单据要准备什么？"}],
           "evalset_version": "v2"}
    spec = tmp_path / "variant.jsonl"
    spec.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    assert cli.main(["specs", "import", str(spec), "--db", db]) == 0
    page = client.get("/case/ec-1").text
    assert "口语变体：单据要准备什么？" in page
    assert 'data-vtab="ec-1#1"' in page and 'data-vpanel="ec-1#1"' in page


def test_case_detail_reference_fold(tmp_path):
    """参考答案是可选项：native details 折叠，不占首屏；无对话时也有空状态。"""
    db = str(tmp_path / "fold.db")
    case = dict(mk_record()["case"])
    row = {**case, "evalset_version": "v1", "reference_answer": "参考答案正文",
           "turns": [{"role": "user", "content": "单据要准备什么？"}]}
    spec = tmp_path / "fold.jsonl"
    spec.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    assert cli.main(["specs", "import", str(spec), "--db", db]) == 0
    page = TestClient(create_app(db)).get("/case/ec-1").text
    assert 'class="rv-fold"' in page and "参考答案正文" in page
    assert "还没有对话" in page


def test_case_detail_dynamic_controls(served):
    """用例详情：变体选择器、对话筛选、并排计数与脚本钩子。"""
    page = served[0].get("/case/ec-1").text
    assert 'data-vtabs' in page and 'data-vpanel="ec-1#0"' in page
    assert 'data-focus-variant="ec-1#0"' in page and 'data-show-all="0"' in page
    assert "/static/case.js" in page
    assert 'data-theme-opt="dark"' in page and 'data-theme-opt="system"' in page and "/static/theme.js" in page
    assert 'data-cfilter="failed"' in page and 'data-cfilter-count="undecided"' in page
    assert 'data-pair-count' in page and 'data-pair-submit' in page
    assert 'data-uid="ec-1#0"' in page and 'data-status="' in page and 'data-conv-scope' in page


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
    # 权重调整后的复核行按百分比显示（百分制标记 → 权重 60.0%）
    st = Store(db)
    q = st.questions()[0]
    st.revise_weights(q["case_id"], {"F1": 60})     # v8：按用例存 item_weights
    st.close()
    r2 = client.get(f"/review/{quote(answer_id)}")
    assert "权重 60%</span>" in r2.text


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


def test_compare_subject_pick(served, tmp_path):
    """被测配置直选对比（主路径）：选两个指纹，自动展示全部差异；恰好一个组件不同
    直接出结果；同一配置、跨评测集快照给出明确指引而非护栏报错。"""
    client, db, tmp_path, ops = served
    snapA = {"skills_mode": "default-roots", "evalset": {"sha256": "a"},
             "agent": {"name": "dsh-cli", "version": "9.9.9"}}   # 与 demo 同评测集（版本 ?）
    run2 = make_run(tmp_path, "demo2", [mk_record(mode="forced")], run_id="demo2",
                    snapshot=snapA)
    assert cli.main(["score", str(run2), "--operators", ops, "--db", db]) == 0
    run3 = make_run(tmp_path, "demo3", [mk_record(mode="forced")], run_id="demo3",
                    snapshot={"skills_mode": "default-roots", "evalset": {"sha256": "a"}})
    assert cli.main(["score", str(run3), "--operators", ops, "--db", db]) == 0
    st = Store(db)
    by_agent = {str(r["agent"]): r["subject_fp"] for r in st.conn.execute(
        "SELECT DISTINCT subject_fp, agent FROM answers")}
    st.close()
    fp_new = next(fp for ag, fp in by_agent.items() if "9.9.9" in ag)
    fp_base = next(fp for ag, fp in by_agent.items() if "9.9.9" not in ag)

    page = client.get(f"/compare?base={fp_base}&target={fp_new}")
    assert page.status_code == 200
    assert "配置差异" in page.text                    # 全量差异表
    assert "一个组件不同" in page.text                # 只有 agent 不同 → 归因明确
    assert "组件贡献" in page.text and "综合分" in page.text

    same = client.get(f"/compare?base={fp_new}&target={fp_new}")
    assert "同一个被测配置" in same.text

    # 同一被测配置（agent 9.9.9）再跑一个别的评测集快照 → 两侧评测集不同：引导而非拦截
    run4 = make_run(tmp_path, "demo4", [mk_record(mode="forced")], run_id="demo4",
                    snapshot={"skills_mode": "default-roots", "evalset": {"version": "vb"},
                              "agent": {"name": "dsh-cli", "version": "9.9.9"}})
    assert cli.main(["score", str(run4), "--operators", ops, "--db", db]) == 0
    cross = client.get(f"/compare?base={fp_base}&target={fp_new}")
    assert "跑在不同的评测集快照" in cross.text and "标准修订验证" in cross.text
    assert "已拒绝比较" not in cross.text


def test_judging_page(served):
    client, db, _, _ = served
    s = Store(db)
    answers = [a["answer_id"] for a in s.answers()]
    s.close()
    for aid in answers:                                     # 同一算子两条改判 → 分母 > 1
        client.post("/api/review", json={"answer_id": aid, "item_id": "F1",
                                         "value": "未提及", "reason": "r", "reviewer": "codfish"})
    r = client.get("/judging")
    assert r.status_code == 200
    assert "判定质量" in r.text and "数据来源" in r.text
    assert "keyword" in r.text                              # 算子行
    assert 'rv-rate-pct">100%' in r.text and ">2/2<" in r.text
    assert 'rv-rate-pct">200%' not in r.text                # 计数不能当 0–1 比率


# ---------------------------------------------------------------- 大库性能（R7/doc-10 §G6）

def _build_big_db(db_path, n_q=1000, n_per=10):
    """1 万答案 / 10 万判定：三分之一未决、三分之一失败；1000 条人工改判
    （其中约 1000 条与机器不一致）。结论行用真实的 chain_sha / rules_sha。"""
    chain = load_operator_config(default_operators_path()).chain_sha
    rules_sha = RULES_SHA
    facts = [{"id": f"F{i}", "kind": "required", "text": f"事实{i}", "keywords": [], "turn": 1,
              "group": None, "evidence": None} for i in range(1, 11)]
    store = Store(db_path)
    conn = store.conn
    with conn:
        conn.execute("INSERT INTO runs(run_id, run_dir, evalset_version, profile_id, snapshot_json,"
                     " imported_at, started_at) VALUES('big','/x','v1','p1','{}','2026-01-01','2026-01-01')")
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
                conclusions.append((a_id, fp, chain, rules_sha, concl_status, "", "{}",
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
                         " topic, granularity, tags_json, input_fp, current_standard_fp, active, first_seen)"
                         " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", questions)
        conn.executemany("INSERT OR REPLACE INTO answers(answer_id, qkey, run_id, episode_id,"
                         " judge_fp_at_run, profile_id, models_json, mode, trial, skills_mode,"
                         " skills_base, evalset_version, run_status, record_json,"
                         " injection_found_gt1, variant_leak_json, injection_observed_json)"
                         " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", answers)
        conn.executemany("INSERT OR REPLACE INTO verdicts(answer_id, standard_fp, chain_sha, item_id,"
                         " kind, turn, item_sha, answer_sha, value, source, operator, reason,"
                         " trace_json, status, judged_at, score) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", verdicts)
        conn.executemany("INSERT OR REPLACE INTO conclusions(answer_id, standard_fp, chain_sha, rules_sha,"
                         " weight_fp, status, reason, detail_json, score, score_min, score_max,"
                         " auto_score, score_source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         [(a, f, c, r, "equal", *rest) for a, f, c, r, *rest in conclusions])
        conn.executemany("INSERT INTO reviews(answer_id, item_id, item_sha, answer_sha, value, reason,"
                         " reviewer, at, legacy_key) VALUES(?,?,?,?,?,?,?,?,?)", reviews)
    store.close()
    return chain, rules_sha


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


def test_case_weights_editor(served):
    """用例详情权重分配（v8：按用例存 item_weights）：编辑器渲染、保存即重算、
    超限 400、无未覆盖条目校验、saved 提示条、标签留痕。"""
    client, db, _, _ = served
    s = Store(db)
    case_id = s.questions()[0]["case_id"]
    s.close()
    page = client.get(f"/case/{case_id}")
    assert 'id="weight-editor"' in page.text and 'id="w-pie"' in page.text   # 饼图编辑器
    assert 'rv-detail-btn' in page.text and 'data-detail=' in page.text      # 简要条目 + 详情下钻
    assert 'data-case="%s"' % case_id in page.text                           # B1：编辑对象=用例
    ok = client.post("/api/case/weights", json={"case_id": case_id, "weights": {"F1": 60}})
    assert ok.status_code == 200 and ok.json()["changed"] and ok.json()["rescored"] >= 1
    page3 = client.get(f"/case/{case_id}")
    assert 'data-wchip="F1">60%</span>' in page3.text        # 百分制原值显示（未分配不折算）
    page2 = client.get(f"/case/{case_id}?saved=abcd1234")
    assert "权重已保存" in page2.text and "不需要重判" in page2.text       # v8：无 rejudge 提示
    bad = client.post("/api/case/weights", json={"case_id": case_id, "weights": {"F1": 101}})
    assert bad.status_code == 400 and "0–100" in bad.json()["error"]   # 该用例只有 1 个计分条目，逐值上限先触发
    # 版本标签留痕（item_weights.label）
    ok2 = client.post("/api/case/weights", json={"case_id": case_id, "weights": {"F1": 30}, "label": "权重-降F1"})
    assert ok2.status_code == 200 and ok2.json()["changed"]
    assert "权重-降F1" in client.get(f"/case/{case_id}").text


def test_run_and_chain_rename(served):
    """命名改造：队列 run 选项显示「标题 · 短 id」；改名 API 两端校验；
    链改名后导航选择器更新；runtag 徽标带完整 run_id 提示。"""
    client, db, _, _ = served
    s = Store(db)
    rid = s.runs()[0]["run_id"]
    label = s.run_labels()[rid]
    chain_sha = s.chains()[0]["chain_sha"]
    s.close()
    page = client.get("/review")
    assert f"{label} · {rid[:8]}" in page.text
    ok = client.post("/api/run/rename", json={"run_id": rid, "title": "基线-glm53low"})
    assert ok.status_code == 200 and ok.json()["title"] == "基线-glm53low"
    assert f"基线-glm53low · {rid[:8]}" in client.get("/review").text
    assert client.post("/api/run/rename", json={"run_id": "no-such", "title": "x"}).status_code == 400
    okc = client.post("/api/chain/rename", json={"chain_sha": chain_sha, "name": "规则+llm-v2"})
    assert okc.status_code == 200
    assert "规则+llm-v2" in client.get("/judging").text
    assert client.post("/api/chain/rename", json={"chain_sha": "no-such", "name": "x"}).status_code == 400
    # runtag 徽标显示标题、悬停给完整 run_id（抽检队列的对话行）
    queue = client.get("/review?tab=spot&spot=100&run_id=" + quote(rid))
    assert f'title="{rid}"' in queue.text and "基线-glm53low" in queue.text


def test_configs_and_runs_pages(served, tmp_path):
    """配置管理页只含被测配置（每配置综合分）；执行记录独立成 /runs 页（run 表 + 命名）；
    用例管理页不再有配置选择器；侧边栏导航两页都有入口。"""
    client, db, _, ops = served
    run_f = make_run(tmp_path, "demof", [mk_record(mode="forced")], run_id="demof",
                     snapshot={"skills_mode": "default-roots", "evalset": {"sha256": "a"}})
    assert cli.main(["score", str(run_f), "--operators", ops, "--db", db]) == 0
    page = client.get("/configs")
    assert page.status_code == 200
    assert "配置管理" in page.text and "被测配置" in page.text and "综合分" in page.text
    assert "重命名执行记录" not in page.text           # 执行记录已拆到 /runs
    runs = client.get("/runs")
    assert runs.status_code == 200
    assert "执行记录" in runs.text and "重命名执行记录" in runs.text
    assert "demof" in runs.text                        # 两次执行各一行（run_id 短码）
    assert 'href="/runs"' in page.text                 # 两页互相有入口
    # 首页已拆分：无配置选择器，有新建入口与配置管理链接
    home = client.get("/").text
    assert "用例管理" in home and "/case/new" in home and "/configs" in home
    assert "被测配置</b>" not in home
    # 执行记录命名在 /runs 页生效
    ok = client.post("/api/run/rename", json={"run_id": "demof", "title": "forced 对照"})
    assert ok.status_code == 200
    assert "forced 对照" in client.get("/runs").text


def test_case_new_page(served, tmp_path):
    """新建用例页：表单骨架 + 词表/注入点/推荐 id 数据块。"""
    client, db, _, _ = served
    cases = tmp_path / "cases"
    cases.mkdir()
    app2 = create_app(db, cases_dir=str(cases))
    page = TestClient(app2).get("/case/new")
    assert page.status_code == 200
    body = page.text
    assert "新建用例" in body and "基本信息" in body and "必要事实" in body
    assert "知识召回" in body and "rewrites" in body       # 维度词表直渲染；改写词表进 JSON
    assert "cn-data" in body and "kr-0001" in body        # 空 cases 目录 → 推荐 kr-0001
    assert '<option value="order_intake"' not in body     # 自定义 cases_dir 不读真库目录


def _kr_body(**over):
    body = {"id": "kr-9001", "dim": "知识召回", "topic": "进单资料审核", "granularity": "环节",
            "source": "t", "tags": ["场景/首轮追问"], "history": "live",
            "turns": ["我司报关的融资业务，需要客户提供哪些融资单据？"], "assistant_refs": [],
            "conditions": [{"slot": "业务类型", "value": "融资业务", "keywords": [], "turn": None}],
            "injections": [], "preconditions": [],
            "facts": [{"kind": "required", "text": "开船后的提单",
                       "keywords": ["开船后的提单"], "turn": None, "group": "",
                       "evidence": "", "weight": None}],
            "paths": [], "evidences": [], "variants": [],
            "reference_answer": "", "note": "", "directory": "batch-a"}
    body.update(over)
    return body


def test_case_create_and_roundtrip(served, tmp_path):
    """新建用例：preview 出 md 与警告（缺来源提示）；create 落盘 md 且能被
    md2case 重新解析（md 是唯一数据源）；重复 id / 非法目录 / 校验失败分别拒绝。"""
    import sys
    client, db, _, _ = served
    cases = tmp_path / "cases"
    cases.mkdir()
    app2 = create_app(db, cases_dir=str(cases))
    c2 = TestClient(app2)

    prev = c2.post("/api/case/preview", json=_kr_body())
    assert prev.status_code == 200
    data = prev.json()
    assert data["ok"] and "## 问题" in data["md"] and "## 关键条件" in data["md"]
    assert data["md"].startswith("---")                    # frontmatter
    assert any("来源" in w for w in data["warnings"])       # 无证据事实 → 提示

    ok = c2.post("/api/case/create", json=_kr_body())
    assert ok.status_code == 200, ok.text
    saved = cases / "batch-a" / "kr-9001.md"
    assert saved.exists()
    text = saved.read_text(encoding="utf-8")
    assert "id: kr-9001" in text and "## 必要事实" in text
    sys.path.insert(0, str(Path("evalset").resolve()))
    import md2case  # noqa: E402
    parsed = md2case.md_to_case(text, str(saved))
    assert parsed["id"] == "kr-9001" and parsed["facts"][0]["keywords"] == ["开船后的提单"]
    assert ok.json()["hint"] and "build" in ok.json()["hint"][1]

    # 重复 id / 非法目录 / 校验失败（ec 缺注入与禁止事实）
    dup = c2.post("/api/case/create", json=_kr_body())
    assert dup.status_code == 400 and "已存在" in dup.json()["error"]
    bad_dir = c2.post("/api/case/create", json=_kr_body(id="kr-9002", directory="../evil"))
    assert bad_dir.status_code == 400 and "目录" in bad_dir.json()["error"]
    ec = c2.post("/api/case/create", json=_kr_body(id="ec-9001", dim="错误知识纠正"))
    assert ec.status_code == 400
    assert any("注入" in e for e in ec.json()["errors"])


def test_case_new_nature_and_partial_rule(served, tmp_path):
    """表单性质/部分判据（doc/16 §3.7）：主观落【性质：主观】、部分判据随行；
    落盘 md 可重新解析出性质；禁止事实带部分判据 → 服务端校验拒绝。"""
    import sys
    client, db, _, _ = served
    cases = tmp_path / "cases"
    cases.mkdir()
    app2 = create_app(db, cases_dir=str(cases))
    c2 = TestClient(app2)
    facts = [
        {"kind": "required", "text": "FOB 报价不含海运费和目的港费用", "keywords": [], "turn": None,
         "group": "", "evidence": "外部", "weight": None},
        {"kind": "expected", "text": "报价单应注明付款方式及比例", "keywords": [], "turn": None,
         "group": "", "evidence": "外部", "weight": None, "nature": "主观"},
        {"kind": "expected", "text": "报价单应注明报价有效期", "keywords": [], "turn": None,
         "group": "", "evidence": "外部", "weight": None, "nature": "主观",
         "partial_rule": "只写短期有效、未给具体期限算部分"},
        {"kind": "forbidden", "text": "建议不写付款方式留给谈判", "keywords": [], "turn": None,
         "group": "", "evidence": "外部", "weight": None, "nature": "主观"},
    ]
    prev = c2.post("/api/case/preview", json=_kr_body(id="kr-9003", facts=facts))
    assert prev.status_code == 200 and prev.json()["ok"]
    md = prev.json()["md"]
    assert "- F1：FOB 报价不含海运费和目的港费用 【外部】" in md      # 客观条目不带性质
    assert md.count("【性质：主观】") == 3                            # E1 / E2 / X1
    assert "【部分判据：只写短期有效、未给具体期限算部分】" in md

    ok = c2.post("/api/case/create", json=_kr_body(id="kr-9003", facts=facts))
    assert ok.status_code == 200, ok.text
    saved = cases / "batch-a" / "kr-9003.md"
    assert saved.exists()
    sys.path.insert(0, str(Path("evalset").resolve()))
    import md2case  # noqa: E402
    parsed = md2case.md_to_case(saved.read_text(encoding="utf-8"), str(saved))
    by_id = {f["id"]: f for f in parsed["facts"]}
    assert "nature" not in by_id["F1"]
    assert by_id["E1"]["nature"] == "subjective" and "partial_rule" not in by_id["E1"]
    assert by_id["E2"]["partial_rule"].startswith("只写短期")
    assert by_id["X1"]["nature"] == "subjective"

    # 误用：禁止事实带部分判据 → 预览即报错（doc/16 §3.5）
    bad = facts + [dict(facts[3], text="另一个错误建议", partial_rule="复述即算部分")]
    prev2 = c2.post("/api/case/preview", json=_kr_body(id="kr-9004", facts=bad))
    assert prev2.status_code == 200 and not prev2.json()["ok"]
    assert any("禁止事实不写【部分判据】" in e for e in prev2.json()["errors"])


def test_case_preview_knowledge_check(served):
    """preview 联动 knowledge 校验：引用不存在的注入点 → 错误（不落盘）。"""
    client, db, _, _ = served
    ec_body = _kr_body(id="ec-9002", dim="错误知识纠正",
                       injections=[{"point": "不存在的注入点", "option": "x"}],
                       facts=[{"kind": "forbidden", "text": "错", "keywords": [], "turn": None,
                               "group": "", "evidence": "", "weight": None}])
    prev = client.post("/api/case/preview", json=ec_body)
    assert prev.status_code == 200
    assert not prev.json()["ok"]
    assert any("不存在的注入点" in e for e in prev.json()["errors"])
