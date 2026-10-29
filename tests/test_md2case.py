"""md2case v2 测试：解析等价、check 报错/提示清单、--docs、编译、diff、rev 联动、render 往返。"""
import json
import sys
from pathlib import Path

import pytest
import yaml

EVALSET = Path(__file__).resolve().parents[1] / "evalset"
sys.path.insert(0, str(EVALSET))

import md2case  # noqa: E402

KNOWLEDGE = """
- id: 收汇保单尾号
  file: trade-company.md
  key: 之前出运业务保单号后三位为 400，之后为 500
  tags: [业务/收汇]
  options:
    反转: {value: "之前出运业务保单号后三位为 500，之后为 400", rev: 1}
    删除: {value: "", rev: 1}
"""

EC_CASE = """---
id: ec-0102
dim: 错误知识纠正
status: admitted
topic: 收汇记录
granularity: 细节
tags: [场景/用户更正, 难点/规则反转, 来源/人工构造]
---

## 对话
- 这单 2025 年 10 月出运，保单号后三位填多少？
- 不对，11.15 之前出运的是 400，你引用的规则写反了。那这单应该填多少？

## 关键条件
- 出运时间：2025 年 10 月 【对象：A】【轮次：1】【关键词：10 月、十月】
- 收汇规则：11.15 为界 【轮次：2】【关键词：11.15】

## 注入
- J1：@收汇保单尾号/反转

## 前提
- P1：2025 年 10 月出运的这单，保单号后三位填 500 【轮次：1】【注入：J1】

## 必要事实
- F1：2025 年 10 月出运的这单，保单号后三位应填 400 【轮次：2】【外部】

## 禁止事实
- X1：2025 年 10 月出运的这单，保单号后三位应填 500 【轮次：2】【证据：R1】

## 路径
- 必须：加载 skill trade-company 【轮次：1】

## 证据
- R1：@收汇保单尾号

## 参考答案
按更正后的规则填 400。

## 变体
- V1：11.15 前出运的是 400 吧？这票尾号三位到底填啥？ 【轮次：2】【改写：口语】

## 备注
动态注入示例。
"""

KR_CASE = """---
id: kr-0001
dim: 知识召回
status: admitted
tags: [来源/人工构造]
---

## 问题
太保的倒签保函需要在什么时候提供？

## 关键条件
- 保险公司：太保
- 单据：倒签保函

## 必要事实
- F1：太保的倒签保函在开船时提供 【证据：R1】

## 证据
- R1：trade-documents:trade-documents.md / 保险 ｜ 原文：倒签保函开船时提供

## 参考答案
太保的倒签保函在开船时提供。
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    """隔离的 evalset 环境（knowledge、rev state、cases）。"""
    kd = tmp_path / "knowledge"
    kd.mkdir()
    (kd / "trade-company.yaml").write_text(KNOWLEDGE, encoding="utf-8")
    monkeypatch.setattr(md2case, "KNOWLEDGE_DIR", kd)
    monkeypatch.setattr(md2case, "REV_STATE_PATH", tmp_path / ".knowledge-state.json")
    return tmp_path


def parse_ok(env, text, name):
    p = env / f"{name}.md"
    p.write_text(text, encoding="utf-8")
    return md2case.md_to_case(text, str(p))


def parse_err(env, text, name="t-0001"):
    with pytest.raises(md2case.CaseError) as ei:
        parse_ok(env, text, name)
    return ei.value


# ---------------------------------------------------------------- 解析与编译

def test_ec_case_compiles_to_schema2(env):
    case = parse_ok(env, EC_CASE, "ec-0102")
    assert len(case["turns"]) == 2 and case["assistant_refs"][0] is None
    rows = md2case.compile_case(case, md2case.load_knowledge([md2case.KNOWLEDGE_DIR]), "vX")
    assert [r["uid"] for r in rows] == ["ec-0102#0", "ec-0102#1"]
    r0 = rows[0]
    assert r0["schema"] == 2 and r0["variant"] is None
    assert r0["injections"][0]["point"] == "收汇保单尾号"
    assert r0["injections"][0]["rev"] == 1
    assert r0["injections"][0]["value"].startswith("之前出运业务保单号后三位为 500")
    assert {"tag": "业务/收汇", "from": "point:收汇保单尾号"} in r0["tags"]
    assert r0["evidence"][0]["quote"].startswith("之前出运业务")
    kinds = {f["kind"] for f in r0["facts"]}
    assert kinds == {"required", "forbidden"}
    assert r0["path"][0] == {"type": "must", "action": {"kind": "load_skill", "skill": "trade-company"}, "turn": 1}
    assert rows[1]["turns"][0]["content"] == r0["turns"][0]["content"]
    assert rows[1]["turns"][1]["content"].startswith("11.15 前出运")


def test_four_input_forms_equivalent(env):
    head = "---\nid: mt-0001\ndim: 多轮上下文召回\nstatus: draft\n---\n\n"
    tail = "\n## 关键条件\n- 槽：值\n\n## 必要事实\n- F1：事实内容\n"
    forms = [
        head + "## 对话\n- 第一轮\n- 第二轮" + tail,
        head + "## 对话\n### 1\n第一轮\n### 2\n第二轮" + tail,
        head + "## 对话记录\n用户：第一轮\n用户：第二轮" + tail,
        head + "## 对话\n```jsonl\n{\"role\":\"user\",\"content\":\"第一轮\"}\n{\"role\":\"user\",\"content\":\"第二轮\"}\n```" + tail,
    ]
    for form in forms:
        case = parse_ok(env, form, "mt-0001")
        assert list(case["turns"]) == ["第一轮", "第二轮"]


def test_assistant_refs_are_per_turn(env):
    text = KR_CASE.replace("id: kr-0001", "id: mt-0002").replace("dim: 知识召回", "dim: 多轮上下文召回") \
        .replace("## 问题\n太保的倒签保函需要在什么时候提供？",
                 "## 对话\n### 1\n客户问倒签保函什么时候提供\n助手：开船时。\n### 2\n那正签的呢？") \
        .replace("- F1：太保的倒签保函在开船时提供 【证据：R1】", "- F1：正签保函随单提供 【轮次：2】【外部】")
    case = parse_ok(env, text, "mt-0002")
    assert case["assistant_refs"] == ["开船时。", None]


# ---------------------------------------------------------------- check 报错清单

def test_error_unknown_section(env):
    e = parse_err(env, KR_CASE + "\n## 不存在的段落\n- x\n", "kr-0001")
    assert any("未知标题" in x for x in e.errors)


def test_error_missing_input(env):
    e = parse_err(env, KR_CASE.replace("## 问题\n太保的倒签保函需要在什么时候提供？\n\n", ""), "kr-0001")
    assert any("问题/对话/对话记录" in x for x in e.errors)


def test_error_prefix_dim_mismatch(env):
    e = parse_err(env, KR_CASE.replace("dim: 知识召回", "dim: 多轮上下文召回"), "kr-0001")
    assert any("不对应" in x for x in e.errors)


def test_error_id_filename_mismatch(env):
    (env / "wrong-name.md").write_text(KR_CASE, encoding="utf-8")
    cases, failures, _ = md2case.load_all([str(env)])
    assert any("与文件名" in x for f in failures for x in f.errors)


def test_error_forbidden_with_keywords(env):
    bad = EC_CASE.replace("- X1：2025 年 10 月出运的这单，保单号后三位应填 500 【轮次：2】【证据：R1】",
                          "- X1：2025 年 10 月出运的这单应填 500 【轮次：2】【关键词：500】")
    e = parse_err(env, bad, "ec-0102")
    assert any("关键词" in x for x in e.errors)


def test_error_evidence_and_external(env):
    bad = EC_CASE.replace("【轮次：2】【外部】\n\n## 禁止事实", "【轮次：2】【证据：R1】【外部】\n\n## 禁止事实")
    e = parse_err(env, bad, "ec-0102")
    assert any("不能同时出现" in x for x in e.errors)


def test_error_group_single_member_and_turn_mismatch(env):
    single = KR_CASE.replace("- F1：太保的倒签保函在开船时提供 【证据：R1】",
                             "- F1：太保的倒签保函在开船时提供 【组：G1】【证据：R1】")
    e = parse_err(env, single, "kr-0001")
    assert any("只有一个成员" in x for x in e.errors)
    both = KR_CASE.replace("- F1：太保的倒签保函在开船时提供 【证据：R1】",
                           "- F1：太保的倒签保函在开船时提供 【组：G1】【证据：R1】\n\n## 期望事实\n- E1：另一说法 【组：G1】【轮次：1】【外部】")
    e = parse_err(env, both, "kr-0001")
    assert any("轮次必须相同" in x for x in e.errors)


def test_error_whole_variant_turn_count(env):
    bad = EC_CASE.replace("- V1：11.15 前出运的是 400 吧？这票尾号三位到底填啥？ 【轮次：2】【改写：口语】",
                          "- V1：【整段】【改写：语序】\n  - 只有一轮")
    e = parse_err(env, bad, "ec-0102")
    assert any("轮数不一致" in x for x in e.errors)


def test_error_turn_out_of_range(env):
    bad = EC_CASE.replace("【轮次：2】【外部】", "【轮次：5】【外部】")
    e = parse_err(env, bad, "ec-0102")
    assert any("超过对话轮数" in x for x in e.errors)


def test_error_tag_rules(env):
    e = parse_err(env, KR_CASE.replace("tags: [来源/人工构造]", "tags: [来源/不存在的来源]"), "kr-0001")
    assert any("不在词表" in x for x in e.errors)
    e = parse_err(env, KR_CASE.replace("tags: [来源/人工构造]", "tags: [业务/收汇]"), "kr-0001")
    assert any("业务类标签由注入点继承" in x for x in e.errors)


def test_error_bad_rewrite(env):
    bad = EC_CASE.replace("【轮次：2】【改写：口语】", "【轮次：2】【改写：倒装】")
    e = parse_err(env, bad, "ec-0102")
    assert any("改写类型" in x for x in e.errors)


def test_error_reviewed_gate(env):
    no_ref = KR_CASE.replace("## 参考答案\n太保的倒签保函在开船时提供。\n", "").replace("status: admitted", "status: reviewed")
    e = parse_err(env, no_ref, "kr-0001")
    assert any("参考答案" in x for x in e.errors)
    unmarked = KR_CASE.replace(" 【证据：R1】", "")
    e = parse_err(env, unmarked, "kr-0001")
    assert any("未标注来源" in x for x in e.errors)


def test_error_non_ec_with_injection(env):
    e = parse_err(env, KR_CASE + "\n## 注入\n- J1：@收汇保单尾号/反转\n", "kr-0001")
    assert any("只有错误知识纠正" in x for x in e.errors)


def test_error_point_multi_option_and_missing(env):
    bad = EC_CASE.replace("- J1：@收汇保单尾号/反转",
                          "- J1：@收汇保单尾号/反转\n- J2：@收汇保单尾号/删除")
    e = parse_err(env, bad, "ec-0102")
    assert any("多个选项" in x for x in e.errors)
    bad = EC_CASE.replace("@收汇保单尾号/反转", "@不存在/反转")
    (env / "ec-0102.md").write_text(bad, encoding="utf-8")
    cases, failures, _ = md2case.load_all([str(env)])
    assert any("注入点「不存在」" in x for f in failures for x in f.errors)


def test_error_precondition_refs_missing_j(env):
    e = parse_err(env, EC_CASE.replace("【注入：J1】", "【注入：J9】"), "ec-0102")
    assert any("J9 不存在" in x for x in e.errors)


def test_error_inline_evidence_requires_path(env):
    e = parse_err(env, KR_CASE.replace("trade-documents:trade-documents.md / 保险", "trade-documents / 保险"), "kr-0001")
    assert any("显式写路径" in x for x in e.errors)


# ---------------------------------------------------------------- 提示

def test_warnings_leak_and_relative_date(env):
    leaky = EC_CASE.replace("- V1：11.15 前出运的是 400 吧？这票尾号三位到底填啥？ 【轮次：2】【改写：口语】",
                            "- V1：保单号后三位这票填啥？ 【轮次：2】【改写：极简】")
    case = parse_ok(env, leaky, "ec-0102")
    assert any("缺少本轮条件的关键词" in w for w in case["_warnings"])
    rel = KR_CASE.replace("太保的倒签保函需要在什么时候提供？", "去年出运的单子倒签保函什么时候提供？")
    case = parse_ok(env, rel, "kr-0001")
    assert any("相对日期" in w for w in case["_warnings"])


def test_title_roundtrip_and_missing_warn(env):
    """title：进 case_to_md 的 META_KEYS（回写不丢）、编译行带 title、缺省只提示不报错。"""
    head = "---\nid: kr-0001\ndim: 知识召回\ntitle: 融资单据清单\nstatus: draft\n---\n"
    body = "\n## 问题\n保单号后三位填多少？\n\n## 关键条件\n- 槽：值\n\n## 必要事实\n- F1：事实内容 【外部】\n"
    case = parse_ok(env, head + body, "kr-0001")
    assert case["title"] == "融资单据清单"
    md = md2case.case_to_md(case, md2case.load_mapping())
    assert "title: 融资单据清单" in md
    # frontmatter 必须在首行（通用解析器只认这种写法）；标题只在 frontmatter 存一份，
    # 页面渲染时生成 H1，md 不再重复标题。
    assert md.startswith("---\nid: kr-0001\n")
    assert "# 融资单据清单" not in md
    assert "\n---\n\n## 问题" in md
    assert "answerable: true" not in md                    # 缺省值不落盘
    back = md2case.md_to_case(md, "kr-0001.md")            # 回写产物再解析，title 不丢
    assert back["title"] == "融资单据清单"
    assert md2case.case_to_md(back) == md                   # 回写幂等
    rows = md2case.compile_case(case, {}, "vX")
    assert rows[0]["title"] == "融资单据清单" and rows[0]["source"] is None
    no = parse_ok(env, "---\nid: kr-0002\ndim: 知识召回\nstatus: draft\n---\n" + body, "kr-0002")
    assert no.get("title") is None
    assert any("缺 title" in w for w in no["_warnings"])


def test_title_h1_parse_fallback_and_mismatch(env):
    """正文 H1 与 frontmatter title 对账：只有 H1 时兜底成 title；两者不一致以 frontmatter
    为准并提示；第二个 H1 直接报错（正文只应有一个标题头）。"""
    head = "---\nid: kr-0001\ndim: 知识召回\nstatus: draft\n---\n"
    body = "\n## 问题\n保单号后三位填多少？\n\n## 关键条件\n- 槽：值\n\n## 必要事实\n- F1：事实内容 【外部】\n"
    only_h1 = parse_ok(env, head + "\n# 只有正文标题\n" + body, "kr-0001")
    assert only_h1["title"] == "只有正文标题"                 # frontmatter 缺 title 时由 H1 兜底
    assert not any("缺 title" in w for w in only_h1["_warnings"])
    mismatch = parse_ok(env, head.replace(
        "status: draft", "status: draft\ntitle: 配置标题") + "\n# 正文标题\n" + body, "kr-0001")
    assert mismatch["title"] == "配置标题"                    # frontmatter 为准
    assert any("不一致" in w for w in mismatch["_warnings"])
    with pytest.raises(md2case.CaseError) as ei:
        md2case.md_to_case(head + "\n# 标题一\n" + body + "\n# 标题二\n", "kr-0001.md")
    assert "只应有一个" in str(ei.value)


# ---------------------------------------------------------------- --docs

def _docs_env(env):
    root = env / "skills"
    root.mkdir()
    (root / "trade-company.md").write_text(
        "---\nname: trade-company\ndescription: 公司介绍\n---\n\n正文：之前出运业务保单号后三位为 400，之后为 500。\n",
        encoding="utf-8")
    return root


def test_docs_key_frontmatter_multihit(env):
    root = _docs_env(env)
    cases = [md2case.md_to_case(EC_CASE, "ec-0102.md")]
    points = md2case.load_knowledge([md2case.KNOWLEDGE_DIR])
    errs: list[str] = []
    md2case.check_docs(cases, points, [root], errs)
    assert errs == []
    (root / "trade-company.md").write_text(
        "---\nname: trade-company\ndescription: 公司介绍\n---\n\n别的\n", encoding="utf-8")
    errs = []
    md2case.check_docs(cases, points, [root], errs)
    assert any("找不到" in x for x in errs)
    (root / "trade-company.md").write_text(
        "---\nname: trade-company\ndescription: 之前出运业务保单号后三位为 400，之后为 500 的规则\n---\n\n正文。\n",
        encoding="utf-8")
    errs = []
    md2case.check_docs(cases, points, [root], errs)
    assert any("frontmatter" in x for x in errs)
    (root / "trade-company.md").write_text(
        "---\nname: trade-company\ndescription: 公司介绍\n---\n\n"
        "之前出运业务保单号后三位为 400，之后为 500。再次：之前出运业务保单号后三位为 400，之后为 500。\n",
        encoding="utf-8")
    errs = []
    md2case.check_docs(cases, points, [root], errs)
    assert any("命中 2 次" in x and x.startswith("提示") for x in errs)


# ---------------------------------------------------------------- build / diff / rev 联动

def test_build_and_diff_and_rev_linkage(env):
    (env / "cases").mkdir()
    (env / "cases" / "ec-0102.md").write_text(EC_CASE, encoding="utf-8")
    (env / "cases" / "kr-0001.md").write_text(KR_CASE, encoding="utf-8")
    v1 = env / "dist" / "v1.jsonl"
    assert md2case.main(["build", str(env / "cases"), "-o", str(v1), "--version", "v1",
                         "--include", "admitted,draft"]) == 0
    lines = [json.loads(l) for l in v1.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["snapshot"]["version"] == "v1"          # 首行快照头（P2，doc/15 §3.9）
    assert (env / "dist" / "v1.snapshot.json").exists()     # 测法 sidecar
    rows = lines[1:]
    assert {r["uid"] for r in rows} == {"ec-0102#0", "ec-0102#1", "kr-0001#0"}

    # v2：kr 退役、ec 备注（内容修订）、rev 1→2、新增 kr-0002
    (env / "cases" / "kr-0001.md").write_text(
        KR_CASE.replace("status: admitted", "status: retired"), encoding="utf-8")
    (env / "cases" / "ec-0102.md").write_text(
        EC_CASE.replace("动态注入示例。", "动态注入示例（修订）。"), encoding="utf-8")
    (env / "cases" / "kr-0002.md").write_text(
        KR_CASE.replace("id: kr-0001", "id: kr-0002").replace("太保", "平安"), encoding="utf-8")
    (md2case.KNOWLEDGE_DIR / "trade-company.yaml").write_text(
        KNOWLEDGE.replace('rev: 1}', 'rev: 2}', 1).replace(
            '反转: {value: "之前出运业务保单号后三位为 500，之后为 400", rev: 1}',
            '反转: {value: "之前出运业务保单号后三位为 900，之后为 400", rev: 2}'),
        encoding="utf-8")
    v2 = env / "dist" / "v2.jsonl"
    assert md2case.main(["build", str(env / "cases"), "-o", str(v2), "--version", "v2",
                         "--include", "admitted,draft", "--prev", str(v1)]) == 0
    diff = (env / "dist" / "v2.diff.md").read_text(encoding="utf-8")

    def section(name):
        part = diff.split(f"## {name}")[1]
        return part.split("##")[0]
    assert "kr-0002#0" in section("新增")
    assert "kr-0001#0" in section("退役")
    assert "ec-0102" in section("因降级移出")          # rev 变化触发降级，v2 中被移出
    src = (env / "cases" / "ec-0102.md").read_text(encoding="utf-8")
    assert "status: structured" in src and "rev 1→2" in src
    v2_rows = [r for r in (json.loads(l) for l in v2.read_text(encoding="utf-8").splitlines()
                           if l.strip()) if r.get("uid")]
    assert "ec-0102#0" not in {r["uid"] for r in v2_rows}


def test_build_default_includes_structured(env):
    """doc/17 §4：一期无准入流程，CLI build 缺省收录 structured/reviewed/admitted，
    与页面保存管线同一口径（不再出现「CLI 构建 0 行、页面保存有行」）。"""
    (env / "cases").mkdir()
    (env / "cases" / "kr-0001.md").write_text(
        KR_CASE.replace("status: admitted", "status: structured"), encoding="utf-8")
    out = env / "dist" / "v1.jsonl"
    assert md2case.main(["build", str(env / "cases"), "-o", str(out), "--version", "v1"]) == 0
    rows = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert {r["uid"] for r in rows if r.get("uid")} == {"kr-0001#0"}
    assert rows[0]["snapshot"]["include"] == md2case.DEFAULT_INCLUDE


def test_build_auto_version_skips_db_versions(env, monkeypatch):
    """build --db（doc/17 决定 16）：自动取号把评测库已登记的版本算进去——dist 不进 git，
    换机器或清空 dist 后同一天不再重号，避免 build 出注定导入失败的版本。
    省略 -o 时按版本号落到 dist/evalset-<版本>.jsonl。"""
    import datetime
    import sqlite3
    monkeypatch.chdir(env)
    (env / "cases").mkdir()
    (env / "cases" / "kr-0001.md").write_text(KR_CASE, encoding="utf-8")
    today = datetime.date.today().strftime("%Y%m%d")
    db = env / "eval.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute("CREATE TABLE snapshots(evalset_version TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO snapshots VALUES(?)", (f"v{today}-1",))
    assert md2case.main(["build", str(env / "cases"), "--db", str(db)]) == 0
    out = env / "dist" / f"evalset-v{today}-2.jsonl"
    header = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
    assert header["snapshot"]["version"] == f"v{today}-2"
    # 库不存在 → 中止（不静默回落到只扫本地 dist）
    assert md2case.main(["build", str(env / "cases"),
                         "--db", str(env / "nope.db")]) == 1


def test_build_refuses_overwrite(env):
    (env / "cases").mkdir()
    (env / "cases" / "kr-0001.md").write_text(KR_CASE, encoding="utf-8")
    v1 = env / "dist" / "v1.jsonl"
    md2case.main(["build", str(env / "cases"), "-o", str(v1), "--version", "v1", "--include", "admitted,draft"])
    assert md2case.main(["build", str(env / "cases"), "-o", str(v1), "--version", "v1",
                         "--include", "admitted,draft"]) == 1


# ---------------------------------------------------------------- render 往返

def test_render_roundtrip(env):
    case = parse_ok(env, EC_CASE, "ec-0102")
    text = md2case.case_to_md(case)
    again = md2case.md_to_case(text, "<render>")
    points = md2case.load_knowledge([md2case.KNOWLEDGE_DIR])
    rows_a = md2case.compile_case(case, points, "v")
    rows_b = md2case.compile_case(again, points, "v")
    strip = lambda rs: [{k: v for k, v in r.items()} for r in rs]
    assert strip(rows_a) == strip(rows_b)


# ---------------------------------------------------------------- 权重（doc/10 §5.1）

def test_scoring_block_compiles_and_round_trips(env):
    """doc/17 决定 12/§3：【权重】是名义百分比（0–100，≤4 位小数），编译时归一进
    顶层 scoring 块；事实/路径行不内联（标准与指纹不含计分配置）。"""
    text = (EC_CASE.replace("保单号后三位应填 400 【轮次：2】【外部】", "保单号后三位应填 400 【轮次：2】【外部】【权重：70】")
                   .replace("后三位应填 500 【轮次：2】【证据：R1】", "后三位应填 500 【轮次：2】【证据：R1】【计分】【权重：30】"))
    case = parse_ok(env, text, "ec-0102")
    rows = md2case.compile_case(case, md2case.load_knowledge([md2case.KNOWLEDGE_DIR]), "vX")
    sc = rows[0]["scoring"]
    assert sc["unassigned"] is False
    assert sc["items"]["F1"] == {"use": "score", "weight": 70.0}   # 必要缺省计分
    assert sc["items"]["X1"] == {"use": "score", "weight": 30.0}   # 禁止缺省参考，显式计分
    assert sc["items"]["P1"] == {"use": "ref"} and sc["items"]["path#1"] == {"use": "ref"}
    assert all("weight" not in f and "use" not in f for f in rows[0]["facts"])
    assert "weight" not in rows[0]["path"][0] and "use" not in rows[0]["path"][0]
    again = md2case.md_to_case(md2case.case_to_md(case), "<render>")      # 计分/权重往返不丢
    assert again["facts"][0]["weight"] == 70 and again["facts"][1]["use"] == "score"


def test_scoring_normalize_and_unassigned(env):
    """归一（§3.1）：名义合计 ≠100 按比例；没写 = 0；全 0 → unassigned（评测跳过）。"""
    text = EC_CASE.replace("保单号后三位应填 400 【轮次：2】【外部】",
                           "保单号后三位应填 400 【轮次：2】【外部】【权重：60.5】【权重：x】")
    # 上面替换故意非法 → 改用合法样例：F1=60.5 与 X1【计分】=29.5，合计 90 → 归一到 100
    text = (EC_CASE.replace("保单号后三位应填 400 【轮次：2】【外部】", "保单号后三位应填 400 【轮次：2】【外部】【权重：60.5】")
                   .replace("后三位应填 500 【轮次：2】【证据：R1】", "后三位应填 500 【轮次：2】【证据：R1】【计分】【权重：29.5】"))
    case = parse_ok(env, text, "ec-0102")
    rows = md2case.compile_case(case, md2case.load_knowledge([md2case.KNOWLEDGE_DIR]), "vX")
    sc = rows[0]["scoring"]
    assert abs(sc["items"]["F1"]["weight"] - 67.2222) < 1e-6      # 60.5/90×100
    assert abs(sc["items"]["X1"]["weight"] - 32.7778) < 1e-6
    # 未分配：一条权重都不写 → 计分条目全 0 → unassigned（admitted 才给提示，决定 14）
    case2 = parse_ok(env, EC_CASE, "ec-0102")
    sc2 = md2case.compile_case(case2, md2case.load_knowledge([md2case.KNOWLEDGE_DIR]), "vX")[0]["scoring"]
    assert sc2["unassigned"] is True and sc2["items"]["F1"]["weight"] == 0.0
    assert any("未分配" in w for w in case2["_warnings"])          # admitted 用例给提示
    # structured 也会进入快照（DEFAULT_INCLUDE），未分配提示同样给出
    case3 = parse_ok(env, EC_CASE.replace("status: admitted", "status: structured"), "ec-0103")
    assert any("未分配" in w for w in case3["_warnings"])
    # draft 不进快照，不给未分配提示
    case4 = parse_ok(env, EC_CASE.replace("status: admitted", "status: draft"), "ec-0104")
    assert not any("未分配" in w for w in case4["_warnings"])


def test_warn_score_items_all_paths(env):
    """计分条目全是路径：非 forced 才能判路径，forced 会话没有计分单位会记 error；check 提示。"""
    text = (EC_CASE.replace("保单号后三位应填 400 【轮次：2】【外部】",
                            "保单号后三位应填 400 【轮次：2】【外部】【参考】")
                   .replace("必须：加载 skill trade-company 【轮次：1】",
                            "必须：加载 skill trade-company 【轮次：1】【计分】【权重：100】"))
    case = parse_ok(env, text, "ec-0105")
    assert set(md2case.score_units_of(case)) == {"path#1"}
    assert any("计分条目全部是路径" in w and "forced" in w for w in case["_warnings"])
    assert not any("计分条目全部是路径" in w for w in parse_ok(env, EC_CASE, "ec-0106")["_warnings"])
    # 权重 0 的计分条目不构成计分单位：路径 100 + F1 计分但 0% 仍然只有路径计分
    text0 = (EC_CASE.replace("保单号后三位应填 400 【轮次：2】【外部】",
                             "保单号后三位应填 400 【轮次：2】【外部】【计分】【权重：0】")
                    .replace("必须：加载 skill trade-company 【轮次：1】",
                             "必须：加载 skill trade-company 【轮次：1】【计分】【权重：100】"))
    case0 = parse_ok(env, text0, "ec-0107")
    assert md2case.score_units_of(case0) == {"F1": 0.0, "path#1": 100.0}
    assert any("计分条目全部是路径" in w for w in case0["_warnings"])


def test_error_bad_weight(env):
    for bad_w in ("-1", "两", "1.23456", "101"):
        bad = KR_CASE.replace("- F1：太保的倒签保函在开船时提供 【证据：R1】",
                              f"- F1：太保的倒签保函在开船时提供 【证据：R1】【权重：{bad_w}】")
        e = parse_err(env, bad, "kr-0001")
        assert any("权重" in x for x in e.errors), bad_w


def test_error_ref_weight_without_score(env):
    """【参考】条目写【权重】报错（§2.3）：显式参考与节点缺省参考都算。"""
    bad = KR_CASE.replace("- F1：太保的倒签保函在开船时提供 【证据：R1】",
                          "- F1：太保的倒签保函在开船时提供 【证据：R1】【参考】【权重：30】")
    assert any("不能写【权重】" in x for x in parse_err(env, bad, "kr-0001").errors)
    bad2 = EC_CASE.replace("后三位应填 500 【轮次：2】【证据：R1】",
                           "后三位应填 500 【轮次：2】【证据：R1】【权重：30】")   # 禁止缺省参考
    assert any("不能写【权重】" in x for x in parse_err(env, bad2, "ec-0102").errors)
    bad3 = KR_CASE.replace("## 证据", "## 前提\n- P1：答复为可以电放 【权重：10】\n\n## 证据")  # 前提缺省参考
    assert any("不能写【权重】" in x for x in parse_err(env, bad3, "kr-0001").errors)


# ---------------------------------------------------------------- 性质与部分判据（doc/16 §3）

KR_WITH_NATURE = KR_CASE.replace(
    "- F1：太保的倒签保函在开船时提供 【证据：R1】",
    "- F1：太保的倒签保函在开船时提供 【证据：R1】\n\n## 期望事实\n"
    "- E1：建议注明保函费用 【外部】【性质：主观】\n"
    "- E2：建议注明有效期 【外部】【性质：主观】【部分判据：只写\u201c短期\u201d、未给具体期限算部分】")


def test_nature_parses_compiles_and_round_trips(env):
    case = parse_ok(env, KR_WITH_NATURE, "kr-0001")
    by_id = {f["id"]: f for f in case["facts"]}
    assert "nature" not in by_id["F1"]                          # 客观缺省不落键
    assert by_id["E1"]["nature"] == "subjective" and "partial_rule" not in by_id["E1"]
    assert by_id["E2"]["nature"] == "subjective"
    assert by_id["E2"]["partial_rule"].startswith("只写")
    rows = md2case.compile_case(case, md2case.load_knowledge([md2case.KNOWLEDGE_DIR]), "vX")
    by_id = {f["id"]: f for f in rows[0]["facts"]}
    assert "nature" not in by_id["F1"] and "partial_rule" not in by_id["F1"]   # 客观编译行与旧版逐字节一致
    assert by_id["E1"]["nature"] == "subjective"
    assert by_id["E2"]["partial_rule"].startswith("只写")
    again = md2case.md_to_case(md2case.case_to_md(case), "<rt>")
    assert again["facts"] == case["facts"]                      # 回写往返


def test_nature_objective_explicit_is_equivalent(env):
    """【性质：客观】与不写等价：解析后不落键、编译行相同。"""
    plain = parse_ok(env, KR_CASE, "kr-0001")
    explicit = parse_ok(env, KR_CASE.replace("【证据：R1】", "【证据：R1】【性质：客观】"), "kr-0001")
    assert explicit["facts"] == plain["facts"]


def test_error_bad_nature_value(env):
    for bad_v in ("中立", ""):
        attr = "【性质】" if bad_v == "" else f"【性质：{bad_v}】"
        e = parse_err(env, KR_CASE.replace("【证据：R1】", f"【证据：R1】{attr}"), "kr-0001")
        assert any("性质只能是" in x for x in e.errors), bad_v


def test_error_partial_rule_placement(env):
    # 客观条目（未标性质）上写部分判据 → 错误
    e = parse_err(env, KR_CASE.replace("【证据：R1】", "【证据：R1】【部分判据：写一半算部分】"), "kr-0001")
    assert any("【性质：主观】" in x for x in e.errors)
    # 禁止条目上写部分判据 → 错误（结论刻度没有「部分」）
    bad = EC_CASE.replace("后三位应填 500 【轮次：2】【证据：R1】",
                          "后三位应填 500 【轮次：2】【证据：R1】【性质：主观】【部分判据：复述即算部分】")
    e = parse_err(env, bad, "ec-0102")
    assert any("禁止事实不写【部分判据】" in x for x in e.errors)
    # 标了主观但部分判据为空 → 错误
    e = parse_err(env, KR_CASE.replace("【证据：R1】", "【证据：R1】【性质：主观】【部分判据】"), "kr-0001")
    assert any("部分判据为空" in x for x in e.errors)


def test_error_bracket_in_fact_body(env):
    """长【部分判据】踩线（值内含【】）时定界被破坏 → 正文含「【」报错。"""
    bad = KR_CASE.replace("- F1：太保的倒签保函在开船时提供 【证据：R1】",
                          "- F1：太保的倒签保函在开船时提供，见【规范】第 3 条 【证据：R1】")
    e = parse_err(env, bad, "kr-0001")
    assert any("正文中不得出现" in x for x in e.errors)


def test_error_group_nature_mismatch(env):
    both = KR_CASE.replace("- F1：太保的倒签保函在开船时提供 【证据：R1】",
                           "- F1：太保的倒签保函在开船时提供 【组：G1】【证据：R1】\n\n"
                           "## 期望事实\n- E1：另一说法 【组：G1】【轮次：1】【外部】【性质：主观】")
    e = parse_err(env, both, "kr-0001")
    assert any("性质必须一致" in x for x in e.errors)
    same = KR_CASE.replace("- F1：太保的倒签保函在开船时提供 【证据：R1】",
                           "- F1：太保的倒签保函在开船时提供 【组：G1】【轮次：1】【证据：R1】【性质：主观】\n\n"
                           "## 期望事实\n- E1：另一说法 【组：G1】【轮次：1】【外部】【性质：主观】\n"
                           "- E2：再一说法 【组：G1】【轮次：1】【外部】【性质：主观】")
    parse_ok(env, same, "kr-0001")                              # 组内全主观且轮次一致 → 通过


def test_warn_required_subjective_and_subjective_with_keywords(env):
    text = KR_CASE.replace(
        "- F1：太保的倒签保函在开船时提供 【证据：R1】",
        "- F1：倒签保函建议在开船时提供 【证据：R1】【性质：主观】【关键词：开船】\n\n"
        "## 期望事实\n- E1：注明费用 【外部】【性质：主观】【关键词：费用】")
    case = parse_ok(env, text, "kr-0001")
    assert any("必要事实 F1 标为主观" in w for w in case["_warnings"])
    assert any("写了关键词" in w for w in case["_warnings"])
