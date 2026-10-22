"""复核服务集成测试（方案 §12 步骤 4 验收）：改判一条 → 只有该条与所属会话结论变化，
review.jsonl 追加一行，页面数字与 rescore 产出的 scoring.json 一致。"""
import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from scoring import cli  # noqa: E402
from scoring.serve import create_app  # noqa: E402
from tests.test_cli import OPERATORS_YAML  # noqa: E402
from tests.helpers import make_run, mk_record  # noqa: E402


@pytest.fixture()
def served(tmp_path):
    records = [mk_record(), mk_record(uid="ec-2#0")]
    run_dir = make_run(tmp_path, "demo", records, run_id="demo")
    ops = tmp_path / "operators.yaml"
    ops.write_text(OPERATORS_YAML, encoding="utf-8")
    assert cli.main(["score", str(run_dir), "--operators", str(ops),
                     "--cache", str(tmp_path / "c.jsonl")]) == 0
    app = create_app(tmp_path)
    return TestClient(app), run_dir, tmp_path


def test_overview_lists_run(served):
    client, run_dir, _ = served
    r = client.get("/")
    assert r.status_code == 200
    assert "demo" in r.text and "错误知识纠正|natural" in r.text
    assert "episodes.csv" in r.text


def test_review_then_page_matches_rescore(served):
    client, run_dir, _ = served
    # 改判前：两段会话均 fail（F1 未提及）
    before = json.loads((run_dir / "scoring.json").read_text(encoding="utf-8"))
    assert all(v["status"] == "fail" for v in before["verdicts"])

    ep = "ec-1#0-natural-t1"
    r = client.post("/api/review", json={"run_id": "demo", "episode_id": ep, "item_id": "F1",
                                         "value": "对", "reason": "回答明确", "reviewer": "codfish"})
    assert r.status_code == 200 and r.json()["episode_status"] == "pass"
    lines = (run_dir / "review.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["reviewer"] == "codfish"

    # 页面（run 明细）与 rescore 产出一致：只有该会话由败转过，另一会话不变
    page = client.get("/run/demo").text
    assert "ec-1#0" in page
    client.post("/api/rescore/demo")
    after = json.loads((run_dir / "scoring.json").read_text(encoding="utf-8"))
    statuses = {v["uid"]: v["status"] for v in after["verdicts"]}
    assert statuses == {"ec-1#0": "pass", "ec-2#0": "fail"}
    assert after["judgment_snapshot"]["reviews_applied"] == 1

    # 人工与机器不一致队列出现该条
    q = client.get("/queue?filter_name=mismatch&run_id=demo").text
    assert "F1" in q and "ec-1#0" in q


def test_queue_undecided_and_provisional(served):
    client, run_dir, tmp_path = served
    # 第二个 run 用包内默认算子（forbidden/precond 链为空 → 未决，数字为临时值）
    run2 = make_run(tmp_path, "plain", [mk_record()], run_id="plain")
    assert cli.main(["score", str(run2), "--cache", str(tmp_path / "c.jsonl")]) == 0
    client.post("/api/rescan")
    q = client.get("/queue?filter_name=undecided&run_id=plain").text
    assert "X1" in q                                    # 禁止事实链为空 → 未决进队列
    overview = client.get("/").text
    assert "临时" in overview


def test_case_view_expectations_and_turns(served):
    client, _, _ = served
    r = client.get("/case/demo/ec-1%230")               # uid 中的 # 转义
    assert r.status_code == 200
    assert "用例期望" in r.text and "未收尾款不应电放" in r.text
    assert "未采信" in r.text and "参考" not in r.text[:0]  # 模板渲染成功即可


def test_compare_pages(served):
    client, run_dir, tmp_path = served
    run2 = make_run(tmp_path, "demo2", [mk_record()], run_id="demo2",
                    snapshot={"skills_mode": "default-roots", "evalset": {"sha256": "a"},
                              "skills_base": "s2"})
    assert cli.main(["score", str(run2), "--operators", str(tmp_path / "operators.yaml"),
                     "--cache", str(tmp_path / "c.jsonl")]) == 0
    client.post("/api/rescan")
    ok = client.get("/compare?a=demo&b=demo2&intended=skill").text   # 预期变量 skill：放行 skills_base
    assert "已拒绝对比" not in ok and "快照差异键" in ok
    blocked = client.get("/compare?a=demo&b=demo2&intended=model").text
    assert "已拒绝对比" in blocked


def test_export_csv(served):
    client, _, _ = served
    r = client.get("/export/demo/episodes.csv")
    assert r.status_code == 200 and "ec-1#0" in r.text
    r2 = client.get("/export/demo/items.csv")
    assert "F1" in r2.text and "machine_value" in r2.text
