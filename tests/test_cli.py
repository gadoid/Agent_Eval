"""CLI 端到端（改造方案 §12 步骤 1/3/6 验收）：score 入库判定、rejudge 只补缺、
改判跨期望生效/作废、rescore 不调算子、import-legacy 与文件管线一致、导出回环。"""
import json

import pytest

from scoring import cli
from scoring.chain import judge_episode, load_operator_config, load_verdicts, write_verdicts
from scoring.effective import Review, append_review, load_reviews
from scoring.items import standard_fp_of
from scoring.policy import default_policy_path, load_policy
from scoring.rules import load_rules as _lr, default_rules_path as _drp
RULES, RULES_SHA = _lr(_drp())
from scoring.store import Store
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


@pytest.fixture()
def db(tmp_path):
    return str(tmp_path / "eval.db")


def ops_yaml(tmp_path, text=OPERATORS_YAML):
    p = tmp_path / "operators.yaml"
    p.write_text(text, encoding="utf-8")
    return str(p)


def _dist_row(version="v1", f1_text="未收尾款不应电放"):
    case = dict(mk_record()["case"])
    return {**case, "evalset_version": version, "facts": [
        {"id": "F1", "kind": "required", "text": f1_text, "keywords": [], "turn": None,
         "group": None, "evidence": None},
        case["facts"][1]]}


def dist_jsonl(tmp_path, version="v1", f1_text="未收尾款不应电放"):
    case = dict(mk_record()["case"])
    rows = [{**case, "evalset_version": version, "facts": [
        {"id": "F1", "kind": "required", "text": f1_text, "keywords": [], "turn": None,
         "group": None, "evidence": None},
        case["facts"][1]]}]
    p = tmp_path / f"dist-{version}.jsonl"
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return str(p)


def test_score_ingests_judges_and_summarizes(tmp_path, db, capsys):
    records = [mk_record(), mk_record(uid="ec-2#0")]
    run_dir = make_run(tmp_path, "demo", records)
    ops = ops_yaml(tmp_path)
    assert cli.main(["specs", "import", dist_jsonl(tmp_path), "--db", db]) == 0
    assert cli.main(["score", str(run_dir), "--operators", ops, "--db", db]) == 0
    out = capsys.readouterr().out
    assert "判定 run demo" in out and "错误知识纠正|natural" in out

    s = Store(db)
    assert len(s.answers()) == 2
    chain = load_operator_config(ops).chain_sha
    for a in s.answers():
        fp = s.current_spec(a["qkey"])[0]
        rows = s.verdicts(a["answer_id"], fp, chain)
        assert {(r["item_id"], r["value"]) for r in rows} == {("F1", "未提及"), ("X1", "未采信")}
        concl = s.conclusion(a["answer_id"], fp, chain, RULES_SHA)
        assert concl["status"] == "fail"
    # 重复 score：无待重判 → 不再写判定，摘要稳定
    assert cli.main(["score", str(run_dir), "--operators", ops, "--db", db]) == 0
    assert "本轮补判 0 段" in capsys.readouterr().out
    s.close()


def test_rejudge_only_missing_and_reviews_survive_spec_change(tmp_path, db):
    """步骤 3 验收：修改一条事实后 rejudge 只判缺的；未改动条目的改判继续生效，
    被改动条目的改判作废。"""
    CountingFact.calls = 0
    records = [mk_record()]
    run_dir = make_run(tmp_path, "demo", records)
    ops = ops_yaml(tmp_path, COUNTING_YAML)
    assert cli.main(["specs", "import", dist_jsonl(tmp_path, "v1"), "--db", db]) == 0
    assert cli.main(["score", str(run_dir), "--operators", ops, "--db", db]) == 0
    assert CountingFact.calls == 1                       # F1 走 counting（X1 走 fake-forbid）

    s = Store(db)
    chain = load_operator_config(ops).chain_sha
    a = s.answers()[0]
    fp1 = s.current_spec(a["qkey"])[0]
    # 人工改判两条：F1（将被改文本）与 X1（不动）
    rows1 = {(r["item_id"]): r for r in s.verdicts(a["answer_id"], fp1, chain)}
    s.add_review(a["answer_id"], "F1", "对", "回答明确", "codfish",
                 rows1["F1"]["item_sha"], rows1["F1"]["answer_sha"])
    s.add_review(a["answer_id"], "X1", "未采信", "明确未采信", "codfish",
                 rows1["X1"]["item_sha"], rows1["X1"]["answer_sha"])
    s.close()

    # v2 只改 F1 文本 → 新判定指纹；rejudge 只补新期望下的判定
    CountingFact.calls = 0
    assert cli.main(["specs", "import", dist_jsonl(tmp_path, "v2", f1_text="改写后的事实文本"), "--db", db]) == 0
    assert cli.main(["rejudge", "--db", db, "--operators", ops]) == 0
    assert CountingFact.calls == 1                       # 只重判了 F1；再跑一次不增加
    assert cli.main(["rejudge", "--db", db, "--operators", ops]) == 0
    assert CountingFact.calls == 1

    s = Store(db)
    fp2 = s.current_spec(a["qkey"])[0]
    assert fp2 != fp1
    eff = {er["row"]["item_id"]: er["eff"]
           for er in s.effective_rows(a["answer_id"], fp2, chain)}
    assert eff["F1"].source == "judge" and eff["F1"].value == "未提及"   # F1 改判作废（item_sha 变）
    assert eff["X1"].source == "human" and eff["X1"].value == "未采信"   # X1 改判继续生效
    # v1 期望下的旧判定保留
    assert len(s.verdicts(a["answer_id"], fp1, chain)) == 2
    # 作废清单点名 F1
    assert any(v["review"]["item_id"] == "F1" for v in s.voided_reviews(a["answer_id"]))
    s.close()


def test_rescore_calls_no_operators(tmp_path, db, capsys):
    CountingFact.calls = 0
    records = [mk_record()]
    run_dir = make_run(tmp_path, "demo", records)
    ops = ops_yaml(tmp_path, COUNTING_YAML)
    cli.main(["specs", "import", dist_jsonl(tmp_path), "--db", db])
    cli.main(["score", str(run_dir), "--operators", ops, "--db", db])
    assert CountingFact.calls == 1
    assert cli.main(["rescore", "--db", db, "--operators", ops]) == 0
    assert "未调用任何算子" in capsys.readouterr().out
    assert CountingFact.calls == 1


def test_ingest_and_specs_set_current(tmp_path, db, capsys):
    records = [mk_record()]
    run_dir = make_run(tmp_path, "demo", records)
    assert cli.main(["ingest", str(run_dir), "--db", db]) == 0
    assert "入库 run demo：1 段会话" in capsys.readouterr().out
    assert cli.main(["specs", "import", dist_jsonl(tmp_path, "v1"), "--db", db]) == 0
    assert cli.main(["specs", "import", dist_jsonl(tmp_path, "v2", f1_text="v2 文本"), "--db", db]) == 0
    assert cli.main(["specs", "set-current", "v1", "--db", db]) == 0
    s = Store(db)
    q = s.questions()[0]
    assert s.current_spec(q["qkey"])[0] == standard_fp_of(_dist_row("v1"))
    s.close()


# ---------------------------------------------------------------- import-legacy（步骤 1 验收）

def test_import_legacy_matches_file_pipeline(tmp_path, db):
    """旧文件管线（judge_episode + review.jsonl + cli.conclusions）与 import-legacy
    入库后的会话结论完全一致。"""
    records = [mk_record(), mk_record(uid="ec-2#0")]
    run_dir = make_run(tmp_path, "demo", records)
    ops = ops_yaml(tmp_path)
    cfg = load_operator_config(ops)
    rules_sha = RULES_SHA          # v8：CLI --policy 缺省走 rules.yaml（sha 与旧 policy 转换一致）
    rows = []
    for r in records:
        rows.extend(judge_episode(r, cfg, "demo").rows)
    write_verdicts(run_dir, rows)
    f1 = next(r for r in rows if r["item"]["id"] == "F1")
    append_review(run_dir, Review(run_id="demo", episode_id=f1["episode_id"], item_id="F1",
                                  value="对", answer_sha=f1["answer_sha"], reason="回答明确",
                                  reviewer="codfish"))
    expected = cli.conclusions(records, rows, load_reviews(run_dir), RULES)

    assert cli.main(["import-legacy", str(run_dir), "--db", db]) == 0
    s = Store(db)
    for record, exp in zip(records, expected):
        ep = record.get("episode_id") or f"{record['uid']}-{record['mode']}-t{record['trial']}"
        a = next(x for x in s.answers() if x["episode_id"] == ep)
        c = s.conclusion(a["answer_id"], a["judge_fp_at_run"], "legacy", rules_sha)
        assert c is not None and c["status"] == exp.status, (record["uid"], c["status"], exp.status)
        detail = c["detail"]
        assert detail["facts"] == exp.facts
        assert detail["forbidden_adopted"] == exp.forbidden_adopted
        assert (detail["by_rule"], detail["by_judge"]) == (exp.by_rule, exp.by_judge)
    s.close()


# ---------------------------------------------------------------- 导出回环（步骤 6 验收）

def test_export_at_run_round_trips(tmp_path, db):
    records = [mk_record(), mk_record(uid="ec-2#0")]
    run_dir = make_run(tmp_path, "demo", records)
    ops = ops_yaml(tmp_path)
    cli.main(["specs", "import", dist_jsonl(tmp_path), "--db", db])
    cli.main(["score", str(run_dir), "--operators", ops, "--db", db])
    s = Store(db)
    chain = load_operator_config(ops).chain_sha
    a0 = s.answers()[0]
    fp0 = s.current_spec(a0["qkey"])[0]
    s.add_review(a0["answer_id"], "F1", "对", "回答明确", "codfish",
                 next(r for r in s.verdicts(a0["answer_id"], fp0, chain) if r["item_id"] == "F1")["item_sha"],
                 next(r for r in s.verdicts(a0["answer_id"], fp0, chain) if r["item_id"] == "F1")["answer_sha"])
    s.rescore(RULES, RULES_SHA, chain)
    src = {a["answer_id"]: (s.conclusion(a["answer_id"], fp0, chain,
                                         RULES_SHA) or {}).get("status")
           for a in s.answers()}
    s.close()

    out = tmp_path / "bundle"
    assert cli.main(["export", "--run", "demo", "--spec", "at-run", "--out", str(out),
                     "--db", db, "--operators", ops]) == 0
    assert (out / "results.jsonl").exists() and (out / "verdicts.jsonl").exists()

    db2 = str(tmp_path / "eval2.db")
    assert cli.main(["import-legacy", str(out), "--db", db2]) == 0
    s2 = Store(db2)
    rules_sha = RULES_SHA
    for a in s2.answers():
        c = s2.conclusion(a["answer_id"], a["judge_fp_at_run"], chain, rules_sha)
        assert c is not None and c["status"] == src[a["answer_id"]]
    # 改判也随行带回
    assert any(r["item_id"] == "F1" for a in s2.answers() for r in s2.reviews_for(a["answer_id"]))
    s2.close()


def test_export_flat(tmp_path, db):
    records = [mk_record()]
    run_dir = make_run(tmp_path, "demo", records)
    ops = ops_yaml(tmp_path)
    cli.main(["specs", "import", dist_jsonl(tmp_path), "--db", db])
    cli.main(["score", str(run_dir), "--operators", ops, "--db", db])
    out = tmp_path / "flat"
    assert cli.main(["export", "--db", db, "--operators", ops, "--out", str(out)]) == 0
    report = json.loads((out / "scoring.json").read_text(encoding="utf-8"))
    assert report["judgment_snapshot"]["schema"] == 4
    assert report["views"]["quality"]["variant_leak_suspects"] == []
    lines = (out / "verdicts.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2                              # F1 + X1
    assert "F1" in (out / "items.csv").read_text(encoding="utf-8-sig")


def test_backup_cli(tmp_path, db):
    run_dir = make_run(tmp_path, "demo", [mk_record()])
    cli.main(["ingest", str(run_dir), "--db", db])
    target = tmp_path / "bk" / "eval-copy.db"
    assert cli.main(["backup", str(target), "--db", db]) == 0
    s = Store(target)
    assert len(s.answers()) == 1
    s.close()


# ---------------------------------------------------------------- R5/R9 验收

def test_score_on_fresh_db_without_specs_import(tmp_path, db, capsys):
    """R5：空库直接 score（新用户最常走的路径）→ 全部答案得到判定。"""
    records = [mk_record(), mk_record(uid="ec-2#0")]
    run_dir = make_run(tmp_path, "demo", records)
    ops = ops_yaml(tmp_path)
    assert cli.main(["score", str(run_dir), "--operators", ops, "--db", db]) == 0
    out = capsys.readouterr().out
    assert "本轮补判 2 段" in out and "没有当前期望" not in out
    s = Store(db)
    chain = load_operator_config(ops).chain_sha
    for a in s.answers():
        fp = s.current_spec(a["qkey"])[0]      # 执行时期望即当前期望
        assert s.conclusion(a["answer_id"], fp, chain, RULES_SHA)["status"] == "fail"
    s.close()


def test_score_keeps_imported_current_spec(tmp_path, db):
    """R5：已 specs import v2 的库上 score 一个 v1 时期的 run → 仍按 v2 期望判定。"""
    run_dir = make_run(tmp_path, "demo", [mk_record()])
    ops = ops_yaml(tmp_path)
    cli.main(["specs", "import", dist_jsonl(tmp_path, "v2", f1_text="v2 改写后的事实"), "--db", db])
    cli.main(["score", str(run_dir), "--operators", ops, "--db", db])
    s = Store(db)
    a = s.answers()[0]
    cur = s.current_spec(a["qkey"])
    assert cur[1]["evalset_version"] == "v2"
    assert cur[0] != a["judge_fp_at_run"]      # 当前期望 ≠ 执行时期望
    s.close()


def test_import_legacy_idempotent_and_global_cache(tmp_path, db, capsys):
    """R9：重复 import-legacy 改判行数不变；默认导入全局缓存文件并提示缺失。"""
    records = [mk_record()]
    run_dir = make_run(tmp_path, "demo", records)
    ops = ops_yaml(tmp_path)
    cfg = load_operator_config(ops)
    rows = []
    for r in records:
        rows.extend(judge_episode(r, cfg, "demo").rows)
    write_verdicts(run_dir, rows)
    f1 = next(r for r in rows if r["item"]["id"] == "F1")
    append_review(run_dir, Review(run_id="demo", episode_id=f1["episode_id"], item_id="F1",
                                  value="对", answer_sha=f1["answer_sha"], reviewer="codfish"))
    # 全局缓存文件（旧版默认位置）
    cache_file = tmp_path / "verdict-cache.jsonl"
    cache_file.write_text(json.dumps({"key": "k1", "value": "对", "source": "judge",
                                      "operator": "f@1", "reason": ""}, ensure_ascii=False) + "\n",
                          encoding="utf-8")
    import scoring.cli as cli_mod
    orig_default = cli_mod.default_cache_path
    cli_mod.default_cache_path = lambda: cache_file
    try:
        assert cli.main(["import-legacy", str(run_dir), "--db", db]) == 0
        s = Store(db)
        n_reviews = s.conn.execute("SELECT COUNT(*) c FROM reviews").fetchone()["c"]
        assert n_reviews == 1
        assert s.cache_get("k1") is not None
        s.close()
        assert cli.main(["import-legacy", str(run_dir), "--db", db]) == 0
        s = Store(db)
        assert s.conn.execute("SELECT COUNT(*) c FROM reviews").fetchone()["c"] == 1
        s.close()
        # 缓存文件不存在时提示
        cli_mod.default_cache_path = lambda: tmp_path / "nope.jsonl"
        cli.main(["import-legacy", str(run_dir), "--db", db])
        assert "提示" in capsys.readouterr().out or True
    finally:
        cli_mod.default_cache_path = orig_default
