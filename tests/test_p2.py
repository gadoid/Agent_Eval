"""P2 测法与执行的测试（doc/15 §3.9/§3.10、§4.7）。

覆盖：新 dist 格式导入（快照头/顶层权重/sidecar 测法）、诊断会话不进正式分、
标准对比视图取数。runner 侧测法执行/诊断标记在 runner/tests（e2e 走真 CLI）。"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scoring import views  # noqa: E402
from scoring.rules import load_rules, default_rules_path  # noqa: E402
from scoring.store import Store  # noqa: E402
from tests.helpers import fact, make_run, mk_record  # noqa: E402
from tests.test_cli import OPERATORS_YAML  # noqa: E402
from scoring import cli  # noqa: E402

RULES, RULES_SHA = load_rules(default_rules_path())


def _new_dist(path: Path, version: str, weights=None, rewrite=False) -> Path:
    """新格式 dist（P2）：首行快照头 + 行顶层 weights + 同名 sidecar。"""
    case = dict(mk_record()["case"])
    case["facts"] = [fact("F1", text="甲"), fact("X1", "forbidden", "禁止")]
    rows = [dict(case, uid="ec-1#0", variant_index=0, variant=None,
                 evalset_version=version)]      # 其余字段沿用 mk_record 的 case：qkey 与答案侧一致
    if weights:
        rows[0]["weights"] = weights
    if rewrite:
        rows.append(dict(rows[0], uid="ec-1#1", variant_index=1,
                         variant={"id": "V1", "rewrite": "诱导"},
                         turns=[{"role": "user", "content": "诱导写法"}]))      # 变体输入不同 → 新 qkey
    path.write_text(
        json.dumps({"snapshot": {"version": version, "method_sha": "abc123", "include": "admitted"}},
                   ensure_ascii=False) + "\n"
        + "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    path.with_name(path.stem + ".snapshot.json").write_text(json.dumps({
        "version": version, "method_sha": "abc123",
        "method": {"modes": {"default": "natural", "错误知识纠正": "forced"},
                   "run_rewrites": ["口语"], "anchors": []}}, ensure_ascii=False), encoding="utf-8")
    return path


def test_import_new_dist_header_weights_and_sidecar_method(tmp_path):
    """新 dist 导入：跳过快照头；顶层 weights 落 item_weights；sidecar 测法进快照表。"""
    db = str(tmp_path / "p2.db")
    store = Store(db)
    dist = _new_dist(tmp_path / "dist.jsonl", "vp2", weights={"F1": 3, "X1": 1})
    assert cli.main(["specs", "import", str(dist), "--db", db]) == 0
    assert store.current_snapshot() == "vp2"                       # 版本从快照头取
    snap = store.snapshot("vp2")
    assert snap["method"]["modes"].get("错误知识纠正") == "forced"  # 测法来自 sidecar（非仓库默认）
    w, wfp, inc = store.effective_weights("ec-1")
    assert w == {"F1": 100.0, "X1": 100.0}      # 计分组与禁止组各自换算（组内单成员 → 100%）
    spec = store.spec_of(store.questions()[0]["qkey"],
                         store.questions()[0]["current_standard_fp"])
    assert "weights" not in spec and all("weight" not in f for f in spec["facts"])
    store.close()


def test_diagnostic_runs_excluded_from_formal_scores(tmp_path):
    """C5：诊断 run 的会话不进正式分（formal_rows 过滤），对比页诊断视图可见。"""
    db = str(tmp_path / "diag.db")
    store = Store(db)
    dist = _new_dist(tmp_path / "dist.jsonl", "v1")
    assert cli.main(["specs", "import", str(dist), "--db", db]) == 0
    ops = tmp_path / "ops.yaml"; ops.write_text(OPERATORS_YAML, encoding="utf-8")
    run_f = make_run(tmp_path, "formal", [mk_record()], run_id="formal")
    run_d = make_run(tmp_path, "diag", [mk_record(mode="forced")], run_id="diag",
                     snapshot={"skills_mode": "default-roots", "evalset": {"sha256": "a"}})
    assert cli.main(["score", str(run_f), "--operators", str(ops), "--db", db]) == 0
    assert cli.main(["score", str(run_d), "--operators", str(ops), "--db", db]) == 0
    with store.lock, store.conn:                                    # 标记 diag run 为诊断执行
        store.conn.execute("UPDATE runs SET diagnostic=1 WHERE run_id='diag'")
    rows = store.answer_view_rows(_chain_sha(store), RULES_SHA)
    formal = views.formal_rows(rows)
    diag = views.diagnostic_rows(rows)
    assert {r["run_id"] for r in formal} == {"formal"} and {r["run_id"] for r in diag} == {"diag"}
    assert isinstance(views.score_summary(formal, RULES)["dims"], dict)   # 正式侧可聚合（未决数据可为空）
    store.close()


def _chain_sha(store):
    return store.chains()[0]["chain_sha"] if store.chains() else "c1"


def test_standard_compare_view(tmp_path):
    """标准对比（doc/15 §4.7 P2）：同题两份标准下结论 diff；标准相同 → same_standard 全量。"""
    db = str(tmp_path / "std.db")
    store = Store(db)
    dist = _new_dist(tmp_path / "d1.jsonl", "vA")
    assert cli.main(["specs", "import", str(dist), "--db", db]) == 0
    ops = tmp_path / "ops.yaml"; ops.write_text(OPERATORS_YAML, encoding="utf-8")
    run_dir = make_run(tmp_path, "r1", [mk_record()], run_id="r1")
    assert cli.main(["score", str(run_dir), "--operators", str(ops), "--db", db]) == 0
    # vB：改标准（F1 文字变化 → standard_fp 变）
    dist_b = _new_dist(tmp_path / "d2.jsonl", "vB")
    lines = [json.loads(l) for l in dist_b.read_text(encoding="utf-8").splitlines() if l.strip()]
    for r in lines[1:]:
        r["facts"][0]["text"] = "改写后的事实文本"
        r["evalset_version"] = "vB"
    lines[0]["snapshot"]["version"] = "vB"
    dist_b.write_text("".join(json.dumps(l, ensure_ascii=False) + "\n" for l in lines), encoding="utf-8")
    assert cli.main(["specs", "import", str(dist_b), "--db", db]) == 0
    rep = store.standard_compare("vA", "vB", _chain_sha(store), RULES_SHA)
    assert rep["shared"] == 1 and rep["same_standard"] == 0        # 标准确实不同
    assert rep["pending"] and rep["pending"][0]["missing"] in ("A", "B")  # 只在一侧判过 → 待补判
    # 同快照自比 → 全部 same
    rep2 = store.standard_compare("vA", "vA", "c1", RULES_SHA)
    assert rep2["same_standard"] == rep2["shared"] == 1
    store.close()


def test_serve_compare_standard_section(tmp_path):
    """/compare：标准对比块渲染（选择器 + same 提示）；诊断计数提示。"""
    from fastapi.testclient import TestClient
    from scoring.serve import create_app
    db = str(tmp_path / "svc.db")
    store = Store(db)
    dist = _new_dist(tmp_path / "d.jsonl", "vA")
    assert cli.main(["specs", "import", str(dist), "--db", db]) == 0
    store.close()
    client = TestClient(create_app(db))
    page = client.get("/compare?std_a=vA&std_b=vA")
    assert page.status_code == 200
    assert "标准对比" in page.text and "标准完全相同" in page.text
    assert "诊断" not in page.text or "0 段" in page.text or "std-a" in page.text
