#!/usr/bin/env python3
"""评测用例 md → 结构化数据 转换与编译（v2，规范 doc/CASE-STRUCTURE.md）

用法（在 evalset/ 下执行）：
  python md2case.py check   <路径>... [--docs <skill根,...>]    校验（--docs 追加与 skill 原文的一致性校验）
  python md2case.py convert <用例.md> [-o out.yaml]            单个用例转 YAML
  python md2case.py build   <cases目录> -o dist/evalset-vX.jsonl --version vX
                                   [--prev dist/evalset-vW.jsonl] [--include admitted]
                                   编译 + 与上一版自动 diff + 注入 rev 联动降级
  python md2case.py render  <结构化用例> -o <目录> [--force]    生成器产出 → md

原则：md 是唯一数据源；编译后执行器不再解析；knowledge 只定义"改什么"；
随行字段（条件/前提/事实/路径/证据/参考答案/备注）原样带出。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
MAPPING_PATH = HERE / "mapping.yaml"
KNOWLEDGE_DIR = HERE / "knowledge"
REV_STATE_PATH = HERE / ".knowledge-state.json"
SCHEMA = 2

H2 = re.compile(r"^##\s+(.+?)\s*$")
BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
COLONS = (":", "：")
QUOTE_SEP = " ｜ 原文："


class CaseError(Exception):
    """一个文件里的全部错误（+可选提示），一次性报出。"""

    def __init__(self, path: str, errors: list[str], warnings: list[str] | None = None):
        self.path, self.errors, self.warnings = path, errors, warnings or []
        lines = [f"{path}: {e}" for e in errors] + [f"{path}: 提示：{w}" for w in self.warnings]
        super().__init__("\n".join(lines))


# ---------------------------------------------------------------- 基础工具

def load_mapping(path: Path = MAPPING_PATH) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def split_once(s: str, seps):
    """按第一个出现的分隔符切两段；全角半角都认。"""
    hits = [(s.find(c), c) for c in seps if s.find(c) >= 0]
    if not hits:
        return None
    i, c = min(hits)
    return s[:i].strip(), s[i + len(c):].strip()


def bullets(lines, errors):
    """段落内只允许列表项；返回 [(行号, 缩进, 内容)]。"""
    items = []
    for no, text in lines:
        if not text.strip():
            continue
        m = BULLET.match(text)
        if not m:
            errors.append(f"第 {no} 行：这里应是以「- 」开头的列表项")
            continue
        if not m.group(2).strip():
            errors.append(f"第 {no} 行：列表项内容为空")
            continue
        items.append((no, len(m.group(1)), m.group(2).strip()))
    return items


# 属性：行尾「【名：值】」或无值旗标「【名】」；可多个、顺序不限
TRAILING_ATTR = re.compile(r"\s*[【\[]\s*([^\s:：【】\[\]]+)\s*(?:[:：]\s*([^【】\[\]]*?))?\s*[】\]]\s*$")
KEYWORD_SEPS = re.compile(r"[/／、,，]")


def split_attrs(text: str):
    """剥离行尾【属性】，返回 (正文, {名: 值或None})。正文中不得出现【。"""
    attrs: dict[str, str | None] = {}
    while True:
        m = TRAILING_ATTR.search(text)
        if not m:
            body = text.strip()
            return body, attrs
        attrs.setdefault(m.group(1), m.group(2).strip() if m.group(2) else None)
        text = text[:m.start()]


def parse_turn_attr(attrs, no, errors) -> int | None:
    v = attrs.get("轮次")
    if v is None:
        return None
    if not re.fullmatch(r"[1-9][0-9]*", v):
        errors.append(f"第 {no} 行：轮次应为正整数，如「【轮次：2】」")
        return None
    return int(v)


# ---------------------------------------------------------------- knowledge

def load_knowledge(dirs: list[Path]) -> dict[str, dict]:
    """knowledge/*.yaml → {注入点id: {file,key,tags,options{选项:{value,rev}}}}；id 冲突报错。"""
    points: dict[str, dict] = {}
    for d in dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.rglob("*.yaml")) + sorted(d.rglob("*.yml")):
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or []
            if not isinstance(data, list):
                continue
            for p in data:
                pid = p.get("id")
                if not pid:
                    continue
                if pid in points:
                    raise SystemExit(f"{f}: 注入点 id「{pid}」重复")
                opts = {}
                for name, o in (p.get("options") or {}).items():
                    opts[name] = {"value": o.get("value", ""), "rev": int(o.get("rev", 1))}
                points[pid] = {"file": p.get("file", ""), "key": p.get("key", ""),
                               "tags": p.get("tags") or [], "options": opts, "src": str(f)}
    return points


# ---------------------------------------------------------------- 段落解析器

USER, ASSISTANT = "user", "assistant"
ROLE_PREFIX = re.compile(r"^\s*(用户|助手|user|assistant)\s*[:：]\s?(.*)$", re.IGNORECASE)
ROLE_NAMES = {"用户": USER, "助手": ASSISTANT, "user": USER, "assistant": ASSISTANT}
H3 = re.compile(r"^###\s+(.+?)\s*$")
FENCE = re.compile(r"^\s*```\s*([A-Za-z]*)\s*$")


WEIGHT_RE = re.compile(r"[0-9]+(?:\.[0-9])?")


def parse_weight_attr(attrs, no, errors):
    """【权重：N】：N ≥ 0、至多一位小数；0 = 只记录不计分（打分口径见 doc/10 §4.2）。"""
    raw = attrs.get("权重")
    if raw is None:
        return None
    if not WEIGHT_RE.fullmatch(raw.strip()):
        errors.append(f"第 {no} 行：权重应为非负数、至多一位小数，如「【权重：2】」「【权重：0.5】」")
        return None
    w = float(raw)
    return int(w) if w.is_integer() else w


def p_text(lines, rule, errors):
    value = "\n".join(t for _, t in lines).strip()
    if not value:
        errors.append("段落内容为空")
    return value


def p_question(lines, rule, errors):
    return to_input([(USER, p_text(lines, rule, errors), 0)], errors)


def p_dialog(lines, rule, errors):
    content = [(no, t) for no, t in lines if t.strip()]
    if not content:
        return None
    if FENCE.match(content[0][1]):
        return to_input(parse_fenced(lines, errors), errors)
    if any(H3.match(t) for _, t in content):
        return to_input(parse_rounds(lines, errors), errors)
    return to_input([(USER, t, no) for no, _, t in bullets(lines, errors)], errors)


def p_transcript(lines, rule, errors):
    messages = []
    for no, t in lines:
        m = ROLE_PREFIX.match(t)
        if m:
            messages.append((ROLE_NAMES[m.group(1).lower()], m.group(2), no))
        elif messages:
            messages[-1] = (messages[-1][0], messages[-1][1] + "\n" + t, messages[-1][2])
        elif t.strip():
            errors.append(f"第 {no} 行：对话记录的每条消息应以「用户：」或「助手：」开头")
    return to_input([(r, c.strip(), no) for r, c, no in messages], errors)


def parse_rounds(lines, errors):
    messages, role, buf, start = [], None, [], 0

    def flush():
        text = "\n".join(buf).strip()
        if role and text:
            messages.append((role, text, start))
        elif role == USER:
            errors.append(f"第 {start} 行：该轮没有用户消息")

    for no, t in lines:
        if H3.match(t):
            flush()
            role, buf, start = USER, [], no
            continue
        if role is None:
            if t.strip():
                errors.append(f"第 {no} 行：分轮写法中，内容必须写在某个「### 轮次标题」下面")
            continue
        m = ROLE_PREFIX.match(t)
        if m and ROLE_NAMES[m.group(1).lower()] == ASSISTANT and role == USER:
            flush()
            role, buf, start = ASSISTANT, [m.group(2)], no
            continue
        if m and ROLE_NAMES[m.group(1).lower()] == USER and role == USER and not "".join(buf).strip():
            buf.append(m.group(2))
            continue
        buf.append(t)
    flush()
    return messages


def parse_fenced(lines, errors):
    body, opened, closed, lang, start = [], False, False, "", 0
    for no, t in lines:
        m = FENCE.match(t)
        if m and not opened:
            opened, lang, start = True, m.group(1).lower(), no
            continue
        if m and opened:
            closed = True
            continue
        if closed and t.strip():
            errors.append(f"第 {no} 行：代码块之后不能再有内容")
        elif opened and not closed:
            body.append((no, t))
    if not closed:
        errors.append(f"第 {start} 行：代码块没有结束的 ```")
        return []
    try:
        if lang == "json":
            items = json.loads("\n".join(t for _, t in body))
            numbered = [(start + 1, item) for item in items] if isinstance(items, list) else None
            if numbered is None:
                errors.append(f"第 {start} 行：json 代码块应为消息数组")
                return []
        else:
            numbered = [(no, json.loads(t)) for no, t in body if t.strip()]
    except json.JSONDecodeError as e:
        errors.append(f"第 {start} 行起：代码块不是合法的 {lang or 'jsonl'}：{e.msg}")
        return []
    messages = []
    for no, item in numbered:
        role = str(item.get("role", "")).lower() if isinstance(item, dict) else ""
        content = item.get("content") if isinstance(item, dict) else None
        if role not in (USER, ASSISTANT) or not isinstance(content, str) or not content.strip():
            errors.append(f"第 {no} 行：每条消息应为 {{\"role\": \"user\"|\"assistant\", \"content\": \"非空文本\"}}")
            continue
        messages.append((role, content.strip(), no))
    return messages


def to_input(messages, errors):
    """消息序列 → {turns, assistant_refs}；连续助手消息合并为该轮参考回复。"""
    if not messages:
        return None
    merged = []
    for role, content, no in messages:
        if merged and role == ASSISTANT and merged[-1][0] == ASSISTANT:
            merged[-1][1] += "\n" + content
        else:
            merged.append([role, content, no])
    if merged[0][0] != USER:
        errors.append(f"第 {merged[0][2]} 行：对话必须以用户消息开始")
        return None
    turns, refs = [], []
    for role, content, _ in merged:
        if role == USER:
            turns.append(content)
            refs.append(None)
        else:
            refs[-1] = content
    return {"turns": turns, "assistant_refs": refs}


def p_conditions(lines, rule, errors):
    out = []
    for no, _, t in bullets(lines, errors):
        t, attrs = split_attrs(t)
        if "【" in t:
            errors.append(f"第 {no} 行：正文中不得出现「【」")
        item: dict = {}
        kv = split_once(t, COLONS)
        if not kv or not kv[0] or not kv[1]:
            errors.append(f"第 {no} 行：应写成「槽位：值」，例如「业务类型：信用证」")
            continue
        item["slot"], item["value"] = kv
        if (obj := attrs.get("对象")) is not None:
            item["object"] = obj
        if (turn := parse_turn_attr(attrs, no, errors)) is not None:
            item["turn"] = turn
        if attrs.get("关键词") is not None:
            kws = [k.strip() for k in KEYWORD_SEPS.split(attrs["关键词"]) if k.strip()]
            if not kws:
                errors.append(f"第 {no} 行：关键词为空，应写成「【关键词：甲/乙】」")
                continue
            item["keywords"] = kws
        out.append(item)
    return out


def p_injections(lines, rule, errors):
    """「J1：@注入点/选项」"""
    out, seen = [], set()
    for no, _, t in bullets(lines, errors):
        kv = split_once(t, COLONS)
        if not kv or not re.fullmatch(rf"{rule['prefix']}[0-9]+", kv[0] or "") or not kv[1]:
            errors.append(f"第 {no} 行：应写成「{rule['prefix']}1：@注入点/选项」")
            continue
        if kv[0] in seen:
            errors.append(f"第 {no} 行：编号 {kv[0]} 重复")
            continue
        seen.add(kv[0])
        ref = re.fullmatch(r"@(.+?)[/／](.+)", kv[1])
        if not ref:
            errors.append(f"第 {no} 行：{kv[0]} 应写成「@注入点/选项」")
            continue
        out.append({"id": kv[0], "point": ref.group(1).strip(), "option": ref.group(2).strip()})
    return out


def p_preconditions(lines, rule, errors):
    out, seen = [], set()
    for no, _, t in bullets(lines, errors):
        t, attrs = split_attrs(t)
        kv = split_once(t, COLONS)
        if not kv or not re.fullmatch(rf"{rule['prefix']}[0-9]+", kv[0] or "") or not kv[1]:
            errors.append(f"第 {no} 行：应写成「{rule['prefix']}1：内容 【轮次：1】【注入：J1】」")
            continue
        if kv[0] in seen:
            errors.append(f"第 {no} 行：编号 {kv[0]} 重复")
            continue
        seen.add(kv[0])
        item: dict = {"id": kv[0], "text": kv[1]}
        if (turn := parse_turn_attr(attrs, no, errors)) is not None:
            item["turn"] = turn
        if attrs.get("注入") is not None:
            item["injections"] = [j.strip() for j in KEYWORD_SEPS.split(attrs["注入"]) if j.strip()]
        out.append(item)
    return out


def p_facts(lines, rule, errors):
    kind, prefix = rule["kind"], rule["prefix"]
    allowed = rule.get("attrs", [])
    out, seen = [], set()
    for no, _, t in bullets(lines, errors):
        t, attrs = split_attrs(t)
        bad = [k for k in attrs if k not in allowed]
        if bad:
            errors.append(f"第 {no} 行：不支持的属性「{'、'.join(bad)}」，可用：{'、'.join(allowed) or '无'}")
            continue
        kv = split_once(t, COLONS)
        if not kv or not re.fullmatch(rf"{prefix}[0-9]+", kv[0] or "") or not kv[1]:
            errors.append(f"第 {no} 行：应写成「{prefix}1：内容」")
            continue
        if kv[0] in seen:
            errors.append(f"第 {no} 行：编号 {kv[0]} 重复")
            continue
        seen.add(kv[0])
        item: dict = {"id": kv[0], "kind": kind, "text": kv[1]}
        if "关键词" in attrs:
            if kind == "forbidden":
                errors.append(f"第 {no} 行：{kv[0]} 禁止事实不写关键词——纠错时的复述会被误判为采信")
                continue
            kws = [k.strip() for k in KEYWORD_SEPS.split(attrs["关键词"] or "") if k.strip()]
            if not kws:
                errors.append(f"第 {no} 行：关键词为空")
                continue
            item["keywords"] = kws
        if (turn := parse_turn_attr(attrs, no, errors)) is not None:
            item["turn"] = turn
        if (grp := attrs.get("组")) is not None:
            item["group"] = grp
        if (w := parse_weight_attr(attrs, no, errors)) is not None:
            item["weight"] = w
        if attrs.get("证据") is not None and "外部" in attrs:
            errors.append(f"第 {no} 行：{kv[0]} 【证据】与【外部】不能同时出现")
            continue
        if attrs.get("证据") is not None:
            item["evidence"] = attrs["证据"]
        elif "外部" in attrs:
            item["evidence"] = "external"
        out.append(item)
    return out


def p_path(lines, rule, errors):
    out = []
    for no, _, t in bullets(lines, errors):
        t, attrs = split_attrs(t)
        kv = split_once(t, COLONS)
        if not kv or kv[0] not in ("必须", "禁止") or not kv[1]:
            errors.append(f"第 {no} 行：应写成「必须：加载 skill 名称」或「禁止：加载 skill 名称」")
            continue
        m = re.fullmatch(r"加载\s*skill\s+(\S+)", kv[1])
        if not m:
            errors.append(f"第 {no} 行：第一期动作只支持「加载 skill <名称>」")
            continue
        item = {"type": "must" if kv[0] == "必须" else "forbid",
                "action": {"kind": "load_skill", "skill": m.group(1)}}
        if (turn := parse_turn_attr(attrs, no, errors)) is not None:
            item["turn"] = turn
        if (w := parse_weight_attr(attrs, no, errors)) is not None:
            item["weight"] = w
        out.append(item)
    return out


def p_evidence(lines, rule, errors):
    """「R1：@注入点」或「R2：skill:file / 章节 ｜ 原文：…」（路径必须显式写出）"""
    out, seen = [], set()
    for no, _, t in bullets(lines, errors):
        kv = split_once(t, COLONS)
        if not kv or not re.fullmatch(rf"{rule['prefix']}[0-9]+", kv[0] or "") or not kv[1]:
            errors.append(f"第 {no} 行：应写成「{rule['prefix']}1：@注入点」或「{rule['prefix']}1：skill:file / 章节」")
            continue
        if kv[0] in seen:
            errors.append(f"第 {no} 行：编号 {kv[0]} 重复")
            continue
        seen.add(kv[0])
        rest = kv[1]
        if rest.startswith("@"):
            out.append({"id": kv[0], "point": rest[1:].strip(), "inline": None})
            continue
        inline: dict = {"quote": None}
        if QUOTE_SEP in rest:
            rest, quote = rest.split(QUOTE_SEP, 1)
            inline["quote"] = quote.strip()
        parts = split_once(rest.strip(), (" / ", " ／ "))
        if parts is None:
            parts = (rest.strip(), None)
        doc = parts[0]
        if ":" not in doc and "：" not in doc:
            errors.append(f"第 {no} 行：内联证据的文档必须显式写路径，如「trade-company:trade-company.md / 章节」")
            continue
        sk, fl = split_once(doc, COLONS)
        if not sk or not fl:
            errors.append(f"第 {no} 行：文档应写成「skill 名:相对路径」")
            continue
        inline["doc"] = {"skill": sk, "file": fl}
        inline["section"] = parts[1]
        out.append({"id": kv[0], "point": None, "inline": inline})
    return out


def p_variants(lines, rule, errors):
    """文本变体（【轮次：N】【改写：x】，无轮次=最后一轮）与【整段】变体（随后的嵌套列表）。"""
    mapping = load_mapping()
    out, seen = [], set()
    i = 0
    items = bullets(lines, errors)
    while i < len(items):
        no, indent, t = items[i]
        t, attrs = split_attrs(t)
        bad = [k for k in attrs if k not in rule.get("attrs", [])]
        if bad:
            errors.append(f"第 {no} 行：不支持的属性「{'、'.join(bad)}」")
            i += 1
            continue
        kv = split_once(t, COLONS)
        if not kv or not re.fullmatch(rf"{rule['prefix']}[0-9]+", kv[0] or "") \
                or (not kv[1] and "整段" not in attrs):
            errors.append(f"第 {no} 行：应写成「{rule['prefix']}1：改写后的用户消息 【改写：口语】」")
            i += 1
            continue
        if kv[0] in seen:
            errors.append(f"第 {no} 行：编号 {kv[0]} 重复")
            i += 1
            continue
        seen.add(kv[0])
        item: dict = {"id": kv[0]}
        if "整段" in attrs:
            item["scope"] = "whole"
            turns = []
            j = i + 1
            while j < len(items) and items[j][1] > indent:
                turns.append(items[j][2])
                j += 1
            if not turns:
                errors.append(f"第 {no} 行：【整段】变体应在下面缩进列出各轮用户消息")
            item["turns"] = turns
            if (rw := attrs.get("改写")) is not None:
                if rw not in mapping.get("rewrites", []):
                    errors.append(f"第 {no} 行：改写类型「{rw}」不在词表（{('、'.join(mapping.get('rewrites', [])))}）")
                else:
                    item["rewrite"] = rw
            out.append(item)
            i = j
            continue
        if not kv[1]:
            errors.append(f"第 {no} 行：变体应为替换后的用户消息，或加【整段】后缩进列出各轮")
            i += 1
            continue
        item["scope"] = "turn"
        item["text"] = kv[1]
        if (turn := parse_turn_attr(attrs, no, errors)) is not None:
            item["turn"] = turn
        if (rw := attrs.get("改写")) is not None:
            if rw not in mapping.get("rewrites", []):
                errors.append(f"第 {no} 行：改写类型「{rw}」不在词表（{('、'.join(mapping.get('rewrites', [])))}）")
            else:
                item["rewrite"] = rw
        out.append(item)
        i += 1
    return out


PARSERS = {
    "text": p_text,
    "question": p_question,
    "dialog": p_dialog,
    "transcript": p_transcript,
    "conditions": p_conditions,
    "injections": p_injections,
    "preconditions": p_preconditions,
    "facts": p_facts,
    "path": p_path,
    "evidence": p_evidence,
    "variants": p_variants,
}


# ---------------------------------------------------------------- md → case

def split_frontmatter(text: str, errors: list[str]):
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        errors.append("第 1 行：文件应以 --- 开头的元数据块（frontmatter）开始")
        return {}, text.splitlines(), 0
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            try:
                meta = yaml.safe_load("\n".join(lines[1:i])) or {}
            except yaml.YAMLError as e:
                errors.append(f"元数据块格式错误：{e}")
                meta = {}
            if not isinstance(meta, dict):
                errors.append("元数据块应为「键: 值」形式")
                meta = {}
            return meta, lines[i + 1:], i + 1
    errors.append("元数据块没有结束的 ---")
    return {}, lines, 0


def split_sections(body, offset, errors):
    sections, current = [], None
    for idx, text in enumerate(body):
        no = offset + idx + 1
        m = H2.match(text)
        if m:
            current = (m.group(1), no, [])
            sections.append(current)
        elif current is None:
            if text.strip():
                errors.append(f"第 {no} 行：内容必须写在某个「## 标题」下面")
        else:
            current[2].append((no, text))
    return sections


META_ENUMS = {"status": None, "history": ["live", "prefill"], "granularity": ["环节", "细节"]}  # status 词表来自 mapping


def md_to_case(text: str, path: str = "<input>", mapping: dict | None = None) -> dict:
    """解析 + 全部结构性校验；报错进 CaseError，提示进 CaseError.warnings。"""
    mapping = mapping or load_mapping()
    errors: list[str] = []
    warns: list[str] = []

    meta, body, offset = split_frontmatter(text, errors)
    case: dict = dict(meta)
    case.setdefault("answerable", True)

    group_owner: dict[str, str] = {}
    for title, no, lines in split_sections(body, offset, errors):
        rule = mapping["sections"].get(title)
        if rule is None:
            errors.append(f"第 {no} 行：未知标题「{title}」。可用标题：{'、'.join(mapping['sections'])}")
            continue
        field, group = rule["field"], rule.get("group", rule["field"])
        if not rule.get("append") and group in group_owner:   # append 段（F/E/X）累加，不互斥
            errors.append(f"第 {no} 行：「{title}」与「{group_owner[group]}」重复或冲突，只能写一个")
            continue
        group_owner[group] = title
        if not any(t.strip() for _, t in lines):
            errors.append(f"第 {no} 行：「{title}」段落内容为空")
            continue
        value = PARSERS[rule["type"]](lines, rule, errors)
        if rule.get("append"):                   # 同字段多段（F/E/X 三类事实）累加
            case.setdefault(field, []).extend(value or [])
        else:
            case[field] = value

    if "input" in case and case["input"]:
        case["turns"] = case["input"]["turns"]
        case["assistant_refs"] = case["input"]["assistant_refs"]
    case.pop("input", None)

    check_frontmatter(case, mapping, errors)
    check_required(case, mapping, errors)
    check_cross(case, mapping, errors, warns)
    check_warnings(case, warns)
    case["_warnings"] = warns          # 成功时提示挂在返回值上；失败时挂在 CaseError

    if errors:
        raise CaseError(path, errors, warns)
    return case


def check_frontmatter(case, mapping, errors):
    statuses = mapping.get("statuses", [])
    if case.get("status") not in statuses:
        errors.append(f"status 应为 {'/'.join(statuses)} 之一，当前为 {case.get('status')!r}")
    if case.get("history") not in (None, "live", "prefill"):
        errors.append("history 只能是 live 或 prefill")
    if case.get("granularity") not in (None, "环节", "细节"):
        errors.append("granularity 只能是 环节 或 细节")
    if not isinstance(case.get("answerable", True), bool):
        errors.append("answerable 应为布尔值")
    dim, cid = case.get("dim"), case.get("id")
    prefix_of = {v: k for k, v in mapping["dims"].items()}
    m = re.fullmatch(r"([a-z]{2})-([0-9]{4})", cid or "")
    if not m:
        errors.append(f"id 应形如 kr-0001（前缀+4 位数字），当前为 {cid!r}")
    elif mapping["dims"].get(dim) != m.group(1):
        want = prefix_of.get(m.group(1), "?")
        errors.append(f"id 前缀 {m.group(1)} 与维度 {dim!r} 不对应（该前缀对应维度「{want}」）")
    tags = case.get("tags") or []
    vocab = mapping.get("tags", {})
    if isinstance(tags, str):
        tags = [tags]
    for tag in tags:
        ns, _, val = tag.partition("/")
        if ns not in vocab or val not in vocab.get(ns, []):
            errors.append(f"标签「{tag}」不在词表中")
        if ns == "业务":
            errors.append(f"标签「{tag}」：业务类标签由注入点继承，用例不直接写")
    case["tags"] = tags


def check_required(case, mapping, errors):
    dim = case.get("dim")
    have = lambda f: case.get(f) not in (None, [], "")
    if not have("turns"):
        errors.append("缺少「问题/对话/对话记录」段落（三选一）")
    if not have("conditions"):
        errors.append("缺少「关键条件」段落")
    if not any(f.get("kind") == "required" for f in case.get("facts") or []):
        errors.append("缺少「必要事实」段落")
    if dim == "错误知识纠正" and not any(f.get("kind") == "forbidden" for f in case.get("facts") or []):
        errors.append("维度「错误知识纠正」必须写「禁止事实」段落")
    for field in mapping.get("required_by_dim", {}).get(dim, []):
        if not have(field):
            names = [t for t, r in mapping["sections"].items() if r["field"] == field]
            errors.append(f"维度「{dim}」必须写「{('、'.join(names)) or field}」段落")
    turns = case.get("turns") or []
    if len(turns) > 5:
        errors.append(f"多轮对话为 2 到 5 轮，当前 {len(turns)} 轮")
    if dim in mapping.get("multi_turn_dims", []) and turns and len(turns) < 2:
        errors.append(f"维度「{dim}」至少需要 2 轮，请用「对话」或「对话记录」逐轮列出")


def check_cross(case, mapping, errors, warns):
    dim = case.get("dim")
    turns = case.get("turns") or []
    n = len(turns)
    jids = {j["id"] for j in case.get("injections_ref") or []}
    rids = {r["id"] for r in case.get("evidence_ref") or []}
    facts = case.get("facts") or []

    # ec 维度特有规则
    if dim == "错误知识纠正":
        if not (case.get("injections_ref")):
            pass  # required_by_dim 已报
        if not any(f.get("kind") == "forbidden" for f in facts):
            pass  # required_by_dim 已报
    elif case.get("injections_ref"):
        errors.append("只有错误知识纠正维度可以写「注入」段落")

    # 注入点去重：同一注入点只能选一个选项
    by_point: dict[str, set[str]] = {}
    for j in case.get("injections_ref") or []:
        by_point.setdefault(j["point"], set()).add(j["option"])
    for point, opts in by_point.items():
        if len(opts) > 1:
            errors.append(f"注入点「{point}」同时选了多个选项：{'、'.join(sorted(opts))}")

    # 轮次越界（事实/前提/条件/变体）
    for label, items in (("必要/期望/禁止事实", facts), ("前提", case.get("preconditions") or []),
                         ("关键条件", case.get("conditions") or [])):
        for it in items:
            if isinstance(it, dict) and it.get("turn", 0) and n and it["turn"] > n:
                errors.append(f"{label} {it.get('id', it.get('slot', '?'))} 的轮次 {it['turn']} 超过对话轮数 {n}")

    # 前提引用的 J 必须存在
    for p in case.get("preconditions") or []:
        for j in p.get("injections") or []:
            if j not in jids:
                errors.append(f"前提 {p['id']} 引用的注入 {j} 不存在")

    # 事实引用的 R 必须存在；reviewed 门槛：参考答案 + 来源标注
    for f in facts:
        ev = f.get("evidence")
        if isinstance(ev, str) and re.fullmatch(r"R[0-9]+", ev) and ev not in rids:
            errors.append(f"事实 {f['id']} 引用的证据 {ev} 不存在")
    if case.get("status") in ("reviewed", "admitted"):
        if not (case.get("reference_answer") or "").strip():
            errors.append("进入 reviewed 前必须写「参考答案」")
        for f in facts:
            if not f.get("evidence"):
                errors.append(f"事实 {f['id']} 未标注来源（【证据：Rn】或【外部】），reviewed 前必须消除")

    # 组：单成员报错；组内轮次一致
    groups: dict[str, list[dict]] = {}
    for f in facts:
        if f.get("group"):
            groups.setdefault(f["group"], []).append(f)
    for g, members in groups.items():
        if len(members) == 1:
            errors.append(f"组 {g} 只有一个成员，要么再补同组事实，要么去掉【组】")
        weights_in_g = {m.get("weight", 1) for m in members}
        if len(weights_in_g) > 1:
            warns.append(f"组 {g} 成员权重不一致（{'、'.join(str(w) for w in sorted(weights_in_g))}），计分取最大值")
        turns_in_g = {m.get("turn") for m in members}
        if len(turns_in_g) > 1:
            errors.append(f"组 {g} 内事实的轮次必须相同，当前为 {'、'.join(str(t) for t in sorted(turns_in_g, key=str))}")

    # prefill：需要各轮参考回复；只考最后一轮
    if case.get("history") == "prefill":
        refs = case.get("assistant_refs") or [None] * n
        missing = [str(i + 1) for i in range(n - 1) if not refs[i]]
        if n < 2:
            errors.append("history: prefill 需要至少 2 轮对话")
        elif missing:
            errors.append(f"history: prefill 需要前面各轮都有助手回复，第 {'、'.join(missing)} 轮缺少「助手：」")

    # 变体：整段轮数一致；泄露提示
    conds = case.get("conditions") or []
    for v in case.get("variants") or []:
        if v["scope"] == "whole":
            if len(v.get("turns") or []) != n:
                errors.append(f"变体 {v['id']}【整段】与种子轮数不一致（{len(v.get('turns') or [])} vs {n}）")
            continue
        idx = v.get("turn") or n
        if idx > n:
            errors.append(f"变体 {v['id']} 的轮次 {idx} 超过对话轮数 {n}")
            continue
        text = v.get("text", "")
        cur = [k for c in conds if c.get("turn") == idx for k in c.get("keywords") or []]
        early = [k for c in conds if c.get("turn") and c["turn"] < idx for k in c.get("keywords") or []]
        if cur and not any(k in text for k in cur):
            warns.append(f"变体 {v['id']}：被替换的第 {idx} 轮缺少本轮条件的关键词（{'、'.join(cur)}），疑似丢失限定")
        leaked = [k for k in early if k in text]
        if leaked:
            warns.append(f"变体 {v['id']}：包含了更早轮次条件的关键词（{'、'.join(leaked)}），疑似泄露")


ANAPHORA = re.compile(r"前文|上述|第[0-9一二三四五]+轮|按.{0,8}作答")
RELATIVE_DATE = re.compile(r"去年|前年|今年|上年|上个月|上月|本月|下个月|下月|下周|下周[一二三四五六日天]?|上周|昨天|明天|后天|前天")


def check_warnings(case, warns):
    facts = case.get("facts") or []
    texts = [f["text"] for f in facts] + [p["text"] for p in case.get("preconditions") or []]
    for t in texts:
        if ANAPHORA.search(t):
            warns.append(f"事实/前提出现指代词（{ANAPHORA.search(t).group(0)}），必须自包含")
            break
    if case.get("dim") in ("知识召回", "错误知识纠正"):
        for f in facts:
            if f["kind"] == "required" and not f.get("evidence"):
                warns.append(f"必要事实 {f['id']} 未标注来源（【证据：Rn】或【外部】）")
                break
    for t in (case.get("turns") or []) + [v.get("text", "") for v in case.get("variants") or []]:
        if RELATIVE_DATE.search(t):
            warns.append(f"问题或变体出现相对日期（{RELATIVE_DATE.search(t).group(0)}），一律写绝对日期")
            break


# ---------------------------------------------------------------- --docs 校验

def frontmatter_of(text: str) -> dict:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?", text, re.S)
    if not m:
        return {}
    try:
        v = yaml.safe_load(m.group(1))
        return v if isinstance(v, dict) else {}
    except yaml.YAMLError:
        return {}


def check_docs(cases: list[dict], points: dict, roots: list[Path], errors_out: list[str]):
    """key 命中、frontmatter 误伤、内联证据原文；roots 为 skill 根目录列表。"""
    files: dict[str, str] = {}
    fm: dict[str, dict] = {}
    for root in roots:
        for f in sorted(root.rglob("*")):
            if f.is_file():
                rel = f.relative_to(root).as_posix()
                files.setdefault(rel, f.read_text(encoding="utf-8"))
    for j in [x for c in cases for x in c.get("injections_ref") or []]:
        p = points.get(j["point"])
        if not p or not p.get("file"):
            continue
        content = files.get(p["file"])
        if content is None:
            errors_out.append(f"注入点「{j['point']}」的 file {p['file']} 在 --docs 根中不存在")
            continue
        fmeta = frontmatter_of(content)
        head = f"{fmeta.get('name', '')} {fmeta.get('description', '')}"
        if p["key"] in head:
            errors_out.append(f"注入点「{j['point']}」的 key 出现在 skill frontmatter 中，注入会改变路由目录")
        cnt = content.count(p["key"])
        if cnt == 0:
            errors_out.append(f"注入点「{j['point']}」的 key 在 {p['file']} 中找不到（skill 改版？）")
        elif cnt != 1:
            errors_out.append(f"提示：注入点「{j['point']}」的 key 在 {p['file']} 中命中 {cnt} 次，替换会全部生效，请确认")
    for c in cases:
        for r in c.get("evidence_ref") or []:
            if not r.get("inline"):
                continue
            inline = r["inline"]
            skill, file = inline["doc"]["skill"], inline["doc"]["file"]
            content = files.get(file) or files.get(f"{skill}/{file}")
            if content is None:
                errors_out.append(f"{c['id']} 证据 {r['id']} 的文档 {file} 在 --docs 根中不存在")
            elif inline.get("quote") and inline["quote"] not in content:
                errors_out.append(f"{c['id']} 证据 {r['id']} 的原文在文档中找不到")


# ---------------------------------------------------------------- 编译

def compile_case(case: dict, points: dict, version: str) -> list[dict]:
    """种子 + 变体 → 编译行列表（schema 2，CASE-STRUCTURE §6）。"""
    cid, turns = case["id"], case.get("turns") or []
    refs = case.get("assistant_refs") or [None] * len(turns)

    inj_rows = []
    for j in case.get("injections_ref") or []:
        p = points.get(j["point"])
        opt = (p or {}).get("options", {}).get(j["option"]) if p else None
        if p is None or opt is None:
            raise CaseError(cid, [f"注入点「{j['point']}」或选项「{j['option']}」在 knowledge 中不存在"])
        inj_rows.append({"id": j["id"], "point": j["point"], "option": j["option"],
                         "rev": opt["rev"], "file": p["file"], "key": p["key"], "value": opt["value"]})

    ev_rows = []
    for r in case.get("evidence_ref") or []:
        if r.get("point"):
            p = points.get(r["point"])
            if p is None:
                raise CaseError(cid, [f"证据 {r['id']} 引用的注入点「{r['point']}」不存在"])
            skill = p["file"].split("/")[0].removesuffix(".md")
            ev_rows.append({"id": r["id"], "doc": {"skill": skill, "file": p["file"]},
                            "section": None, "quote": p["key"]})
        else:
            ev_rows.append({"id": r["id"], **r["inline"]})

    tags = [{"tag": t, "from": "case"} for t in case.get("tags") or []]
    seen_points = set()
    for j in case.get("injections_ref") or []:
        if j["point"] in seen_points:
            continue
        seen_points.add(j["point"])
        for t in (points.get(j["point"], {}).get("tags") or []):
            entry = {"tag": t, "from": f"point:{j['point']}"}
            if entry not in tags:
                tags.append(entry)

    def facts_of():
        out = []
        for f in case.get("facts") or []:
            row = {"id": f["id"], "kind": f["kind"], "text": f["text"]}
            row["keywords"] = f.get("keywords", [])
            row["turn"] = f.get("turn")
            row["group"] = f.get("group")
            row["evidence"] = f.get("evidence")
            out.append(row)          # v8 起权重不进标准（doc/15 §3.9）：行顶层 weights 带 md 原值
        return out

    def weights_of():
        w = {}
        for f in case.get("facts") or []:
            if "weight" in f and f.get("id"):
                w[f["id"]] = f["weight"]
        for i, p in enumerate(case.get("path") or [], 1):
            if "weight" in p:
                w[f"path#{i}"] = p["weight"]
        return w

    base = {
        "schema": SCHEMA, "id": cid, "dim": case["dim"], "status": case["status"],
        "answerable": case.get("answerable", True), "history": case.get("history", "live"),
        "topic": case.get("topic"), "granularity": case.get("granularity"),
        "pair": case.get("pair"), "tags": tags,
        "injections": inj_rows,
        "conditions": [{"slot": c["slot"], "value": c["value"], "object": c.get("object"),
                        "turn": c.get("turn"), "keywords": c.get("keywords", [])}
                       for c in case.get("conditions") or []],
        "preconditions": [{"id": p["id"], "turn": p.get("turn"), "text": p["text"],
                           "injections": p.get("injections", [])}
                          for p in case.get("preconditions") or []],
        "facts": facts_of(),
        "path": [{k: v for k, v in p.items() if k != "weight"} for p in (case.get("path") or [])],
        "weights": weights_of(),
        "evidence": ev_rows,
        "reference_answer": case.get("reference_answer"),
        "note": case.get("note"),
        "evalset_version": version,
    }

    if not base.get("weights"):
        base.pop("weights", None)                # 无显式权重的用例不落键（等权计分）
    base["case_id"] = cid
    rows = [dict(base, uid=f"{cid}#0", variant_index=0, variant=None,
                 turns=[{"role": USER, "content": t} for t in turns])]
    for k, v in enumerate(case.get("variants") or [], 1):
        if v["scope"] == "turn":
            idx = (v.get("turn") or len(turns)) - 1
            new_turns = list(turns)
            new_turns[idx] = v["text"]
            variant = {"id": v["id"], "scope": "turn", "turn": idx + 1, "rewrite": v.get("rewrite")}
        else:
            new_turns = v["turns"]
            variant = {"id": v["id"], "scope": "whole", "rewrite": v.get("rewrite")}
        rows.append(dict(base, uid=f"{cid}#{k}", variant_index=k, variant=variant,
                         turns=[{"role": USER, "content": t} for t in new_turns]))
    return rows


# ---------------------------------------------------------------- diff 与 rev 联动

def build_diff(prev_rows: dict[str, dict], new_rows: dict[str, dict],
               retired: set[str], demoted: dict[str, str], version: str, prev_version: str) -> str:
    lines = [f"# evalset {prev_version} → {version} 变更", ""]
    added = sorted(set(new_rows) - set(prev_rows))
    gone = sorted(set(prev_rows) - set(new_rows))
    changed = sorted(u for u in set(new_rows) & set(prev_rows)
                     if json.dumps(new_rows[u], sort_keys=True, ensure_ascii=False)
                     != json.dumps(prev_rows[u], sort_keys=True, ensure_ascii=False))
    rev_changed = sorted(u for u in set(new_rows) & set(prev_rows)
                         if [i["point"] + "/" + i["option"] + ":" + str(i["rev"]) for i in new_rows[u].get("injections", [])]
                         != [i["point"] + "/" + i["option"] + ":" + str(i["rev"]) for i in prev_rows[u].get("injections", [])])
    sections = [
        ("新增", [u for u in added]),
        ("退役", [u for u in gone if u.split("#")[0] in retired]),
        ("因降级移出", [f"{u}（{demoted.get(u.split('#')[0], '')}）" for u in gone
                        if u.split("#")[0] not in retired and u.split("#")[0] in demoted]),
        ("内容修订", [u for u in changed if u not in rev_changed]),
        ("注入 rev 变化", rev_changed),
    ]
    for name, items in sections:
        lines.append(f"## {name}（{len(items)}）")
        lines += [f"- {u}" for u in items]
        lines.append("")
    return "\n".join(lines)


def load_rev_state() -> dict:
    if REV_STATE_PATH.exists():
        return json.loads(REV_STATE_PATH.read_text(encoding="utf-8"))
    return {}


def save_rev_state(state: dict) -> None:
    REV_STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def demote_case_file(path: Path, notes: list[str]) -> None:
    """重写用例 md 的 frontmatter：status → structured，status_note 追加原因；正文不动。"""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    meta = yaml.safe_load("\n".join(lines[1:end])) or {}
    meta["status"] = "structured"
    note = meta.get("status_note") or ""
    add = "；".join(notes)
    meta["status_note"] = f"{note}；{add}" if note else add
    head = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
    path.write_text("---\n" + head + "\n---\n" + "\n".join(lines[end + 1:]), encoding="utf-8")


def apply_rev_linkage(cases: list[dict], paths: dict[str, Path], points: dict,
                      verbose: list[str]) -> set[str]:
    """key/rev 变化 → 引用用例自动降回 structured。返回被降级的 case_id 集合。"""
    state = load_rev_state()
    demoted: set[str] = set()
    for c in cases:
        cid = c["id"]
        refs = {f"{j['point']}/{j['option']}":
                points.get(j["point"], {}).get("options", {}).get(j["option"], {}).get("rev")
                for j in c.get("injections_ref") or []}
        old = state.get(cid)
        if old is not None:
            notes = []
            for k, rev in refs.items():
                if k in old and old[k] != rev:
                    notes.append(f"{k} rev {old[k]}→{rev}，需复核")
            for k in set(old) - set(refs):
                notes.append(f"{k} 已不再引用")
            if notes and c["status"] in ("reviewed", "admitted"):
                demote_case_file(paths[cid], notes)
                c["status"] = "structured"
                c.setdefault("status_note", "")
                c["status_note"] = (c["status_note"] + "；" if c["status_note"] else "") + "；".join(notes)
                demoted.add(cid)
                verbose.append(f"{cid}：{ '；'.join(notes) } → 自动降回 structured")
        if refs:
            state[cid] = refs
    save_rev_state(state)
    return demoted


# ---------------------------------------------------------------- 命令

def iter_md(paths):
    for p in map(Path, paths):
        if p.is_dir():
            yield from sorted(p.rglob("*.md"))
        else:
            yield p


def check_knowledge(case: dict, points: dict, errors: list[str]):
    """@注入点/选项在 knowledge 中必须存在（check 报错清单）。"""
    for j in case.get("injections_ref") or []:
        p = points.get(j["point"])
        if p is None:
            errors.append(f"注入 {j['id']} 引用的注入点「{j['point']}」在 knowledge 中不存在")
        elif j["option"] not in p.get("options", {}):
            errors.append(f"注入 {j['id']} 的选项「{j['option']}」在注入点「{j['point']}」中不存在"
                          f"（可用：{'、'.join(p['options']) or '无'}）")
    for r in case.get("evidence_ref") or []:
        if r.get("point") and r["point"] not in points:
            errors.append(f"证据 {r['id']} 引用的注入点「{r['point']}」在 knowledge 中不存在")


def load_all(paths, mapping=None):
    mapping = mapping or load_mapping()
    points = load_knowledge([KNOWLEDGE_DIR]) if KNOWLEDGE_DIR.is_dir() else {}
    cases, failures, ids, by_id = [], [], {}, {}
    for f in iter_md(paths):
        try:
            case = md_to_case(f.read_text(encoding="utf-8"), str(f), mapping)
        except CaseError as e:
            failures.append(e)
            continue
        kn_errors: list[str] = []
        check_knowledge(case, points, kn_errors)
        if kn_errors:
            failures.append(CaseError(str(f), kn_errors))
            continue
        if case["id"] in ids:
            failures.append(CaseError(str(f), [f"id {case['id']} 与 {ids[case['id']]} 重复"]))
            continue
        if f.stem != case["id"]:
            failures.append(CaseError(str(f), [f"id {case['id']} 与文件名 {f.stem} 不一致"]))
            continue
        ids[case["id"]] = str(f)
        by_id[case["id"]] = f
        cases.append(case)
    return cases, failures, by_id


def report(failures) -> None:
    for e in failures:
        print(e, file=sys.stderr)


def cmd_check(args) -> int:
    mapping = load_mapping()
    cases, failures, _ = load_all(args.paths, mapping)
    docs_errors: list[str] = []
    if args.docs:
        roots = [Path(p) for p in args.docs.split(",") if p.strip()]
        points = load_knowledge([KNOWLEDGE_DIR] if KNOWLEDGE_DIR.is_dir() else [])
        check_docs(cases, points, roots, docs_errors)
    report(failures)
    for e in docs_errors:
        print(e, file=sys.stderr)
    n_warn = sum(len(e.warnings) for e in failures)
    ok = not failures and not [e for e in docs_errors if not e.startswith("提示")]
    print(f"通过 {len(cases)} 个，失败 {len(failures)} 个；--docs 问题 {len(docs_errors)} 条；提示 {n_warn} 条")
    return 0 if ok else 1


def cmd_convert(args) -> int:
    try:
        case = md_to_case(Path(args.file).read_text(encoding="utf-8"), args.file)
    except CaseError as e:
        report([e])
        return 1
    points = load_knowledge([KNOWLEDGE_DIR]) if KNOWLEDGE_DIR.is_dir() else {}
    rows = compile_case(case, points, "convert")
    text = yaml.safe_dump(rows, allow_unicode=True, sort_keys=False)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


def load_method(method_path: str | None = None) -> dict:
    """测法（doc/15 §3.9）：--method 给定用之；缺省 evalset/method.yaml；缺失即报错。"""
    path = Path(method_path) if method_path else HERE / "method.yaml"
    if not path.exists():
        raise SystemExit(f"构建中止：需要测法文件（{path}，或 --method 指定；doc/15 §3.9）")
    method = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {"modes": method.get("modes") or {"default": "natural"},
            "run_rewrites": list(method.get("run_rewrites") or []),
            "anchors": list(method.get("anchors") or [])}


def method_sha(method: dict) -> str:
    import hashlib
    canon = json.dumps(method, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def validate_method(method: dict, mapping: dict, uids: set) -> list:
    """校验测法：模式取值、改写方式在词表、锚点 uid 在本快照（不在的剔除并提示）。"""
    warns = []
    for dim, mode in (method.get("modes") or {}).items():
        if dim != "default" and dim not in mapping.get("dims", {}):
            raise SystemExit(f"构建中止：测法 modes 引用了未知维度「{dim}」")
        if mode not in ("natural", "forced"):
            raise SystemExit(f"构建中止：测法模式只能是 natural/forced，{dim}={mode}")
    vocab = set(mapping.get("rewrites") or [])
    bad = [r for r in method.get("run_rewrites") or [] if r not in vocab]
    if bad:
        raise SystemExit(f"构建中止：测法 run_rewrites 不在改写词表：{bad}")
    missing = [a for a in method.get("anchors") or [] if a not in uids]
    if missing:
        warns.append(f"提示：测法锚点不在本快照（已剔除）：{'、'.join(missing)}")
        method["anchors"] = [a for a in method["anchors"] if a in uids]
    return warns


def cmd_build(args) -> int:
    mapping = load_mapping()
    cases, failures, by_id = load_all(args.paths, mapping)
    if failures:
        report(failures)
        print(f"构建中止：{len(failures)} 个文件有错误", file=sys.stderr)
        return 1
    verbose = []
    points = load_knowledge([KNOWLEDGE_DIR]) if KNOWLEDGE_DIR.is_dir() else {}
    demoted = apply_rev_linkage(cases, by_id, points, verbose) if args.linkage else set()
    include = set(args.include.split(","))
    picked = [c for c in cases if c["status"] in include]
    rows, row_map = [], {}
    for c in picked:
        for r in compile_case(c, points, args.version):
            rows.append(r)
            row_map[r["uid"]] = r
    method = load_method(args.method)
    verbose += validate_method(method, mapping, set(row_map))
    msha = method_sha(method)
    out = Path(args.output)
    if out.exists():
        print(f"构建中止：{out} 已存在（dist 按版本不可变）", file=sys.stderr)
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        # 快照头（doc/15 §3.9）：首行声明版本/测法/入选；正文行权重在顶层 weights
        fh.write(json.dumps({"snapshot": {"version": args.version, "method_sha": msha,
                                          "include": args.include}}, ensure_ascii=False) + "\n")
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    sidecar = out.with_name(out.stem + ".snapshot.json")
    sidecar.write_text(json.dumps({"version": args.version, "method_sha": msha, "method": method},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
    if args.prev:
        prev_map = {r["uid"]: r for r in
                    (json.loads(l) for l in Path(args.prev).read_text(encoding="utf-8").splitlines()
                     if l.strip()) if r.get("uid")}      # 跳过快照头行（P2 dist / 新旧皆可作 prev）
        retired = {c["id"] for c in cases if c["status"] == "retired"}
        demoted_notes = {c["id"]: (c.get("status_note") or "").split("；")[-1] for c in cases if c["id"] in demoted}
        diff = build_diff(prev_map, row_map, retired, demoted_notes,
                          args.version, Path(args.prev).stem)
        diff_path = out.with_name(out.stem + ".diff.md")
        diff_path.write_text(diff, encoding="utf-8")
        print(f"diff → {diff_path}")

    for v in verbose:
        print(v)
    print(f"版本 {args.version}：用例 {len(picked)}/{len(cases)} 个（状态 {'/'.join(sorted(include))}），"
          f"展开后 {len(rows)} 行 → {out}")
    return 0


# ---------------------------------------------------------------- case → md（render）

META_KEYS = ("id", "dim", "status", "status_note", "answerable", "history", "topic",
             "granularity", "pair", "tags", "source", "owner")


def case_to_md(case: dict, mapping: dict | None = None) -> str:
    mapping = mapping or load_mapping()
    meta = {k: case[k] for k in META_KEYS if case.get(k) not in (None, [])}
    out = ["---", yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip(), "---", ""]
    turns = case.get("turns") or []
    refs = case.get("assistant_refs") or [None] * len(turns)
    if len(turns) == 1:
        out += ["## 问题", turns[0], ""]
    elif not any(refs) and all("\n" not in t for t in turns):
        out += ["## 对话", *[f"- {t}" for t in turns], ""]
    else:
        out += ["## 对话记录"]
        for u, r in zip(turns, refs):
            out.append(f"用户：{u}")
            if r:
                out.append(f"助手：{r}")
        out.append("")
    emit = {
        "conditions": lambda v: [f"- {c['slot']}：{c['value']}"
                                 + attrs_str({"对象": c.get("object"), "轮次": c.get("turn"),
                                              "关键词": "/".join(c["keywords"]) if c.get("keywords") else None})
                                 for c in v],
        "injections_ref": lambda v: [f"- {j['id']}：@{j['point']}/{j['option']}" for j in v],
        "preconditions": lambda v: [f"- {p['id']}：{p['text']}"
                                    + attrs_str({"轮次": p.get("turn"),
                                                 "注入": "、".join(p.get("injections") or []) or None})
                                    for p in v],
        "facts": lambda v: [f"- {f['id']}：{f['text']}"
                            + attrs_str({"关键词": "/".join(f["keywords"]) if f.get("keywords") else None,
                                         "轮次": f.get("turn"), "组": f.get("group"),
                                         "证据": f.get("evidence") if f.get("evidence") not in ("external", None) else None,
                                         "外部": True if f.get("evidence") == "external" else None,
                                         "权重": f.get("weight")})
                            for f in v],
        "path": lambda v: [f"- {'必须' if p['type'] == 'must' else '禁止'}：加载 skill {p['action']['skill']}"
                           + attrs_str({"轮次": p.get("turn"), "权重": p.get("weight")}) for p in v],
        "evidence_ref": lambda v: [f"- {r['id']}：@{r['point']}" if r.get("point")
                                   else f"- {r['id']}：{r['inline']['doc']['skill']}:{r['inline']['doc']['file']}"
                                        + (f" / {r['inline']['section']}" if r['inline'].get("section") else "")
                                        + (f" {QUOTE_SEP}{r['inline']['quote']}" if r['inline'].get("quote") else "")
                                   for r in v],
        "variants": lambda v: [f"- {x['id']}：{x.get('text', '')}"
                               + attrs_str({"轮次": x.get("turn"), "改写": x.get("rewrite")})
                               if x["scope"] == "turn" else
                               f"- {x['id']}：{attrs_str({'整段': True, '改写': x.get('rewrite')})}"
                               + "\n" + "\n".join(f"  - {t}" for t in x.get("turns") or [])
                               for x in v],
    }
    for title, rule in mapping["sections"].items():
        if rule.get("group") == "input":
            continue
        field, value = rule["field"], case.get(rule["field"])
        if rule["type"] == "facts":                   # 三段各自只渲染本 kind 的事实
            value = [f for f in value or [] if f.get("kind") == rule["kind"]]
        if value in (None, [], ""):
            continue
        if rule["type"] == "text":
            out += [f"## {title}", str(value), ""]
        else:
            out += [f"## {title}", *emit[field](value), ""]
    return "\n".join(out)


def attrs_str(attrs: dict) -> str:
    parts = [f"【{k}：{v}】" if not isinstance(v, bool) else f"【{k}】"
             for k, v in attrs.items() if not (v is None or v == "" or v is False)]   # 权重 0 要保留
    return (" " + " ".join(parts)) if parts else ""


def load_structured(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    data = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
    return data if isinstance(data, list) else [data]


def cmd_render(args) -> int:
    mapping = load_mapping()
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    written, failures = 0, []
    for i, row in enumerate(load_structured(Path(args.input)), 1):
        case = dict(row)
        name = str(case.get("id") or f"case-{i:04d}")
        target = out_dir / f"{name}.md"
        if target.exists() and not args.force:
            failures.append(CaseError(str(target), ["文件已存在，未覆盖（需要覆盖请加 --force）"]))
            continue
        case.setdefault("status", "structured")
        text = case_to_md(case, mapping)
        target.write_text(text, encoding="utf-8")
        written += 1
        try:
            md_to_case(text, str(target), mapping)
        except CaseError as e:
            failures.append(e)
    report(failures)
    print(f"已渲染 {written} 个用例到 {out_dir}，其中 {len(failures)} 个需要修正")
    return 1 if failures else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="评测用例 md → 结构化数据（v2）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("check", help="校验 md 用例")
    p.add_argument("paths", nargs="+")
    p.add_argument("--docs", help="skill 根目录（逗号分隔），追加与原文一致性校验")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("convert", help="单个 md 转 YAML")
    p.add_argument("file")
    p.add_argument("-o", "--output")
    p.set_defaults(func=cmd_convert)

    p = sub.add_parser("build", help="编译为运行用 JSONL（首行快照头 + 同名 .snapshot.json 测法）")
    p.add_argument("paths", nargs="+")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--method", help="测法文件（缺省 evalset/method.yaml）")
    p.add_argument("--prev", help="上一版 dist，用于生成自动 diff")
    p.add_argument("--include", default="admitted")
    p.add_argument("--no-linkage", dest="linkage", action="store_false",
                   help="跳过注入 rev 联动降级（调试用）")
    p.set_defaults(func=cmd_build, linkage=True)

    p = sub.add_parser("render", help="结构化用例渲染为 md")
    p.add_argument("input")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_render)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
