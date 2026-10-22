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
    rows = [json.loads(l) for l in v1.read_text(encoding="utf-8").splitlines()]
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
    v2_rows = [json.loads(l) for l in v2.read_text(encoding="utf-8").splitlines()]
    assert "ec-0102#0" not in {r["uid"] for r in v2_rows}


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
