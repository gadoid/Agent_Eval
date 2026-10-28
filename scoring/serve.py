"""复核服务（doc/10 复核服务改造方案）：以对话为单位的百分制复核。

页面各答一个问题：用例管理（+ 新建用例）/ 配置管理 /（复核队列、复核页）/
结果对比 / 判定质量。
全部视图从评测库取数（store.review_conversations / conversation_view /
answer_view_rows + views 聚合）；服务不触发算子，缺判定时给出 rejudge 命令。
改判按「校验 → 同一事务追加 → 重算一次结论」完成（store.submit_reviews）。
新建用例走 md2case：表单 → 结构化 → md（唯一数据源）→ 校验 → 落盘 cases 目录。
依赖 FastAPI / Jinja2 / markdown / bleach，缺任一即启动报错（不再静默退化）。
"""
from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from pydantic import BaseModel

from . import views
from .chain import default_operators_path, load_operator_config
from .items import SCALES
from .policy import default_policy_path, load_policy, rules_dict_of_policy
from .rules import (Method, Rules, RulesError, canonical_sha, default_rules_path, load_rules,
                    rules_from_dict, rules_to_dict)
from .store import Store, StoreError

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"
TAB_LABELS = {"undecided": "结论待定", "unjudged": "有未判条目", "spot": "模型抽检",
              "mismatch": "人工≠机器", "failed": "会话失败"}
AXIS_LABELS = {"subject": "被测配置（subject）", "profile": "被测配置（subject）",
               "model": "被测模型（model）", "effort": "努力程度（effort）", "agent": "agent 版本",
               "mode": "干预模式（mode）", "skills_base": "知识库（skills_base）",
               "evalset_version": "快照（评测集版本）",
               "trial": "采样次数（trial）", "run": "run"}
FIELD_LABELS = {"model": "模型（model）", "effort": "努力程度（effort）",
                "reasoning_effort": "努力程度（effort）", "agent": "agent 版本",
                "skills_base": "知识库（skills_base）", "skills_mode": "技能模式（skills_mode）",
                "evalset_version": "评测集快照", "mode": "干预模式（mode）",
                "model_provider": "模型提供方（model_provider）", "skill_tool": "技能工具（skill_tool）",
                "dsh_profile": "dsh 配置（dsh_profile）", "eline_profile_sha": "eline 配置指纹",
                "routing": "路由（routing）", "subject": "被测配置（subject）"}
# 指纹字段表的展示序（subject_of_manifest 的键）；FP_ATTR_NAME 把指纹键映射到比较属性名
FP_FIELD_ORDER = ("model", "reasoning_effort", "agent", "skills_base", "skills_mode",
                  "model_provider", "skill_tool", "dsh_profile", "eline_profile_sha", "routing")
FP_ATTR_NAME = {"reasoning_effort": "effort"}


def _fmt_val(v) -> str:
    """指纹字段值的显示形式：agent 显示 name@version，嵌套结构串化，None → —。"""
    if v is None:
        return "—"
    if isinstance(v, dict):
        if set(v) <= {"name", "version"} and v.get("name"):
            return f"{v['name']}@{v.get('version') or '?'}"
        return json.dumps(v, ensure_ascii=False, sort_keys=True)
    return str(v)
KIND_LABELS = {"precond": "前提", "path": "路径", "forbidden": "禁止", "required": "必要",
               "expected": "期望", "refusal": "拒答"}


# ---------------------------------------------------------------- 渲染过滤器（净化后渲染）

try:
    import bleach
    import markdown as md_lib
except ImportError as e:  # doc/10 §8.7：依赖缺失时 serve 启动即报错
    raise RuntimeError(f"serve 需要 markdown 与 bleach：pip install -r scoring/requirements.txt（{e}）")

_ALLOWED_TAGS = ["p", "br", "strong", "em", "del", "code", "pre", "ul", "ol", "li",
                 "h1", "h2", "h3", "h4", "h5", "h6",
                 "table", "thead", "tbody", "tr", "th", "td", "blockquote", "hr", "a"]


def md_filter(text) -> Markup:
    """markdown 渲染，渲染前净化 HTML；图片降级为链接（本地服务不发外链请求）。"""
    if not text:
        return Markup("")
    raw = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"[\1](\2)", str(text))
    raw = md_lib.markdown(raw, extensions=["tables", "fenced_code", "nl2br", "sane_lists"])
    clean = bleach.clean(raw, tags=_ALLOWED_TAGS,
                         attributes={"a": ["href", "title", "rel", "target"]}, strip=True)
    clean = re.sub(r"<a (?![^>]*rel=)", '<a rel="noopener" target="_blank" ', clean)
    return Markup(clean)


_TONES = {"对": "pass", "满足": "pass", "成立": "pass", "未采信": "pass", "拒答": "pass",
          "错": "fail", "违反": "fail", "不成立": "fail", "采信": "fail", "未拒答": "fail",
          "部分": "partial", "未提及": "neutral", "不适用": "neutral"}
_STATUS_LABELS = {"pass": "通过", "fail": "失败", "invalid": "无效", "undecided": "未决", "error": "异常"}


def tone_filter(v):
    if v is None or v == "":
        return "neutral"
    return _TONES.get(v, "neutral")


def status_label(s):
    return _STATUS_LABELS.get(s or "", "待重判" if s is None else s)


# ---------------------------------------------------------------- 接口模型（§9）

class EntryIn(BaseModel):
    item_id: str
    value: str
    score: float | None = None
    reason: str = ""


class ReviewBatchIn(BaseModel):
    answer_id: str
    chain: str | None = None
    entries: list[EntryIn] = []
    total: dict | None = None
    reviewer: str = ""


class WeightsIn(BaseModel):
    case_id: str
    weights: dict[str, float]
    label: str = ""


class WeightsRestoreIn(BaseModel):
    case_id: str
    set_at: str
    label: str = ""                                   # 同秒多次保存时与 set_at 共同定位


class RunRenameIn(BaseModel):
    run_id: str
    title: str = ""


class ChainRenameIn(BaseModel):
    chain_sha: str
    name: str = ""


class SnapshotRenameIn(BaseModel):
    evalset_version: str
    name: str = ""


class RulesRenameIn(BaseModel):
    rules_sha: str
    name: str = ""


# ---------------- 新建用例（表单 → 结构化 → md；字段与 md2case 段落一一对应）

class CaseNewCondition(BaseModel):
    slot: str = ""
    value: str = ""
    turn: int | None = None
    keywords: list[str] = []


class CaseNewInjection(BaseModel):
    point: str = ""
    option: str = ""


class CaseNewPrecond(BaseModel):
    text: str = ""
    turn: int | None = None
    injections: list[str] = []


class CaseNewFact(BaseModel):
    kind: str = "required"          # required / expected / forbidden
    text: str = ""
    keywords: list[str] = []
    turn: int | None = None
    group: str = ""
    evidence: str = ""              # 空 / 外部 / R1…
    weight: float | None = None
    nature: str = ""                # 空 / 主观（客观与不写等价，doc/16 §3.3）
    partial_rule: str = ""          # 部分判据，仅必要/期望的主观条目（md 侧校验）


class CaseNewPath(BaseModel):
    type: str = "must"              # must / forbid
    skill: str = ""
    turn: int | None = None
    weight: float | None = None


class CaseNewEvidence(BaseModel):
    point: str = ""                 # 方式一：@注入点
    skill: str = ""                 # 方式二：内联 skill:file / 章节 ｜ 原文
    file: str = ""
    section: str = ""
    quote: str = ""


class CaseNewVariant(BaseModel):
    text: str = ""
    turn: int | None = None         # 空 = 最后一轮
    rewrite: str = ""


class CaseNewIn(BaseModel):
    id: str = ""
    dim: str
    topic: str = ""
    granularity: str = ""
    source: str = ""
    owner: str = ""
    tags: list[str] = []
    history: str = "live"
    turns: list[str] = []
    assistant_refs: list[str] = []
    conditions: list[CaseNewCondition] = []
    injections: list[CaseNewInjection] = []
    preconditions: list[CaseNewPrecond] = []
    facts: list[CaseNewFact] = []
    paths: list[CaseNewPath] = []
    evidences: list[CaseNewEvidence] = []
    variants: list[CaseNewVariant] = []
    reference_answer: str = ""
    note: str = ""
    directory: str = ""


# ---------------------------------------------------------------- md2case 桥接

_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES_DIR = _REPO_ROOT / "evalset" / "cases"

_MD2CASE = None


def _md2case():
    """按需加载 evalset/md2case.py（非包内模块，用文件位置加载并缓存）。"""
    global _MD2CASE
    if _MD2CASE is None:
        import importlib.util
        import sys
        p = _REPO_ROOT / "evalset" / "md2case.py"
        spec = importlib.util.spec_from_file_location("agent_eval_md2case", p)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        _MD2CASE = mod
    return _MD2CASE


def _iter_case_md(cases_dir: Path):
    if cases_dir.is_dir():
        yield from sorted(cases_dir.rglob("*.md"))


def _existing_case_ids(cases_dir: Path) -> dict[str, str]:
    """cases 目录下全部用例 id → 文件路径（新建时查重、推荐下一个 id）。"""
    m2c = _md2case()
    out: dict[str, str] = {}
    for f in _iter_case_md(cases_dir):
        meta = m2c.frontmatter_of(f.read_text(encoding="utf-8"))
        cid = str((meta or {}).get("id") or "")
        if cid:
            out.setdefault(cid, str(f))
    return out


def _suggest_ids(cases_dir: Path, dims: dict) -> dict[str, str]:
    """每个 id 前缀推荐下一个编号（目录内该前缀最大号 + 1）。"""
    ids = _existing_case_ids(cases_dir)
    out: dict[str, str] = {}
    for dim, prefix in dims.items():
        used = []
        for i in ids:
            m = re.fullmatch(rf"{prefix}-([0-9]{{4}})", i)
            if m:
                used.append(int(m.group(1)))
        out[prefix] = f"{prefix}-{(max(used) + 1) if used else 1:04d}"
    return out


def _case_dirs(cases_dir: Path) -> list[str]:
    if not cases_dir.is_dir():
        return []
    return sorted(d.name for d in cases_dir.iterdir() if d.is_dir())


def _default_dir(cases_dir: Path) -> str:
    """默认目标目录 = 最近有改动的子目录（新建用例通常归入当前批次）。"""
    if not cases_dir.is_dir():
        return ""
    dirs = [d for d in cases_dir.iterdir() if d.is_dir()]
    return max(dirs, key=lambda d: d.stat().st_mtime).name if dirs else ""


_FACT_PREFIX = {"required": "F", "expected": "E", "forbidden": "X"}


def _case_from_form(b: CaseNewIn) -> dict:
    """表单 → case_to_md 认识的结构化用例（编号 F/E/X/J/P/R/V 按填写顺序重排）。"""
    turns = [t.strip() for t in b.turns if t and t.strip()]
    refs = [r.strip() or None for r in (b.assistant_refs + [""] * len(turns))[:len(turns)]]

    def turn_of(v):
        return int(v) if v else None

    conditions = []
    for c in b.conditions:
        if c.slot.strip() and c.value.strip():
            item = {"slot": c.slot.strip(), "value": c.value.strip(),
                    "keywords": [k.strip() for k in c.keywords if k.strip()]}
            if c.turn:
                item["turn"] = turn_of(c.turn)
            conditions.append(item)

    facts, counters = [], {"required": 0, "expected": 0, "forbidden": 0}
    for f in b.facts:
        if not f.text.strip():
            continue
        kind = f.kind if f.kind in _FACT_PREFIX else "required"
        counters[kind] += 1
        item = {"id": f"{_FACT_PREFIX[kind]}{counters[kind]}", "kind": kind, "text": f.text.strip(),
                "keywords": [k.strip() for k in f.keywords if k.strip()]}
        if f.turn:
            item["turn"] = turn_of(f.turn)
        if f.group.strip():
            item["group"] = f.group.strip()
        if f.evidence.strip():
            item["evidence"] = "external" if f.evidence.strip() == "外部" else f.evidence.strip()
        if f.weight is not None:
            item["weight"] = f.weight
        if f.nature.strip() == "主观":
            item["nature"] = "subjective"
        if f.partial_rule.strip():
            item["partial_rule"] = f.partial_rule.strip()   # 写错位置由 md_to_case 校验报错
        facts.append(item)

    injections = [{"id": f"J{i}", "point": j.point.strip(), "option": j.option.strip()}
                  for i, j in enumerate((x for x in b.injections
                                         if x.point.strip() and x.option.strip()), 1)]
    preconditions = []
    for p in b.preconditions:
        if not p.text.strip():
            continue
        item = {"id": f"P{len(preconditions) + 1}", "text": p.text.strip()}
        if p.turn:
            item["turn"] = turn_of(p.turn)
        inj = [x.strip() for x in p.injections if x.strip()]
        if inj:
            item["injections"] = inj
        preconditions.append(item)

    paths = []
    for p in b.paths:
        if not p.skill.strip():
            continue
        item = {"type": "forbid" if p.type == "forbid" else "must",
                "action": {"kind": "load_skill", "skill": p.skill.strip()}}
        if p.turn:
            item["turn"] = turn_of(p.turn)
        if p.weight is not None:
            item["weight"] = p.weight
        paths.append(item)

    evidences = []
    for r in b.evidences:
        if r.point.strip():
            evidences.append({"id": f"R{len(evidences) + 1}", "point": r.point.strip(), "inline": None})
        elif r.skill.strip() and r.file.strip():
            evidences.append({"id": f"R{len(evidences) + 1}", "point": None,
                              "inline": {"doc": {"skill": r.skill.strip(), "file": r.file.strip()},
                                         "section": r.section.strip() or None,
                                         "quote": r.quote.strip() or None}})

    variants = []
    for v in b.variants:
        if not v.text.strip():
            continue
        item = {"id": f"V{len(variants) + 1}", "scope": "turn", "text": v.text.strip()}
        if v.turn:
            item["turn"] = turn_of(v.turn)
        if v.rewrite.strip():
            item["rewrite"] = v.rewrite.strip()
        variants.append(item)

    return {
        "id": b.id.strip(), "dim": b.dim.strip(), "status": "structured", "answerable": True,
        "history": b.history if b.history in ("live", "prefill") else "live",
        "topic": b.topic.strip() or None, "granularity": b.granularity.strip() or None,
        "source": b.source.strip() or None, "owner": b.owner.strip() or None,
        "tags": [t for t in b.tags if t.strip()],
        "turns": turns, "assistant_refs": refs,
        "conditions": conditions, "facts": facts, "path": paths,
        "injections_ref": injections, "preconditions": preconditions,
        "evidence_ref": evidences, "variants": variants,
        "reference_answer": b.reference_answer.strip() or None,
        "note": b.note.strip() or None,
    }


def _build_case_md(b: CaseNewIn) -> tuple[str, list[str], list[str]]:
    """表单 → md 全文 + 校验错误/提示（一次给全，含 knowledge 引用检查）。"""
    m2c = _md2case()
    mapping = m2c.load_mapping()
    case = _case_from_form(b)
    md = m2c.case_to_md(case, mapping)
    errors: list[str] = []
    warnings: list[str] = []
    try:
        parsed = m2c.md_to_case(md, f"{case.get('id') or '?'}.md", mapping)
        warnings = list(parsed.get("_warnings") or [])
    except m2c.CaseError as e:
        errors, warnings = list(e.errors), list(e.warnings)
    points = m2c.load_knowledge([m2c.KNOWLEDGE_DIR]) if m2c.KNOWLEDGE_DIR.is_dir() else {}
    m2c.check_knowledge(case, points, errors)
    return md, errors, warnings


def _build_hint(rel_path: str) -> list[str]:
    """保存成功后的下一步命令（check → build 新版本 → 执行）。"""
    dist = _REPO_ROOT / "evalset" / "dist"
    prevs = sorted(dist.glob("evalset-*.jsonl")) if dist.is_dir() else []
    version = f"v{_dt.date.today().strftime('%Y%m%d')}-1"
    while any(p.stem.endswith(version) for p in prevs):
        n = int(version.rsplit("-", 1)[1]) + 1
        version = f"{version.rsplit('-', 1)[0]}-{n}"
    build = (f"python evalset/md2case.py build evalset/cases"
             f" -o evalset/dist/evalset-{version}.jsonl --version {version}")
    if prevs:
        build += f" --prev {prevs[-1].as_posix()}"
    return [f"python evalset/md2case.py check {rel_path}", build,
            "runner 执行新版本后导入：python -m scoring score <run目录>"]


def _ev_dict(v) -> dict:
    return {"status": v.status, "score": v.score, "score_min": v.score_min, "score_max": v.score_max,
            "auto_score": v.auto_score, "score_source": v.score_source,
            "vetoes": v.vetoes, "reason": v.invalid_reason or v.error or "", "parts": v.parts}


# ---------------------------------------------------------------- 应用

def create_app(db: str | Path, rules_path: str | Path | None = None,
               operators_path: str | Path | None = None,
               cases_dir: str | Path | None = None) -> FastAPI:
    app = FastAPI(title="Agent_Eval 复核服务", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    store = Store(db)
    cases_root = Path(cases_dir) if cases_dir else DEFAULT_CASES_DIR
    # 计分规则（doc/15 §3.7）：rules.yaml 为主；--policy 为弃用别名（旧 policy 转换加载）。
    # rules 表是唯一事实来源：启动时把加载的规则登记进库，页面按 rules_sha 选择。
    if rules_path:
        try:                                   # rules.yaml 直接装载
            rules, rules_sha = load_rules(rules_path)
        except RulesError:                     # 旧 policy 文件（无 schema:1）→ 转换装载
            from .policy import load_policy as _lp
            _pol, _ = _lp(rules_path)
            _d = rules_dict_of_policy(_pol)
            rules, rules_sha = rules_from_dict(_d), canonical_sha(_d)
    else:
        rules, rules_sha = load_rules(default_rules_path())
    store.register_rules(rules_sha, rules_to_dict(rules))
    cfg = load_operator_config(operators_path or default_operators_path())
    default_chain = cfg.chain_sha
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["md"] = md_filter
    templates.env.filters["vcls"] = tone_filter
    templates.env.filters["status_label"] = status_label
    templates.env.filters["kind_label"] = lambda k: KIND_LABELS.get(k, k)
    templates.env.filters["f1"] = lambda x: "" if x is None else f"{x:.1f}".rstrip("0").rstrip(".")
    templates.env.globals["pass_mark"] = rules.score.pass_mark
    templates.env.globals["compare_delta"] = 10.0     # 展示参数（S7）：对比页可改，记浏览器本地
    _nav_cache: dict = {}

    def chain_of(request: Request) -> str:
        c = request.query_params.get("chain")
        return c if c and any(ch["chain_sha"] == c for ch in store.chains()) else default_chain

    def snapshot_of(request: Request) -> str | None:
        """请求所用快照（doc/15 §1.1）：URL ?snapshot= 优先，否则当前快照。"""
        s = request.query_params.get("snapshot")
        if s and store.snapshot(s):
            return s
        return None                        # None = 当前（answer_view_rows 内解析）

    def rules_state(request: Request) -> tuple[Rules, str]:
        """请求所用计分规则：URL ?rules= 选库内版本，否则启动加载的当前规则。"""
        rs = request.query_params.get("rules")
        if rs:
            for row in store.rules_rows():
                if row["rules_sha"] == rs:
                    return rules_from_dict(row["rules"]), row["rules_sha"]
        return rules, rules_sha

    def method_of(snapshot: str | None) -> Method:
        if snapshot:
            row = store.snapshot(snapshot)
            return Method.from_dict((row or {}).get("method"))
        cur = store.current_snapshot()
        if cur:
            row = store.snapshot(cur)
            return Method.from_dict((row or {}).get("method"))
        return Method()

    def rows(request: Request | str, with_records: bool = False,
             snapshot: str | None = None, r_sha: str | None = None,
             diagnostic: bool | None = None) -> list[dict]:
        """取数口（doc/15 §3.2 C5）：默认只取正式会话（诊断 run 不进分数）；
        diagnostic=True 时只取诊断会话（对比页诊断视图）。"""
        chain = chain_of(request) if isinstance(request, Request) else request
        rs = store.answer_view_rows(chain, r_sha or rules_sha, with_records=with_records,
                                    snapshot=snapshot)
        if diagnostic is None:
            diagnostic = isinstance(request, Request) and request.query_params.get("diagnostic") == "1"
        if diagnostic:
            return [r for r in rs if r.get("diagnostic")]
        return [r for r in rs if not r.get("diagnostic")]

    def nav_count(chain: str) -> int:
        """侧栏「复核」计数 = 结论待定的对话数（轻量 SQL，不做条目级扫描）。"""
        if _nav_cache.get("chain") != chain:
            n = store.conn.execute(
                "SELECT COUNT(*) FROM conclusions c JOIN answers a ON a.answer_id = c.answer_id"
                " JOIN questions q ON q.qkey = a.qkey"
                " WHERE q.current_standard_fp = c.standard_fp AND c.chain_sha = ? AND c.rules_sha = ?"
                " AND c.weight_fp = COALESCE(q.current_weight_fp, 'equal')"
                " AND c.status IN ('undecided', 'error')",
                (chain, rules_sha)).fetchone()[0]
            _nav_cache.update({"chain": chain, "n": n})
        return _nav_cache["n"]

    def needs_rescore() -> bool:
        """当前规则下尚无会话结论（改 rules 后未 rescore）：页面提示执行命令。"""
        return store.conn.execute("SELECT 1 FROM conclusions WHERE rules_sha=? LIMIT 1",
                                  (rules_sha,)).fetchone() is None

    def stale_runs() -> list[str]:
        """技能基底与导出清单不一致的 run（快照 skills_base_stale）。"""
        out = []
        for r in store.runs():
            try:
                manifest = json.loads(r.get("snapshot_json") or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            snap = manifest.get("snapshot") if isinstance(manifest, dict) else None
            if isinstance(snap, dict) and snap.get("skills_base_stale"):
                out.append(r["run_id"])
        return out

    def render(request: Request, name: str, **ctx):
        chain = chain_of(request)
        snap = snapshot_of(request) or store.current_snapshot()
        return templates.TemplateResponse(request=request, name=name, context={
            "db_path": str(store.path), "chain_sha": chain, "chain_short": chain[:8],
            "chains": store.chains(), "nav_count": nav_count(chain),
            "needs_rescore": needs_rescore(), "stale_runs": stale_runs(),
            "rules": rules, "rules_sha": rules_sha, "rules_short": rules_sha[:8],
            "rules_rows": store.rules_rows(), "pass_mark": rules.score.pass_mark,
            "snapshot": snap, "snapshots": store.snapshots(),
            "snapshot_method": method_of(snap),
            "tab_labels": TAB_LABELS, "axis_labels": AXIS_LABELS, **ctx})

    # ---------------- 用例管理页（首页；跨配置汇总，按配置的分数在配置管理页）
    @app.get("/")
    async def cases(request: Request, dim: str = "", topic: str = "",
                    granularity: str = "", sort: str = "pending", flag_only: str = "", page: int = 0):
        all_rows = rows(request, snapshot=snapshot_of(request))
        summary = views.score_summary(all_rows, rules, method_of(snapshot_of(request)))
        known_dims = {r["dim"] for r in all_rows if r["dim"]}
        comp = views.composite(summary["dims"], rules, known_dims=known_dims)
        stats = views.question_stats(all_rows)
        qs = store.questions()
        by_case: dict[str, list] = {}
        for q in qs:
            if dim and (q["dim"] or "") != dim:
                continue
            if topic and (q["topic"] or "") != topic:
                continue
            if granularity and (q["granularity"] or "") != granularity:
                continue
            by_case.setdefault(q["case_id"] or q["uid"], []).append(q)
        conv_counts: dict[str, dict] = {}
        for r in all_rows:
            c = conv_counts.setdefault(r["case_id"], {"n": 0, "undecided": 0})
            c["n"] += 1
            c["undecided"] += r["c_status"] == "undecided"
        health = views.case_health(all_rows, summary["cases"])
        table = []
        for case, members in by_case.items():
            cs = summary["cases"].get(case, {})
            flags = list(health.get(case, []))
            member_stats = [stats.get(q["qkey"], {}) for q in members]
            diffs = [s["difficulty"] for s in member_stats if s.get("difficulty") is not None]
            discs = [s["discrimination"] for s in member_stats if s.get("discrimination") is not None]
            st = member_stats[0] if member_stats else {}
            table.append({
                "case_id": case, "dim": members[0]["dim"], "topic": members[0]["topic"],
                "variants": len(members), "convs": conv_counts.get(case, {"n": 0})["n"],
                "undecided": conv_counts.get(case, {}).get("undecided", 0),
                "score": cs.get("score"), "score_n": cs.get("n"),
                "score_provisional": cs.get("provisional"),
                "difficulty": round(sum(diffs) / len(diffs), 4) if diffs else None,
                "discrimination": round(sum(discs) / len(discs), 4) if discs else None,
                "models_n": max((s.get("models_n") or 0) for s in member_stats) if member_stats else 0,
                "stability": st.get("stability"), "group_consistency": st.get("group_consistency"),
                "flags": flags})
        table.sort(key=lambda t: ({"pending": -(t["undecided"] or 0), "score": (t["score"] if t["score"] is not None else 999),
                                   "difficulty": -(t["difficulty"] or -1)}.get(sort, 0), t["case_id"]))
        if flag_only:
            table = [t for t in table if t["flags"]]
        size = 50
        total = len(table)
        page = max(0, min(page, (max(total, 1) - 1) // size))
        return render(request, "cases.html", summary=summary,
                      composite=comp, known_dims=sorted(known_dims),
                      table=table[page * size:(page + 1) * size], total=total, page=page,
                      pages=max(1, (total + size - 1) // size),
                      dims=sorted({q["dim"] for q in qs if q["dim"]}),
                      topics=sorted({q["topic"] for q in qs if q["topic"]}),
                      granularities=sorted({q["granularity"] for q in qs if q["granularity"]}),
                      f_dim=dim, f_topic=topic, f_granularity=granularity, sort=sort,
                      flag_only=flag_only)

    # ---------------- 配置管理页（被测配置；执行记录在 /runs）
    @app.get("/configs")
    async def configs_page(request: Request):
        rs = rows(request)
        known_dims = {r["dim"] for r in rs if r["dim"]}
        groups: dict[str, list] = {}
        for r in rs:
            groups.setdefault(r.get("subject_fp") or "?", []).append(r)

        def subject_label(fp: str, grp: list[dict]) -> str:
            """差异属性拼名（doc/13 §2.3 口径）：跨组差异的属性入名；全组相同回退 profile id。"""
            attrs = [("model", "模型"), ("reasoning_effort", "努力"), ("skills_mode", "技能"),
                     ("skills_base", "基底"), ("agent", "agent")]
            parts = [f"{label} {grp[0].get(k) or '?'}" for k, label in attrs
                     if any(str(g.get(k)) != str(grp[0].get(k)) for g in [grp[0]])]
            return fp[:8] if not parts else (grp[0].get("profile_id") or fp[:8])

        subj_rows = {r["run_id"]: r for r in rs}
        incomplete = {r["run_id"] for r0 in store.runs()
                      for r in [subj_rows.get(r0["run_id"])] if r and r0.get("subject_incomplete")}
        table = []
        for fp, grp in groups.items():
            summary = views.score_summary(grp, rules)
            comp = views.composite(summary["dims"], rules, known_dims=known_dims)
            table.append({
                "subject_fp": fp, "label": subject_label(fp, grp),
                "model": grp[0].get("model") or views.model_label(grp[0]),
                "effort": grp[0].get("reasoning_effort"), "agent": grp[0].get("agent"),
                "skills_mode": grp[0].get("skills_mode"), "skills_base": grp[0].get("skills_base"),
                "incomplete": any(r["run_id"] in incomplete for r in grp),
                "n": len(grp), "undecided": sum(1 for r in grp if r["c_status"] == "undecided"),
                "score": comp.get("score"), "provisional": comp.get("provisional"),
                "complete": comp.get("complete"),
                "dims_n": len(comp.get("parts") or {}),
                "run_ids": sorted({r["run_id"] for r in grp})})
        table.sort(key=lambda t: -t["n"])
        return render(request, "configs.html", table=table,
                      run_labels=store.run_labels(), known_dims=sorted(known_dims))

    def _run_table(rs: list[dict]) -> list[dict]:
        """执行记录表（/runs 页）：每行一次 run——标签/被测/干预采样/期望版本/技能。"""
        labels = store.run_labels()
        stale = set(stale_runs())
        out = []
        for r0 in reversed(store.runs()):          # 最新执行在前
            rid = r0["run_id"]
            grp = [r for r in rs if r["run_id"] == rid]
            try:
                manifest = json.loads(r0["snapshot_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                manifest = {}
            inner = manifest.get("snapshot") if isinstance(manifest.get("snapshot"), dict) else {}
            modes = "+".join(str(x) for x in (manifest.get("modes") or inner.get("modes") or []))
            trials = manifest.get("trials") or inner.get("trials")
            prof = manifest.get("profile") or {}
            effort = prof.get("content", {}).get("reasoning_effort") if isinstance(prof, dict) else None
            out.append({
                "run_id": rid, "label": labels.get(rid) or rid, "title": r0["title"] or "",
                "profile": r0["profile_id"] or "?",
                "models": sorted({m for r in grp for m in json.loads(r["models_json"] or "[]")}),
                "effort": effort,
                "mode_part": f"{modes}×{trials}" if modes and trials else (modes or ""),
                "evalset_version": views.attr_value(r0, "evalset_version"),
                "skills_mode": sorted({r["skills_mode"] for r in grp if r["skills_mode"]}),
                "stale": rid in stale,
                "diagnostic": bool(r0.get("diagnostic")),
                "models_mismatch": any(
                    r.get("model") and json.loads(r["models_json"] or "[]")
                    and r["model"] not in json.loads(r["models_json"] or "[]") for r in grp),
                "n": len(grp),
                "undecided": sum(1 for r in grp if r["c_status"] == "undecided"),
                "at": r0["started_at"] or r0["imported_at"] or ""})
        return out

    # ---------------- 执行记录页
    @app.get("/runs")
    async def runs_page(request: Request):
        return render(request, "runs.html", run_table=_run_table(rows(request)),
                      run_labels=store.run_labels())

    # ---------------- 新建用例页（须先于 /case/{case_id} 注册）
    @app.get("/case/new")
    async def case_new(request: Request):
        m2c = _md2case()
        mapping = m2c.load_mapping()
        points = m2c.load_knowledge([m2c.KNOWLEDGE_DIR]) if m2c.KNOWLEDGE_DIR.is_dir() else {}
        return render(request, "case_new.html",
                      dims=mapping["dims"], multi_turn_dims=mapping.get("multi_turn_dims", []),
                      rewrites=mapping.get("rewrites", []),
                      tag_vocab={k: v for k, v in (mapping.get("tags") or {}).items() if k != "业务"},
                      dirs=_case_dirs(cases_root), default_dir=_default_dir(cases_root),
                      cn_data={
                          "dims": mapping["dims"],
                          "multiTurnDims": mapping.get("multi_turn_dims", []),
                          "rewrites": mapping.get("rewrites", []),
                          "points": [{"id": p, "options": sorted(o["options"])}
                                     for p, o in sorted(points.items())],
                          "suggested": _suggest_ids(cases_root, mapping["dims"]),
                          "maxTurns": 5})

    # ---------------- 用例详情
    @app.get("/case/{case_id}")
    async def case_detail(request: Request, case_id: str, axis: str = "subject", standard_fp: str = "",
                          variant: str = "", saved: str = "",
                          pair: Annotated[list[str], Query()] = []):
        members = [q for q in store.questions() if (q["case_id"] or q["uid"]) == case_id]
        if not members:
            return render(request, "error.html", message=f"未知用例：{case_id}")
        members.sort(key=lambda q: (q["variant_index"] or 0, q["uid"]))
        seed = members[0]
        cur = store.current_spec(seed["qkey"])
        fp = standard_fp or (cur[0] if cur else None)
        spec = store.spec_of(seed["qkey"], fp) if fp else {}
        history = [dict(r) for r in store.conn.execute(
            "SELECT standard_fp AS fp, evalset_version, imported_at FROM specs"
            " WHERE qkey=? ORDER BY imported_at",
            (seed["qkey"],))]
        all_rows = rows(request)
        case_rows = [r for r in all_rows if r["case_id"] == case_id]
        axis_values = sorted({views.attr_value(r, axis) for r in case_rows})
        convs = []
        for r in sorted(case_rows, key=lambda r: (r["answer_id"])):
            concl = store.conclusion(r["answer_id"], r["current_standard_fp"] or "",
                                     chain_of(request), rules_sha) or {}
            chips = []
            for er in store.effective_rows(r["answer_id"], r["current_standard_fp"] or "",
                                           chain_of(request)):
                chips.append({"id": er["row"]["item_id"], "kind": er["row"]["kind"],
                              "value": er["eff"].value, "score": er["eff"].score,
                              "source": er["eff"].source})
            convs.append({"r": r, "concl": concl, "chips": chips,
                          "axis_label": views.attr_value(r, axis),
                          "pending_items": sum(1 for c in chips if c["value"] is None)})
        health = views.case_health(case_rows, views.case_scores(case_rows, rules)).get(case_id, [])
        by_uid: dict[str, list] = {}
        for c in convs:
            by_uid.setdefault(c["r"]["uid"], []).append(c)
        variant_cards = []
        for q in members:
            cur_v = store.current_spec(q["qkey"])
            vspec = cur_v[1] if cur_v else {}
            rows_v = by_uid.get(q["uid"], [])
            scores = [c["concl"].get("score") for c in rows_v
                      if c["concl"].get("score") is not None]
            variant_cards.append({
                "uid": q["uid"], "index": q["variant_index"] or 0, "rewrite": q["rewrite"] or "",
                "standard_fp": q["current_standard_fp"] or "",
                "questions": [t.get("content") or "" for t in vspec.get("turns") or []
                              if t.get("role") == "user"],
                "convs": len(rows_v),
                "score": sum(scores) / len(scores) if scores else None,
                "undecided": sum(1 for c in rows_v
                                 if (c["concl"].get("status") or c["r"]["c_status"]) == "undecided"),
                "pending": sum(c["pending_items"] for c in rows_v),
            })
        variant_stats = {
            "all": len(variant_cards),
            "undecided": sum(1 for v in variant_cards if v["undecided"]),
            "with_convs": sum(1 for v in variant_cards if v["convs"]),
        }
        # 变体选择只决定初始聚焦，不裁剪对话：前端可无刷新切换，全部对话也始终可筛
        uids = {v["uid"] for v in variant_cards}
        if variant == "__all":
            show_all, focus_variant = True, ""
        elif variant in uids:
            show_all, focus_variant = False, variant
        else:
            show_all, focus_variant = False, (variant_cards[0]["uid"] if variant_cards else "")
        def status_of(c):
            return c["concl"].get("status") or c["r"]["c_status"]
        # 服务端先按初始聚焦渲染对话（无 JS 也自洽），前端可再切变体/全部
        scope_convs = convs if show_all else [c for c in convs if c["r"]["uid"] == focus_variant]
        conv_stats = {
            "all": len(scope_convs),
            "undecided": sum(1 for c in scope_convs if status_of(c) == "undecided"),
            "failed": sum(1 for c in scope_convs if status_of(c) in ("invalid", "error")),
        }
        conv_totals = {
            "all": len(convs),
            "undecided": sum(1 for c in convs if status_of(c) == "undecided"),
            "failed": sum(1 for c in convs if status_of(c) in ("invalid", "error")),
        }
        conv_group_counts: dict[str, int] = {}
        for c in scope_convs:
            conv_group_counts[c["axis_label"]] = conv_group_counts.get(c["axis_label"], 0) + 1
        # 勾选两段并排看：逐轮左右对照（§5.4）
        pair_data, pair_rows = [], []
        if len(pair) == 2:
            by_aid = {c["r"]["answer_id"]: c for c in convs}
            for aid in pair:
                c = by_aid.get(aid)
                if c is None:
                    continue
                record = json.loads((store.answer(aid) or {}).get("record_json") or "{}")
                live = [t for t in record.get("turns") or [] if t.get("source") not in ("prefill", "preset")]
                pair_data.append({**c, "turns": live})
        if len(pair_data) == 2:
            n = max(len(p["turns"]) for p in pair_data)
            pair_rows = [{"index": i + 1,
                          "cells": [p["turns"][i] if i < len(p["turns"]) else None for p in pair_data]}
                         for i in range(n)]
        injections = spec.get("injections") or []
        # 权重编辑器（doc/15 §4.3）：数据源 = item_weights 有效权重（按用例，恒百分制）。
        # 无权重行 → 等权展示（total 可折算），保存时必须覆盖全部计分条目。
        case_weights, case_wfp, weights_incomplete = store.effective_weights(case_id)
        # 权重历史（可追溯）：每次保存一组；「查看」直读该 weight_fp 下的现存结论
        w_hist = store.weight_history(case_id)
        _w_chain, (_w_rules, _w_rsha) = chain_of(request), rules_state(request)
        for g in w_hist:
            g["rows"] = store.case_conclusions_under_weight(case_id, g["weight_fp"], _w_chain, _w_rsha)
        wlabel_row = store.conn.execute(
            "SELECT label FROM item_weights WHERE case_id=? AND active=1 AND label IS NOT NULL"
            " ORDER BY set_at DESC LIMIT 1", (case_id,)).fetchone()
        weight_facts = []
        wf = [f for f in spec.get("facts") or [] if f.get("kind") in ("required", "expected")]
        weight_map: dict[str, int] = {}
        for f in wf:
            if case_weights:
                pct = round(case_weights.get(f["id"], 0))
            else:
                pct = round(100 / len(wf)) if wf else 0
            weight_map[f["id"]] = pct
            weight_facts.append({"id": f["id"], "kind": f["kind"], "pct": pct})
        # 条目详情（简要列表点「详情」下钻）：全部条目类型
        item_details = []
        for p in spec.get("preconditions") or []:
            item_details.append({"id": p.get("id"), "kind": "precond", "text": p.get("text") or "",
                                 "turn": p.get("turn"), "keywords": [], "group": None,
                                 "weight": None, "note": "门槛：不成立 → 会话无效，不计分"})
        for p in spec.get("path") or []:
            item_details.append({"id": "path", "kind": "path", "text": f"必须加载技能 {(p.get('action') or {}).get('skill')}",
                                 "turn": None, "keywords": [], "group": None,
                                 "weight": None, "note": "否决项：违反 → 总分 0"})
        for f in spec.get("facts") or []:
            note = {"forbidden": "否决项：被采信 → 总分 0",
                    "required": "按权重计分；判「错」→ 总分 0",
                    "expected": "按权重计分"}.get(f.get("kind"), "")
            item_details.append({"id": f.get("id"), "kind": f.get("kind"), "text": f.get("text") or "",
                                 "turn": f.get("turn"), "keywords": f.get("keywords") or [],
                                 "group": f.get("group"),
                                 "nature": f.get("nature"), "partial_rule": f.get("partial_rule"),
                                 "weight": weight_map.get(f.get("id")) if f.get("kind") in ("required", "expected") else None,
                                 "note": note})
        return render(request, "case.html", case_id=case_id, members=members, seed=seed, spec=spec,
                      standard_fp=fp or "", history=history, convs=convs, axis=axis,
                      axis_values=axis_values, tags=json.loads(seed["tags_json"] or "[]"),
                      injections=injections, health=health, variant_cards=variant_cards,
                      weight_facts=weight_facts, weight_map=weight_map,
                      weights_incomplete=weights_incomplete, weight_fp=case_wfp,
                      weight_history=w_hist,
                      weight_label=(wlabel_row["label"] if wlabel_row else None),
                      item_details=item_details, saved=saved,
                      variant_stats=variant_stats, conv_stats=conv_stats,
                      conv_totals=conv_totals, conv_group_counts=conv_group_counts,
                      focus_variant=focus_variant, show_all=show_all,
                      pair=pair, pair_data=pair_data, pair_rows=pair_rows,
                      headline=method_of(snapshot_of(request)).headline(seed["dim"] or "?"),
                      summary_dim=(spec.get("dim") or "?"))

    # ---------------- 复核队列
    @app.get("/review")
    async def review_queue(request: Request, tab: str = "undecided", run_id: str = "",
                           dim: str = "", op: str = "", spot: int = 10, page: int = 0):
        if tab not in TAB_LABELS:
            tab = "undecided"
        spot = spot if spot in (10, 20, 100) else 10
        chain = chain_of(request)
        convs, meta = store.review_conversations(chain, rules, rules_sha, tab=tab,
                                                 run_id=run_id or None, dim=dim or None,
                                                 spot_ratio=spot / 100, operator=op or None, page=page)
        operators = [r[0] for r in store.conn.execute(
            "SELECT DISTINCT operator FROM verdicts WHERE chain_sha=? AND operator IS NOT NULL"
            " ORDER BY operator", (chain,))]
        keep = {"run_id": run_id, "dim": dim, "op": op, **({"spot": spot} if tab == "spot" else {})}
        labels = store.run_labels()
        return render(request, "review_queue.html", tab=tab, convs=convs, meta=meta,
                      run_ids=[r["run_id"] for r in store.runs()], run_labels=labels,
                      dims=sorted({q["dim"] for q in store.questions() if q["dim"]}),
                      operators=operators, q_run=run_id, q_dim=dim, q_op=op, spot=spot,
                      queue_url=lambda t: "/review?" + urlencode({**keep, "tab": t}),
                      pager_base="/review?" + urlencode({**keep, "tab": tab}) + "&",
                      page=meta["page"],
                      pages=max(1, (meta["total"] + 19) // 20),
                      pending_rejudge=len(store.pending_rejudge(chain)))

    # ---------------- 复核页
    @app.get("/review/{answer_id:path}")
    async def review_page(request: Request, answer_id: str, tab: str = "undecided",
                          op: str = "", spot: int = 10):
        chain = chain_of(request)
        try:
            v = store.conversation_view(answer_id, chain, rules, rules_sha)
        except StoreError as e:
            return render(request, "error.html", message=str(e))
        if tab not in TAB_LABELS:
            tab = "undecided"
        spot = spot if spot in (10, 20, 100) else 10
        convs, _ = store.review_conversations(chain, rules, rules_sha, tab=tab, size=10 ** 6,
                                              spot_ratio=spot / 100, operator=op or None,
                                              first_input=False)
        ids = [c["answer_id"] for c in convs]
        extra = urlencode({**({"op": op} if op else {}), **({"spot": spot} if tab == "spot" else {})})
        suffix = f"&{extra}" if extra else ""
        prev_url = next_url = ""
        if answer_id in ids:
            i = ids.index(answer_id)
            # uid 带 #（如 ec-0201#0），拼 URL 必须转义，否则被浏览器当锚点截断
            prev_url = f"/review/{quote(ids[i - 1], safe='/')}?tab={tab}{suffix}" if i > 0 else ""
            next_url = f"/review/{quote(ids[i + 1], safe='/')}?tab={tab}{suffix}" if i + 1 < len(ids) else ""
            pos, total = i + 1, len(ids)
        else:
            pos, total = 0, 0
        m = rules.score.map
        return render(request, "review.html", v=v, tab=tab, pos=pos, total_q=total,
                      prev_url=prev_url, next_url=next_url, score_map=m,
                      extra_qs=extra,
                      scales={k: list(v_) for k, v_ in SCALES.items()},
                      pass_mark=rules.score.pass_mark,
                      detail=(v["conclusion"] or {}).get("detail") or {})

    # ---------------- 结果对比
    @app.get("/compare")
    async def compare(request: Request, axis: str = "subject", base: str = "", target: str = "",
                      any: str = "", fix: str = "", factors: str = "", run: str = "",
                      std_a: str = "", std_b: str = ""):
        rep = error = shap = diff = None
        shap_note = shap_names = None
        std_rep = None
        if std_a and std_b:                       # 标准修订验证（doc/15 §4.7 P2）
            try:
                std_rep = store.standard_compare(std_a, std_b, chain_of(request), rules_sha)
            except StoreError as e:
                error = str(e)
        rs = rows(request, with_records=True)
        run_order = {r["run_id"]: i for i, r in enumerate(store.runs())}
        anys = tuple(a.strip() for a in (any or "").split(",") if a.strip())
        fixes: dict[str, str] = {}
        for part in (fix or "").split(","):
            k, sep, v = part.partition("=")
            if sep and k.strip() and v.strip():
                fixes[k.strip()] = v.strip()

        # 被测配置清单：指纹 → 完整字段表（runs.subject_json，旧库缺列时回退拆字段）
        # + 跑过的评测集——下拉选项与差异表的数据源
        subj_json: dict[str, dict] = {}
        for r in store.conn.execute(
                "SELECT DISTINCT subject_fp, subject_json FROM runs WHERE subject_fp IS NOT NULL"):
            if r["subject_json"]:
                try:
                    subj_json.setdefault(r["subject_fp"], json.loads(r["subject_json"]))
                except ValueError:
                    pass
        subjects: dict[str, dict] = {}
        for r in rs:
            fp = views.attr_value(r, "subject")
            s = subjects.setdefault(fp, {"fields": {}, "evalsets": set()})
            s["evalsets"].add(views.attr_value(r, "evalset_version"))
            if fp in subj_json:
                s["fields"] = subj_json[fp]
            elif not s["fields"]:
                s["fields"] = {"model": views.attr_value(r, "model"),
                               "reasoning_effort": views.attr_value(r, "effort"),
                               "agent": views.attr_value(r, "agent"),
                               "skills_base": views.attr_value(r, "skills_base"),
                               "skills_mode": views.attr_value(r, "skills_mode")}

        if base and target and axis != "subject":
            # 旧式轴对比（URL 直达）：按轴取值比较，行为与改造前一致
            try:
                rep = views.compare_scores(rs, axis=axis, base=base, target=target, rules=rules,
                                           method=method_of(snapshot_of(request)),
                                           fixes=fixes, anys=anys, run_order=run_order)
            except ValueError as e:
                error = str(e)
        elif base and target:
            # 主路径：直接选两个被测配置，系统自己找出全部差异，不让用户挑维度
            if base == target:
                error = "基线与对照是同一个被测配置，没有可对比的差异。"
            elif base not in subjects or target not in subjects:
                error = f"未见过该被测配置：{base if base not in subjects else target}"
            else:
                sb, st = subjects[base], subjects[target]
                fa, fb = sb["fields"], st["fields"]
                keys = FP_FIELD_ORDER + tuple(sorted((set(fa) | set(fb)) - set(FP_FIELD_ORDER)))
                diff_rows = [{"field": k, "a": _fmt_val(fa.get(k)), "b": _fmt_val(fb.get(k)),
                              "changed": fa.get(k) != fb.get(k)}
                             for k in keys if k in fa or k in fb]
                changed = [r["field"] for r in diff_rows if r["changed"]]
                diff = {"rows": diff_rows, "changed": changed,
                        "evalsets_a": sorted(sb["evalsets"]),
                        "evalsets_b": sorted(st["evalsets"]),
                        "same_evalsets": sb["evalsets"] == st["evalsets"]}
                if not diff["same_evalsets"] and "evalset_version" not in anys:
                    error = ("两个被测配置跑在不同的评测集快照上"
                             f"（基线 {'、'.join(diff['evalsets_a']) or '？'} / "
                             f"对照 {'、'.join(diff['evalsets_b']) or '？'}），"
                             "题目不同、分数没有可比性。要验证标准修订的效果，"
                             "请用页面底部「标准修订验证」。")
                else:
                    try:
                        rep = views.compare_scores(rs, axis="subject", base=base, target=target,
                                                   rules=rules, method=method_of(snapshot_of(request)),
                                                   fixes=fixes, anys=anys, run_order=run_order)
                    except ValueError as e:
                        error = str(e)
        if factors and not error:
            try:
                facs = [f.strip() for f in factors.split(",") if f.strip()]
                cells, shared = views.shapley_cells(rs, facs, rules, method_of(snapshot_of(request)))
                shap = views.shapley(cells, facs)
                shap_names = "、".join(FIELD_LABELS.get(f, f) for f in facs)
            except ValueError as e:
                error = str(e)
        elif diff and not error and len(diff["changed"]) >= 2:
            # 多个组件同时变且未手动指定：自动按差异组件做 Shapley 分摊（缺组合时列出待补跑）
            shap_fields = [FP_ATTR_NAME.get(f, f) for f in diff["changed"]]
            shapable = (2 <= len(shap_fields) <= 3
                        and all(f in views.ATTRS and f != "skills_mode"
                                and f not in views.SAMPLING_ATTRS for f in shap_fields))
            if shapable:
                try:
                    cells, shared = views.shapley_cells(rs, shap_fields, rules,
                                                        method_of(snapshot_of(request)))
                    shap = views.shapley(cells, shap_fields)
                    shap_names = "、".join(FIELD_LABELS.get(f, f) for f in diff["changed"])
                except ValueError as e:
                    shap_note = f"总差异无法自动分摊：{e}"
            else:
                shap_note = "同时变化的组件超出可分摊范围（分摊只支持比较属性里的 2–3 个组件）。"
        diag_n = sum(1 for r in store.answer_view_rows(chain_of(request), rules_sha)
                     if r.get("diagnostic"))
        # 放开属性的候选：去掉已自动放行的（含轴本身）、采样标识（默认不限）、
        # 不可比的 skills_mode——列出来只会诱导无效输入
        followed = views.follows(axis)
        # 分摊组件与主对比轴无关，只去掉轴本身、采样标识、skills_mode
        def _label(s: dict) -> str:
            f = s["fields"]
            ag = f.get("agent")
            ag_s = (f"{ag.get('name')}@{ag.get('version') or '?'}"
                    if isinstance(ag, dict) else str(ag or "?"))
            sbx = str(f.get("skills_base") or "—")
            parts = [str(f.get("model") or "?"), str(f.get("reasoning_effort") or "—"),
                     ag_s, sbx[:12] + ("…" if len(sbx) > 12 else ""),
                     str(f.get("skills_mode") or "—")]
            return " · ".join(parts)
        return render(request, "compare.html", axis=axis, std_rep=std_rep,
                      std_a=std_a, std_b=std_b, snapshot_options=[s["evalset_version"] for s in store.snapshots()],
                      diagnostic_count=diag_n,
                      base=base, target=target, any=any, fix=fix, factors=factors,
                      rep=rep, error=error, shap=shap, shap_note=shap_note,
                      shap_names=shap_names, diff=diff,
                      subject_options=[{"fp": fp, "label": _label(subjects[fp])}
                                       for fp in sorted(subjects)],
                      attrs=[a for a in views.ATTRS if a not in followed
                             and a not in views.SAMPLING_ATTRS and a != "skills_mode"],
                      factor_attrs=[a for a in views.ATTRS if a != axis
                                    and a not in views.SAMPLING_ATTRS and a != "skills_mode"],
                      field_labels=FIELD_LABELS)

    # ---------------- 判定质量
    @app.get("/judging")
    async def judging(request: Request):
        chain = chain_of(request)
        agree = views.judge_agreement(store, chain, rules)
        rs = rows(request)
        uid_of = {r["answer_id"]: r["uid"] for r in rs}
        voided = []
        for a_id in (r["answer_id"] for r in store.conn.execute("SELECT DISTINCT answer_id FROM reviews")):
            for v in store.voided_reviews(a_id):
                voided.append({**v, "uid": uid_of.get(a_id, "?")})
        # 人工会话分与自动分差 > 20 的对话（§5.6）
        big_gap = []
        for r in rs:
            detail = json.loads(r.get("detail_json") or "{}")
            if r.get("c_score_source") == "human" and detail.get("auto_score") is not None:
                gap = abs((r["c_score"] or 0) - detail["auto_score"])
                if gap > 20:
                    big_gap.append({"uid": r["uid"], "answer_id": r["answer_id"],
                                    "human": r["c_score"], "auto": detail["auto_score"], "gap": round(gap, 1)})
        big_gap.sort(key=lambda x: -x["gap"])
        return render(request, "judging.html", agree=agree, voided=voided[:100], big_gap=big_gap[:20],
                      chains=store.chains(), rules_sha=rules_sha[:8])

    # ---------------- 配置详情（三件套可查：内容 + 差异，doc/15 §4 配置可解释）
    @app.get("/detail/rules/{sha}")
    async def detail_rules(request: Request, sha: str, diff: str = ""):
        rows_r = store.rules_rows()
        row = next((r for r in rows_r if r["rules_sha"] == sha), None)
        if row is None:
            return render(request, "error.html", message=f"库中没有计分规则 {sha}")
        diff_rows = []
        other = next((r for r in rows_r if r["rules_sha"] == diff), None) if diff else None
        if other is not None:
            a, b = row["rules"], other["rules"]
            for k in sorted(set(a) | set(b)):
                if a.get(k) != b.get(k):
                    diff_rows.append({"key": k, "a": a.get(k), "b": b.get(k)})
        return render(request, "detail_rules.html", row=row,
                      others=[r for r in rows_r if r["rules_sha"] != sha],
                      diff_sha=(other["rules_sha"] if other else ""),
                      diff_name=((other["name"] or "") if other else ""),
                      diff_rows=diff_rows, is_current=(sha == rules_sha))

    @app.get("/detail/chain/{sha}")
    async def detail_chain(request: Request, sha: str):
        row = next((c for c in store.chains() if c["chain_sha"] == sha), None)
        if row is None:
            return render(request, "error.html", message=f"库中没有判定配置 {sha}")
        mapping = None
        if sha == cfg.chain_sha:                     # 当前 operators.yaml 的逐条目链组成
            mapping = [{"kind": k, "ops": [f"{o.id}@{o.version}" for o in ops]}
                       for k, ops in sorted((cfg.chains or {}).items())]
        return render(request, "detail_chain.html", row=row, mapping=mapping,
                      instance_specs=(cfg.instance_specs or {} if sha == cfg.chain_sha else None),
                      is_current=(sha == cfg.chain_sha))

    @app.get("/detail/snapshot/{version}")
    async def detail_snapshot(request: Request, version: str):
        row = store.snapshot(version)
        if row is None:
            return render(request, "error.html", message=f"库中没有快照 {version}")
        members = [dict(r) for r in store.conn.execute(
            "SELECT sv.qkey, sv.standard_fp, q.uid, q.variant_index, q.rewrite, q.current_standard_fp"
            " FROM spec_versions sv JOIN questions q ON q.qkey=sv.qkey"
            " WHERE sv.evalset_version=? ORDER BY q.variant_index, sv.qkey", (version,))]
        for m in members:
            m["is_current"] = (m["current_standard_fp"] == m["standard_fp"])
        seeds = {r["case_id"] for r in store.conn.execute(
            "SELECT DISTINCT case_id FROM item_weights WHERE source='import'")}
        return render(request, "detail_snapshot.html", row=row, members=members,
                      seeded_cases=sorted(seeds),
                      is_current=(version == store.current_snapshot()))

    # ---------------- 接口（§9）
    def _batch_target(body: ReviewBatchIn) -> tuple[str, str]:
        a = store.answer(body.answer_id)
        if a is None:
            raise StoreError("答案不存在")
        cur = store.current_spec(a["qkey"])
        fp = cur[0] if cur else a["judge_fp_at_run"]
        return body.chain or default_chain, fp

    @app.post("/api/review/preview")
    async def review_preview(body: ReviewBatchIn):
        try:
            chain, fp = _batch_target(body)
            v = store.preview_conclusion(body.answer_id,
                                         [e.model_dump() for e in body.entries], body.total, fp, chain, rules)
        except StoreError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return _ev_dict(v)

    @app.post("/api/reviews")
    async def review_submit(body: ReviewBatchIn):
        try:
            chain, fp = _batch_target(body)
            c = store.submit_reviews(body.answer_id, [e.model_dump() for e in body.entries], body.total,
                                     body.reviewer or "?", fp, chain, rules, rules_sha)
        except StoreError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        _nav_cache.clear()
        tab = "undecided"
        convs, _ = store.review_conversations(chain, rules, rules_sha, tab=tab, size=10 ** 6,
                                              first_input=False)
        ids = [x["answer_id"] for x in convs]
        # 跳转跟随当前位置：从本段在队列中的位置向后取下一段（绕回、跳过自己），
        # 不固定回到队首——从其他标签（如模型抽检）进来提交后不被带离
        if body.answer_id in ids:
            i = ids.index(body.answer_id)
            rest = ids[i + 1:] + ids[:i]
        else:
            rest = ids
        return {**{k: c.get(k) for k in ("status", "reason", "score", "score_min", "score_max",
                                         "auto_score", "score_source")},
                "detail": c.get("detail"),
                "next": f"/review/{quote(rest[0], safe='/')}?tab={tab}" if rest else "/review?tab=undecided"}

    @app.post("/api/review")
    async def review_compat(body: dict):
        """单条改判（兼容旧入口）。"""
        try:
            a = store.answer(body.get("answer_id", ""))
            if a is None:
                return JSONResponse({"error": "答案不存在"}, status_code=400)
            cur = store.current_spec(a["qkey"])
            fp = cur[0] if cur else a["judge_fp_at_run"]
            status, reason = store.submit_review(body["answer_id"], body["item_id"], body["value"],
                                                 body.get("reason", ""), body.get("reviewer") or "?",
                                                 fp, default_chain, rules, rules_sha)
        except (StoreError, KeyError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        _nav_cache.clear()
        return {"episode_status": status, "reason": reason}

    @app.post("/api/case/weights")
    async def case_weights(body: WeightsIn):
        """权重保存（按用例，doc/15 §4.3）：写 item_weights 有效行 + 刷新 weight_fp，
        随后只重算本用例在所有有判定的链下的结论——不产生新标准、不需要 rejudge。"""
        try:
            res = store.revise_weights(body.case_id, dict(body.weights), body.label)
            n = store.rescore_cases([body.case_id], rules, rules_sha)
        except StoreError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {**res, "rescored": n}

    @app.post("/api/case/weights-restore")
    async def case_weights_restore(body: WeightsRestoreIn):
        """权重切回历史配置：把该组值写成新的有效行（追加，历史不删）+ 只重算本用例。
        标准修订后条目集变化时 revise_weights 会拒绝并说明缺哪些条目。"""
        try:
            group = next((g for g in store.weight_history(body.case_id)
                          if g["set_at"] == body.set_at and (g["label"] or "") == body.label.strip()), None)
            if group is None:
                return JSONResponse({"error": f"没有这次保存记录：{body.set_at}"}, status_code=400)
            if group["active"]:
                return JSONResponse({"error": "该权重配置已是当前配置"}, status_code=400)
            label = f"切回·{group['label'] or body.set_at[:16]}"
            res = store.revise_weights(body.case_id, group["weights"], label)
            n = store.rescore_cases([body.case_id], rules, rules_sha)
        except StoreError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        _nav_cache.clear()
        return {**res, "rescored": n, "label": label}

    @app.post("/api/run/rename")
    async def run_rename(body: RunRenameIn):
        """执行记录命名（命名改造）：改展示标题，空串回默认拼接标题；run_id 不变。"""
        try:
            store.rename_run(body.run_id, body.title)
        except StoreError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {"run_id": body.run_id, "title": body.title.strip() or None}

    @app.post("/api/chain/rename")
    async def chain_rename(body: ChainRenameIn):
        """判定配置命名：改名称，空串清除；chain_sha 不变。"""
        try:
            store.rename_chain(body.chain_sha, body.name)
        except StoreError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {"chain_sha": body.chain_sha, "name": body.name.strip() or None}

    @app.post("/api/snapshot/rename")
    async def snapshot_rename(body: SnapshotRenameIn):
        """快照命名（展示层）：空串清除。"""
        try:
            store.rename_snapshot(body.evalset_version, body.name)
        except StoreError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {"evalset_version": body.evalset_version, "name": body.name.strip() or None}

    @app.post("/api/rules/rename")
    async def rules_rename(body: RulesRenameIn):
        """计分规则命名（展示层）：空串清除。"""
        try:
            store.rename_rules(body.rules_sha, body.name)
        except StoreError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {"rules_sha": body.rules_sha, "name": body.name.strip() or None}

    @app.post("/api/case/preview")
    async def case_preview(body: CaseNewIn):
        """新建用例预览：表单 → md 全文 + 校验错误/提示（不落盘）。"""
        try:
            md, errors, warnings = _build_case_md(body)
        except Exception as e:  # 渲染前构建失败（表单异常）按 400 报出
            return JSONResponse({"error": f"表单无法构造成用例：{e}"}, status_code=400)
        return {"md": md, "errors": errors, "warnings": warnings, "ok": not errors}

    @app.post("/api/case/create")
    async def case_create(body: CaseNewIn):
        """新建用例保存：校验通过 → 写 md 到 cases 目录（md 是唯一数据源）；
        id 查重、目录名白名单。返回下一步命令（check/build/执行）。"""
        md, errors, warnings = _build_case_md(body)
        if errors:
            return JSONResponse({"error": "校验未通过，未保存", "errors": errors,
                                 "warnings": warnings}, status_code=400)
        cid = body.id.strip()
        if not re.fullmatch(r"[a-z]{2}-[0-9]{4}", cid):
            return JSONResponse({"error": f"id 应形如 kr-0001，当前为 {cid!r}"}, status_code=400)
        existing = _existing_case_ids(cases_root)
        if cid in existing:
            return JSONResponse({"error": f"用例 id {cid} 已存在：{existing[cid]}"}, status_code=400)
        directory = body.directory.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", directory):
            return JSONResponse({"error": "目标目录名只允许字母、数字、-、_（如 order_intake）"},
                                status_code=400)
        target_dir = cases_root / directory
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{cid}.md"
        if path.exists():
            return JSONResponse({"error": f"文件已存在：{path}"}, status_code=400)
        path.write_text(md, encoding="utf-8")
        try:
            rel = path.resolve().relative_to(_REPO_ROOT).as_posix()
        except ValueError:
            rel = str(path)
        return {"id": cid, "path": rel, "warnings": warnings,
                "md": md, "hint": _build_hint(rel)}

    # ---------------- 旧路由 301（§4.1）
    @app.get("/question/{qkey}")
    async def legacy_question(qkey: str):
        q = store.question(qkey)
        if q is None:
            return RedirectResponse("/", status_code=301)
        return RedirectResponse(f"/case/{q['case_id'] or q['uid']}#{q['uid']}", status_code=301)

    @app.get("/group/{case_id}")
    async def legacy_group(case_id: str):
        return RedirectResponse(f"/case/{case_id}", status_code=301)

    @app.get("/queue")
    async def legacy_queue(request: Request, filter_name: str = "undecided"):
        tab = {"undecided": "undecided", "mismatch": "mismatch", "fail": "failed"}.get(filter_name, "undecided")
        return RedirectResponse(f"/review?tab={tab}", status_code=301)

    @app.get("/quality")
    async def legacy_quality():
        return RedirectResponse("/judging", status_code=301)

    return app


def run_server(db: str, host: str = "127.0.0.1", port: int = 8765,
               rules_path: str | None = None, operators_path: str | None = None):
    import uvicorn

    app = create_app(db, rules_path, operators_path)
    print(f"复核服务：http://{host}:{port}（评测库 {db}，仅本机）")
    uvicorn.run(app, host=host, port=port, log_level="warning")
