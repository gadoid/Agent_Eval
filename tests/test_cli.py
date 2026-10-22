"""CLI 端到端（方案 §12 步骤 1/2 验收）：score 落盘、rescore 不调用算子且改判生效。"""
import json

from scoring import cli
from scoring.chain import load_verdicts
from scoring.effective import Review, append_review
from tests.helpers import CountingFact, make_run, mk_record

OPERATORS_YAML = """schema: 1
operators:
  keyword:     {impl: keyword}
  skill-path:  {impl: skill-path}
  fake-fact:   {impl: "tests.helpers:FakeFact"}
  fake-forbid: {impl: "tests.helpers:FakeForbidden"}
  fake-refusal: {impl: "tests.helpers:FakeRefusal"}
  fake-precond: {impl: "tests.helpers:FakePrecond"}
chains:
  required:  [keyword, fake-fact]
  expected:  [keyword, fake-fact]
  forbidden: [fake-forbid]
  precond:   [fake-precond]
  path:      [skill-path]
  refusal:   [fake-refusal]
"""

COUNTING_YAML = OPERATORS_YAML.replace('fake-fact:   {impl: "tests.helpers:FakeFact"}',
                                       'fake-fact:   {impl: "tests.helpers:CountingFact"}')


def test_score_writes_verdicts_and_scoring(tmp_path, capsys):
    records = [mk_record(), mk_record(uid="ec-2#0")]
    run_dir = make_run(tmp_path, "demo", records)
    ops = tmp_path / "operators.yaml"
    ops.write_text(OPERATORS_YAML, encoding="utf-8")
    rc = cli.main(["score", str(run_dir), "--operators", str(ops), "--cache", str(tmp_path / "c.jsonl")])
    assert rc == 0
    rows = load_verdicts(run_dir)
    assert len(rows) == 4                                 # 2 会话 × (F1 + X1)
    assert {r["item"]["kind"] for r in rows} == {"required", "forbidden"}
    report = json.loads((run_dir / "scoring.json").read_text(encoding="utf-8"))
    snap = report["judgment_snapshot"]
    assert snap["schema"] == 2 and "fake-fact@1" in snap["operators"]
    assert snap["reviews_applied"] == 0 and snap["reviews_voided"] == 0
    assert snap["operators_config_sha"] and snap["policy_sha"]
    assert "判定" in capsys.readouterr().out


def test_rescore_applies_review_without_judging(tmp_path):
    CountingFact.calls = 0
    run_dir = make_run(tmp_path, "demo2", [mk_record()])
    ops = tmp_path / "counting.yaml"
    ops.write_text(COUNTING_YAML, encoding="utf-8")
    cli.main(["score", str(run_dir), "--operators", str(ops), "--cache", str(tmp_path / "c2.jsonl")])
    assert CountingFact.calls == 1
    report = json.loads((run_dir / "scoring.json").read_text(encoding="utf-8"))
    assert report["verdicts"][0]["status"] == "fail"      # counting 判"未提及" → 失败

    row = next(r for r in load_verdicts(run_dir) if r["item"]["id"] == "F1")
    append_review(run_dir, Review(run_id="demo2", episode_id=row["episode_id"], item_id="F1",
                                  value="对", answer_sha=row["answer_sha"], reason="回答明确",
                                  reviewer="codfish"))
    rc = cli.main(["rescore", str(run_dir)])
    assert rc == 0
    assert CountingFact.calls == 1                        # rescore 未调用任何算子
    report2 = json.loads((run_dir / "scoring.json").read_text(encoding="utf-8"))
    assert report2["judgment_snapshot"]["reviews_applied"] == 1
    assert report2["verdicts"][0]["status"] == "pass"     # 改判生效，会话由败转过
    # 作废的改判（sha 不匹配）
    append_review(run_dir, Review(run_id="demo2", episode_id=row["episode_id"], item_id="F1",
                                  value="错", answer_sha="过期sha", reviewer="codfish"))
    cli.main(["rescore", str(run_dir)])
    report3 = json.loads((run_dir / "scoring.json").read_text(encoding="utf-8"))
    assert report3["judgment_snapshot"]["reviews_voided"] == 1
    assert report3["verdicts"][0]["status"] == "pass"
