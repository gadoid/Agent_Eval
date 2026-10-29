"""doc/17 条目计分与权重改造的验收测试（§6 计分与默认规则、§6.4 队列、§5.1 管线）。

覆盖 §10 测试表里在前置迁移之外新增的行为：两点求值统一（C5 修正）、live_items
边界（参考/权重 0 的期望不进、权重 0 的必要仍进）、veto_mode=cap、计分分母 0 →
error、参考必要事实仍触发否决、计分前提/路径、保存管线 e2e。"""
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient  # noqa: E402

from scoring.rules import Rules, live_items, rules_from_dict, session_conclusion  # noqa: E402
from scoring.items import NODES, Item, build_context, extract_items, resolve_plan  # noqa: E402
from scoring.operators import Verdict  # noqa: E402
from scoring.store import Store  # noqa: E402
from tests.helpers import fact, mk_record  # noqa: E402

RULES = rules_from_dict({})


def _conclude(rec, eff, rules=RULES, scoring=None):
    ctx = build_context(rec)
    items = extract_items(ctx)
    return session_conclusion(rec, ctx, items, eff, rules, scoring=scoring)


# ---------------------------------------------------------------- 两点求值（§6.3，C5 修正）

def _mk(facts, block=None):
    """构造会话 + 解析后的计分方案（session_conclusion 吃 resolve_plan 的产物，
    不是 dist 的 scoring 原块）。"""
    case = dict(mk_record()["case"])
    case.pop("scoring", None)
    case["facts"] = facts
    rec = mk_record(case=case)
    return rec, resolve_plan(case, block if block is not None else rec["case"]["scoring"])


def test_two_point_undecided_required_vetoes_unfavorable():
    """C5 修正：9 条对 + 1 条未决的必要事实，不利一次按「错」触发否决 → 区间 0–90，
    不再是 pass（doc/17 §1 C5 的复现场景）。"""
    facts = [fact(f"F{i}", text=f"事实{i}") for i in range(1, 11)]
    block = {"items": {f"F{i}": {"use": "score", "weight": 10.0} for i in range(1, 11)},
             "unassigned": False}
    rec, scoring = _mk(facts, block=block)
    eff = {f"F{i}": Verdict("对", "judge", "f@1") for i in range(1, 10)}
    eff["F10"] = Verdict(None, "", "")                      # 未决
    v = _conclude(rec, eff, scoring=scoring)
    assert v.status == "undecided"
    assert v.score_max == 100 and v.score_min == 0           # 修正前 min=90（区间 90–100 → 误判 pass）
    assert v.score is None                                    # 区间跨及格线，未收敛


def test_veto_mode_cap():
    """veto_mode=cap：否决触发后会话分封顶到 cap（40），不清零。"""
    rules = rules_from_dict({"score": {"veto_mode": "cap", "cap": 40}})
    facts = [fact("F1", text="甲"), fact("X1", "forbidden", "禁止内容")]
    rec, scoring = _mk(facts)
    eff = {"F1": Verdict("对", "judge", "f@1"), "X1": Verdict("采信", "judge", "f@1")}
    v = _conclude(rec, eff, rules, scoring=scoring)
    assert v.status == "fail" and v.score == 40 and "禁止事实被采信" in v.vetoes


def test_required_as_ref_still_vetoes():
    """必要事实设为参考后不再计分，但判「错」仍触发否决（默认规则与用途无关）。"""
    facts = [fact("F1", text="甲", use="ref"), fact("F9", text="乙"),
             fact("X1", "forbidden", "禁止")]
    block = {"items": {"F1": {"use": "ref"}, "F9": {"use": "score", "weight": 100.0},
                       "X1": {"use": "ref"}}, "unassigned": False}
    rec, scoring = _mk(facts, block=block)
    eff = {"F1": Verdict("错", "judge", "f@1"), "F9": Verdict("对", "judge", "f@1"),
           "X1": Verdict("未采信", "judge", "f@1")}
    v = _conclude(rec, eff, scoring=scoring)
    assert v.status == "fail" and v.score == 0 and "必要事实判错" in v.vetoes
    # 判「未提及」不触发否决（只认「错」）
    eff2 = {"F1": Verdict("未提及", "judge", "f@1"), "F9": Verdict("对", "judge", "f@1"),
            "X1": Verdict("未采信", "judge", "f@1")}
    v2 = _conclude(rec, eff2, scoring=scoring)
    assert v2.status == "pass" and v2.score == 100           # 参考条目不影响 F9 满分


def test_all_ref_unassigned_skipped():
    """全部条目参考（计分权重合计 0）→ skipped（§4），不算分不判否决。"""
    facts = [fact("F1", text="甲", use="ref"), fact("X1", "forbidden", "禁止", use="ref")]
    block = {"items": {"F1": {"use": "ref"}, "X1": {"use": "ref"}}, "unassigned": True}
    rec, scoring = _mk(facts, block=block)
    eff = {"F1": Verdict("错", "judge", "f@1"), "X1": Verdict("采信", "judge", "f@1")}
    v = _conclude(rec, eff, scoring=scoring)
    assert v.status == "skipped" and v.invalid_reason == "权重未分配"


def test_scored_precond_and_path():
    """前提/路径可计分（§6.1）：成立/满足 → 100 分；不成立走会话无效门控。"""
    case = dict(mk_record()["case"])
    case.pop("scoring", None)
    case["facts"] = [fact("F1", text="未收尾款不应电放")]
    case["preconditions"] = [{"id": "P1", "turn": 1, "text": "前提成立", "injections": [], "use": "score"}]
    case["path"] = [{"type": "must", "action": {"kind": "load_skill", "skill": "s"},
                     "turn": 1, "use": "score"}]
    from tests.helpers import turn
    rec = mk_record(case=case, turns=[turn(output="答：未收尾款不应电放。", skills=("s",))])
    scoring = resolve_plan(case, rec["case"]["scoring"])
    assert scoring["uses"]["P1"] == "score" and scoring["uses"]["path#1"] == "score"
    eff = {"F1": Verdict("对", "judge", "f@1"), "P1": Verdict("成立", "judge", "p@1"),
           "path#1": Verdict("满足", "rule", "skill-path@1")}
    v = _conclude(rec, eff, scoring=scoring)
    assert v.status == "pass" and v.score == 100
    eff_bad = {"F1": Verdict("对", "judge", "f@1"), "P1": Verdict("不成立", "judge", "p@1"),
               "path#1": Verdict("满足", "rule", "skill-path@1")}
    v2 = _conclude(rec, eff_bad, scoring=scoring)
    assert v2.status == "invalid"                            # 门控与用途无关


def test_all_not_applicable_paths_error():
    """计分路径全部「不适用」→ 分母 0 → error（§6.1），不再兜底满分。"""
    case = dict(mk_record()["case"])
    case.pop("scoring", None)
    case["facts"] = [fact("F1", text="甲", use="ref")]
    case["path"] = [{"type": "must", "action": {"kind": "load_skill", "skill": "s"},
                     "turn": 1, "use": "score"}]
    rec = mk_record(case=case)
    scoring = resolve_plan(case, rec["case"]["scoring"])
    eff = {"F1": Verdict("对", "judge", "f@1"), "path#1": Verdict("不适用", "rule", "skill-path@1")}
    v = _conclude(rec, eff, scoring=scoring)
    assert v.status == "error" and "全部不适用" in v.error


# ---------------------------------------------------------------- live_items 边界（§2.1/§6.4）

def _item(iid, kind):
    return Item(kind, iid, None, None, {})


def test_live_items_membership():
    plan = {"uses": {"F1": "score", "E1": "score", "F0": "score", "E0": "score", "Eref": "ref"},
            "weights": {"F1": 50.0, "E1": 30.0, "F0": 0.0, "E0": 0.0},
            "unassigned": False}
    items = ([_item("P1", "precond"), _item("P2", "precond"), _item("path#1", "path")]
             + [_item(i, "required" if i.startswith("F") else "expected")
                for i in ("F1", "E1", "F0", "E0", "Eref")]
             + [_item("X1", "forbidden")])
    live = live_items(items, {}, plan)
    assert {"F1", "E1"} <= live                              # 权重>0 计分条目
    assert "F0" in live                                      # 权重 0 的必要：节点有默认规则（决定 13）
    assert "E0" not in live and "Eref" not in live          # 权重 0 期望 / 参考期望：不进
    assert {"P1", "path#1", "X1"} <= live                    # 默认规则节点全进
    # 短路：zero 否决已触发（禁止采信）→ 只有未决的前提条目
    live2 = live_items(items, {"X1": "采信"}, plan)
    assert live2 == {"P1", "P2"}
    # 前提不成立短路：只有未决的前提条目（P1 已判不成立，本就不进队列；P2 未决仍进）
    live3 = live_items(items, {"P1": "不成立"}, plan)
    assert live3 == {"P2"}
    # 未分配 → 空集
    assert live_items(items, {}, {**plan, "unassigned": True}) == set()
    # forced 会话：路径不进
    live4 = live_items(items, {}, plan, forced=True)
    assert "path#1" not in live4 and "F1" in live4


def test_store_queue_zero_weight_required_stays(tmp_path):
    """库上验收：权重 0 的必要事实未决仍进队列；权重 0 的期望未决不进。"""
    store = Store(tmp_path / "q.db")
    case = dict(mk_record()["case"])
    case.pop("scoring", None)
    case["facts"] = [fact("F0", text="甲"), fact("F9", text="乙"),
                     fact("E0", "expected", "丙"), fact("X1", "forbidden", "禁止")]
    case["scoring"] = {"items": {"F0": {"use": "score", "weight": 0.0},
                                 "F9": {"use": "score", "weight": 100.0},
                                 "E0": {"use": "score", "weight": 0.0},
                                 "X1": {"use": "ref"}},
                       "unassigned": False}
    from tests.helpers import make_run
    from scoring.cli import ingest_run
    run_dir = make_run(tmp_path, "q", [mk_record(case=case)], run_id="q")
    ingest_run(store, run_dir)
    a = store.answers()[0]
    fp = a["judge_fp_at_run"]
    rows = []
    for iid, kind in (("F0", "required"), ("F9", "required"), ("E0", "expected"), ("X1", "forbidden")):
        rows.append({"schema": 1, "run_id": "q", "episode_id": "e", "uid": "u",
                     "item": {"kind": kind, "id": iid, "turn": None},
                     "machine": {"value": None, "source": "", "operator": "", "reason": ""},
                     "trace": [], "answer_sha": "a1", "item_sha": "s1", "status": "undecided"})
    store.write_verdicts(a["answer_id"], fp, "c1", rows)
    got = {i["item_id"] for i in store.undecided_items("c1")[0]}
    assert got == {"F0", "F9", "X1"}                          # E0 不进，F0/X1（默认规则）进
    store.close()


# ---------------------------------------------------------------- 保存管线 e2e（§5.1 决定 11+16）

PIPE_CASE = """---
id: kr-0301
dim: 知识召回
status: admitted
topic: 测试
granularity: 环节
tags: [来源/人工构造]
---

## 问题
保单号后三位填多少？

## 关键条件
- 出运时间：2025 年 10 月 【轮次：1】【关键词：10 月】

## 必要事实
- F1：保单号后三位应填 400 【外部】【权重：60】
- F2：说明判断依据是出运时间 【外部】【权重：40】

## 参考答案
400。
"""

PIPE_CASE2 = """---
id: kr-0302
title: 前提计分的保单号填写（路径与分组渲染测试）
dim: 知识召回
status: admitted
topic: 测试
granularity: 环节
tags: [来源/人工构造]
---

## 问题
保单号后三位填多少？

## 关键条件
- 问点：保单号填写 【轮次：1】【关键词：保单号】

## 前提
- P1：客户已提供保单 【计分】【权重：30】

## 路径
- 必须：加载 skill trade-documents 【轮次：1】

## 必要事实
- F1：保单号后三位应填 400 【外部】【权重：70】

## 参考答案
400。
"""


@pytest.fixture()
def pipeline_env(tmp_path):
    """临时 cases/dist 目录 + 用例页服务（不动真仓库目录）。"""
    cases = tmp_path / "cases"
    (cases / "batch").mkdir(parents=True)
    (cases / "batch" / "kr-0301.md").write_text(PIPE_CASE, encoding="utf-8")
    dist = tmp_path / "dist"
    dist.mkdir()
    db = str(tmp_path / "pipe.db")
    app = create_app_env(db, cases, dist)
    return TestClient(app), cases, dist, db


def create_app_env(db, cases, dist):
    from scoring.serve import create_app
    return create_app(db, cases_dir=str(cases), dist_dir=str(dist))


def test_save_pipeline_edit_builds_imports_and_rescores(pipeline_env):
    """保存管线（决定 11+16）：编辑用途/权重 → md 归一写回 → 新版本 dist → 导入设为
    当前 → rescore → 报告；check 不通过不落盘。"""
    client, cases, dist, db = pipeline_env
    # 1) 改权重（F1 60/F2 40 → 提交 3:1 名义 → 归一 75/25 写回 md）
    r = client.post("/api/case/kr-0301/edit", json={
        "case_id": "kr-0301",
        "items": [{"id": "F1", "weight": 3}, {"id": "F2", "weight": 1}]})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] and j["version"] and j["rescored"] >= 0
    md = (cases / "batch" / "kr-0301.md").read_text(encoding="utf-8")
    assert "【权重：75】" in md and "【权重：25】" in md        # 归一后的百分比写回
    out = dist / f"evalset-{j['version']}.jsonl"
    assert out.exists()
    s = Store(db)
    assert s.current_snapshot() == j["version"]
    sc = s.case_scoring("kr-0301")
    assert sc["items"]["F1"]["weight"] == 75.0
    fp_before = s.questions()[0]["current_standard_fp"]
    s.close()

    # 2) 只改用途（F2 → 参考）：standard_fp 不变（不重判），快照版本再 +1
    r2 = client.post("/api/case/kr-0301/edit", json={
        "case_id": "kr-0301", "items": [{"id": "F2", "use": "ref"}]})
    assert r2.status_code == 200
    j2 = r2.json()
    assert j2["version"] != j["version"]                      # 每次保存一个新版本
    s = Store(db)
    assert s.questions()[0]["current_standard_fp"] == fp_before   # 标准未变
    assert s.case_scoring("kr-0301")["items"]["F2"]["use"] == "ref"
    s.close()

    # 3) 改条目文本（标准内容）→ 新 standard_fp → 报告待重判
    r3 = client.post("/api/case/kr-0301/edit", json={
        "case_id": "kr-0301", "items": [{"id": "F1", "text": "保单号后三位应填 500"}]})
    assert r3.status_code == 200
    s = Store(db)
    assert s.questions()[0]["current_standard_fp"] != fp_before
    s.close()

    # 4) 转参考时权重被剥（参考带权重是 md 侧 check 报错，接口直接规范化）
    r4 = client.post("/api/case/kr-0301/edit", json={
        "case_id": "kr-0301", "items": [{"id": "F2", "use": "ref", "weight": 10}]})
    assert r4.status_code == 200
    md4 = (cases / "batch" / "kr-0301.md").read_text(encoding="utf-8")
    f2_line = next(l for l in md4.splitlines() if l.startswith("- F2："))
    assert "【参考】" in f2_line and "【权重" not in f2_line

    # 5) 未知条目拒绝
    bad2 = client.post("/api/case/kr-0301/edit", json={
        "case_id": "kr-0301", "items": [{"id": "F99", "weight": 1}]})
    assert bad2.status_code == 400 and "未知条目" in bad2.json()["error"]


def test_save_pipeline_version_autoincrements(pipeline_env):
    """决定 16②：不给版本号自动取号 vYYYYMMDD-N，N 递增；dist 不可变。"""
    import evalset.md2case as m2c  # noqa: F401  （自动取号在管线内）
    client, cases, dist, db = pipeline_env
    vs = []
    for w in (60, 70):
        r = client.post("/api/case/kr-0301/edit", json={
            "case_id": "kr-0301", "items": [{"id": "F1", "weight": w}, {"id": "F2", "weight": 100 - w}]})
        vs.append(r.json()["version"])
    assert vs[0] != vs[1] and vs[1].endswith(str(int(vs[1].rsplit("-", 1)[1])))
    files = sorted(p.name for p in dist.glob("evalset-*.jsonl"))
    assert len(files) == 2


def test_case_page_use_slots_markup(pipeline_env):
    """用例页简要列表：前提/路径行也带用途角标（计分=权重%、参考=参考胶囊），
    详情卡计分行带 data-duse——case.js syncBars 随图例切换重写这三处。"""
    client, cases, dist, db = pipeline_env
    (cases / "batch" / "kr-0302.md").write_text(PIPE_CASE2, encoding="utf-8")
    # structured 用例也在 cases 里：保存管线 build 显式带 --include，不能被默认
    # admitted 口径挤出新快照（整批导入会把它设为当前）
    (cases / "batch" / "kr-0303.md").write_text(
        PIPE_CASE2.replace("id: kr-0302", "id: kr-0303").replace("status: admitted", "status: structured"),
        encoding="utf-8")
    r = client.post("/api/case/kr-0302/edit", json={
        "case_id": "kr-0302", "items": [{"id": "P1", "use": "score", "weight": 30}]})
    assert r.status_code == 200, r.text
    # 保存管线整链回写：md 正文带 # H1 标题头（与 frontmatter title 同源）
    saved_md = (cases / "batch" / "kr-0302.md").read_text(encoding="utf-8")
    assert "\n# 前提计分的保单号填写（路径与分组渲染测试）\n" in saved_md
    out = dist / f"evalset-{r.json()['version']}.jsonl"
    uids = set()
    for line in out.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)                      # dist 里有非用例行（无 uid），跳过
        if row.get("uid"):
            uids.add(row["uid"].split("#")[0])
    assert {"kr-0301", "kr-0302", "kr-0303"} <= uids
    page = client.get("/case/kr-0302").text
    assert 'data-use-slot="P1"' in page and 'data-use-slot="path#1"' in page
    assert 'data-use-slot="F1"' in page and 'data-duse="P1"' in page

    def slot(iid):
        m = re.search(r'<span class="([^"]*)" data-use-slot="%s">([^<]*)</span>' % re.escape(iid), page)
        return m.groups() if m else (None, None)
    assert slot("P1") == ("rv-weight rv-wpct-chip", "30%")     # 计分前提 → 权重角标
    assert slot("path#1") == ("rv-pill", "参考")                 # 缺省参考路径 → 参考胶囊
    # 计分/参考两组容器：参考组（标题+列表）初始隐藏，case.js classifyRows 随用途
    # 归位行；计分组带空态提示（全部切参考时由 JS 显出）
    assert 'id="item-ref-list" hidden' in page and 'id="rv-ref-head" hidden' in page
    assert "判定条目 · 计分" in page and 'id="item-score-empty"' in page
    # 标题位：h1 显 title，id 以小字缀后；概览展示真正的关键条件（槽位：值）而非前提/路径
    assert "<h1>前提计分的保单号填写" in page and 'class="rv-h1-id">kr-0302' in page
    assert "问点：保单号填写" in page and "关键词" in page
    # 列表页：标题做主链接、id 副标；标签可筛（kr-0302 带 来源/人工构造）
    lst = client.get("/", params={"tag": "来源/人工构造"}).text
    assert "前提计分的保单号填写" in lst and 'rv-case-id-sub">kr-0302' in lst
    lst_none = client.get("/", params={"tag": "来源/现场记录"}).text
    assert "kr-0302" not in lst_none


REF_CASE = """---
id: kr-0401
dim: 知识召回
topic: 测试
granularity: 细节
status: admitted
answerable: false
拒答权重: 80
tags: [来源/人工构造]
---

## 问题
舱单资料最晚开船前几天提交？

## 关键条件
- 业务：舱单

## 参考答案
依据资料无法回答。

## 备注
不可回答题：期望说明依据资料无法回答。
"""


def test_save_pipeline_refusal_weight(pipeline_env):
    """拒答权重（饼图编辑器拖杠）：拒答伪条目不在 edit 的 items 集内（带去即 400，
    前端把它从 items 里排除）；改拒答权重走 refusal_weight 字段落 md，保存时按
    决定 12 归一写回——唯一计分条目恒回 100。"""
    client, cases, dist, db = pipeline_env
    (cases / "batch" / "kr-0401.md").write_text(REF_CASE, encoding="utf-8")

    bad = client.post("/api/case/kr-0401/edit", json={
        "case_id": "kr-0401", "items": [{"id": "refusal", "weight": 60}]})
    assert bad.status_code == 400 and "未知条目" in bad.json()["error"]

    r = client.post("/api/case/kr-0401/edit", json={
        "case_id": "kr-0401", "refusal_weight": 60})
    assert r.status_code == 200, r.text
    md = (cases / "batch" / "kr-0401.md").read_text(encoding="utf-8")
    assert "拒答权重: 100" in md                       # 单一计分条目归一回 100
    s = Store(db)
    assert s.case_scoring("kr-0401")["items"]["refusal"]["weight"] == 100.0
    s.close()
    # 拒答行进判定条目列表：角标显示权重、data-fixed 标记固定计分不可切
    page = client.get("/case/kr-0401").text
    assert 'data-use-slot="refusal" data-fixed="1"' in page
    assert ">拒答</span>" in page and "100%" in page
