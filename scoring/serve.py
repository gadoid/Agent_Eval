"""复核服务（评测结果管理改造方案 §8）：所有视图从评测库取数并分页，
不再在请求中加载 jsonl 或全量重算（RunStore 与 Index 已删除）。

服务不触发算子：重判经命令行完成，页面只显示待重判数并给出命令；
改判提交按"校验 → 写入 reviews → 重算该答案结论"完成（store.submit_review）。
默认只监听本机。仅本文件依赖 FastAPI / Jinja2 / markdown / bleach（可选安装）。
"""
from __future__ import annotations

import html
import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from pydantic import BaseModel

from . import views
from .chain import default_operators_path, load_operator_config
from .items import SCALES, build_context, extract_items
from .policy import default_policy_path, load_policy
from .store import Store, StoreError

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
QUEUE_FILTERS = ("undecided", "fail", "mismatch", "pending")


# ---------------------------------------------------------------- 渲染过滤器（净化后渲染）

try:
    import bleach
    import markdown as md_lib

    _ALLOWED_TAGS = ["p", "br", "strong", "em", "code", "pre", "ul", "ol", "li", "h1", "h2", "h3", "h4",
                     "table", "thead", "tbody", "tr", "th", "td", "blockquote", "hr", "a"]

    def md_filter(text) -> Markup:
        """markdown 渲染，渲染前净化 HTML。"""
        if not text:
            return Markup("")
        raw = md_lib.markdown(str(text), extensions=["tables", "fenced_code", "nl2br"])
        return Markup(bleach.clean(raw, tags=_ALLOWED_TAGS,
                                   attributes={"a": ["href", "title"], "code": ["class"]}, strip=True))
except ImportError:  # 依赖未装时退化为纯文本
    def md_filter(text) -> Markup:
        return Markup("<pre>%s</pre>" % html.escape(str(text or "")))


def highlight_filter(text, keywords) -> Markup:
    """关键词命中片段高亮：先转义再包 <mark>。"""
    if not text:
        return Markup("")
    esc = html.escape(str(text))
    for kw in keywords or []:
        if kw:
            k = html.escape(str(kw))
            esc = esc.replace(k, f"<mark>{k}</mark>")
    return Markup(esc)


def value_class(v):
    return {"对": "ok", "满足": "ok", "成立": "ok", "拒答": "ok", "未采信": "ok",
            "错": "bad", "违反": "bad", "不成立": "bad", "采信": "bad", "未拒答": "bad"}.get(v or "", "mid")


# ---------------------------------------------------------------- 应用

class ReviewIn(BaseModel):
    answer_id: str
    item_id: str
    value: str
    reason: str = ""
    reviewer: str = ""


def _overview_matrix(rows: list[dict], axis: str) -> dict:
    """首页顶部：维度 × 干预 × 轴值的通过率（聚合展示，不做比较轴的一致性门禁）。"""
    cells: dict[tuple, dict[str, list[int]]] = {}
    for r in rows:
        if r["c_status"] not in ("pass", "fail"):
            continue
        key = (r["dim"] or "?", r["mode"] or "?")
        by_axis = cells.setdefault(key, {})
        c = by_axis.setdefault(views.attr_value(r, axis), [0, 0])
        c[0] += r["c_status"] == "pass"
        c[1] += 1
    return {f"{dim}|{mode}": {av: {"pass": c[0], "n": c[1], "rate": round(c[0] / c[1], 4) if c[1] else None}
                              for av, c in sorted(per_axis.items())}
            for (dim, mode), per_axis in sorted(cells.items())}


def create_app(db: str | Path, policy_path: str | Path | None = None,
               operators_path: str | Path | None = None) -> FastAPI:
    app = FastAPI(title="Agent_Eval 复核服务", docs_url=None, redoc_url=None)
    store = Store(db)
    policy, policy_sha = load_policy(policy_path or default_policy_path())
    cfg = load_operator_config(operators_path or default_operators_path())
    chain_sha = cfg.config_sha
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["md"] = md_filter
    templates.env.filters["highlight"] = highlight_filter
    templates.env.filters["vcls"] = value_class

    def render(request: Request, name: str, **ctx):
        return templates.TemplateResponse(request=request, name=name, context={
            "db_path": str(store.path), "chain_sha": chain_sha[:8], **ctx})

    def rows(with_records: bool = False) -> list[dict]:
        return store.answer_view_rows(chain_sha, policy_sha, with_records=with_records)

    def run_order() -> dict[str, int]:
        return {r["run_id"]: i for i, r in enumerate(store.runs())}

    def card_material(answer_id: str, judge_fp: str, item_id: str) -> dict:
        """条目卡片展示材料：期望文本/关键词与该轮回答摘录（按需解析单条答案）。"""
        a = store.answer(answer_id) or {}
        spec = store.spec_of(a.get("qkey", ""), judge_fp) or {}
        record = json.loads(a.get("record_json") or "{}")
        ctx = build_context(record, spec)
        text, keywords, answer, kind = "", [], "", "required"
        for item in extract_items(ctx):
            if item.id == item_id:
                text = item.text or ""
                keywords = item.attrs.get("keywords") or []
                answer = ctx.answer_for(item)
                kind = item.kind
                break
        return {"text": text, "keywords": keywords, "answer": answer[:400], "kind": kind}

    # ---------------- 题目列表（首页，§8）
    @app.get("/")
    async def question_list(request: Request, dim: str = "", topic: str = "", tag: str = "",
                            active: str = "", sort: str = "pending", axis: str = "profile", page: int = 0):
        all_rows = rows()
        stats = views.question_stats(all_rows, run_order())
        qs = store.questions()

        def _match(q):
            if dim and q["dim"] != dim:
                return False
            if topic and q["topic"] != topic:
                return False
            if tag and tag not in json.loads(q["tags_json"] or "[]"):
                return False
            if active in ("1", "0") and str(q["active"]) != active:
                return False
            return True

        filtered = [q for q in qs if _match(q)]

        def sort_key(q):
            s = stats.get(q["qkey"], {})
            if sort == "difficulty":
                return (-(s.get("difficulty") or -1), q["uid"])
            if sort == "discrimination":
                return (-(s.get("discrimination") or -1), q["uid"])
            if sort == "stability":
                st = s.get("stability")
                return (1.0 if st is None else st, q["uid"])
            return (-(s.get("pending") or 0), q["uid"])      # 默认：待重判优先

        filtered.sort(key=sort_key)
        size = 50
        total = len(filtered)
        page = max(0, min(page, (max(total, 1) - 1) // size))
        page_qs = filtered[page * size:(page + 1) * size]
        dims = sorted({q["dim"] for q in qs if q["dim"]})
        topics = sorted({q["topic"] for q in qs if q["topic"]})
        tags = sorted({t for q in qs for t in json.loads(q["tags_json"] or "[]")})
        pending_total = sum(s.get("pending", 0) for s in stats.values())
        matrix = _overview_matrix(all_rows, axis)
        axis_vals = sorted({av for per in matrix.values() for av in per})
        return render(request, "questions.html", questions=[
            {**q, "stats": stats.get(q["qkey"], {})} for q in page_qs],
            stats_total={"pending": pending_total}, matrix=matrix, axis_vals=axis_vals,
            dims=dims, topics=topics, tags=tags, f_dim=dim, f_topic=topic, f_tag=tag, f_active=active,
            sort=sort, axis=axis, page=page, pages=max(1, (total + size - 1) // size), total=total)

    # ---------------- 题目视图（§8）：期望固定上方，答案按比较轴分列
    @app.get("/question/{qkey}")
    async def question_view(request: Request, qkey: str, judge_fp: str = "", axis: str = "profile"):
        q = store.question(qkey)
        if q is None:
            return render(request, "error.html", message=f"未知题目：{qkey}")
        cur = store.current_spec(qkey)
        fp = judge_fp or (cur[0] if cur else None)
        if fp is None:
            answers0 = store.answers(qkey=qkey)
            fp = answers0[0]["judge_fp_at_run"] if answers0 else None
        if fp is None:
            return render(request, "error.html", message=f"题目没有任何期望：{qkey}")
        spec = store.spec_of(qkey, fp) or {}
        history = [dict(r) for r in store.conn.execute(
            "SELECT judge_fp, evalset_version, imported_at FROM specs WHERE qkey=? ORDER BY imported_at",
            (qkey,))]
        expectations = {
            "dim": spec.get("dim"), "answerable": spec.get("answerable", True),
            "dialog": spec.get("turns") or [],
            "preconditions": spec.get("preconditions") or [],
            "facts": spec.get("facts") or [], "path": spec.get("path") or [],
            "reference": spec.get("reference_answer") or "",
        }
        columns = []
        for a in store.answers(qkey=qkey):
            record = json.loads(a["record_json"])
            concl = store.conclusion(a["answer_id"], fp, chain_sha, policy_sha)
            eff_by_item = {er["row"]["item_id"]: er["eff"]
                           for er in store.effective_rows(a["answer_id"], fp, chain_sha)}
            by_turn: dict[int, list[dict]] = {}
            n_turns = len(record.get("turns") or [])
            for item in extract_items(build_context(record, spec)):
                if item.id not in eff_by_item:
                    continue
                eff = eff_by_item[item.id]
                t = item.turn
                by_turn.setdefault(t if t is not None and 1 <= t <= n_turns else max(n_turns, 1), []).append({
                    "id": item.id, "kind": item.kind, "value": eff.value, "source": eff.source,
                    "reason": eff.reason, "machine": None, "undecided": eff.value is None})
            turns = []
            for t in record.get("turns") or []:
                obs = t.get("observations") or {}
                turns.append({"index": t.get("index"), "input": t.get("input", ""),
                              "output": t.get("output", ""), "status": t.get("status"),
                              "skills": [s["name"] for s in obs.get("skills", []) if s.get("ok")],
                              "ask_user": (obs.get("ask_user") or {}).get("count", 0),
                              "chips": by_turn.get(t.get("index") or 1, [])})
            columns.append({
                "answer_id": a["answer_id"], "run_id": a["run_id"], "mode": a["mode"], "trial": a["trial"],
                "axis_label": views.attr_value(a, axis), "profile": a["profile_id"],
                "model": views.model_label(a),
                "status": (concl or {}).get("status") or "待重判",
                "reason": (concl or {}).get("reason") or "", "turns": turns})
        n_turns = max((len(c["turns"]) for c in columns), default=0)
        return render(request, "question.html", qkey=qkey, uid=q["uid"], dim=q["dim"],
                      judge_fp=fp, is_current=(cur and fp == cur[0]), history=history,
                      exp=expectations, columns=columns, n_turns=n_turns, axis=axis,
                      pending=len(store.pending_rejudge(chain_sha, qkey=qkey)))

    # ---------------- 题组视图（§8）
    @app.get("/group/{case_id}")
    async def group_view(request: Request, case_id: str, axis: str = "profile"):
        all_rows = rows()
        group_rows = [r for r in all_rows if r["case_id"] == case_id]
        stats = views.question_stats(all_rows, run_order())
        members = sorted((q for q in store.questions() if q["case_id"] == case_id),
                         key=lambda q: (q["variant_index"] or 0, q["uid"]))
        axis_values = sorted({views.attr_value(r, axis) for r in group_rows})
        matrix = []
        for m in members:
            qrows = [r for r in group_rows if r["qkey"] == m["qkey"]]
            cells = {}
            for av in axis_values:
                grp = [r for r in qrows if views.attr_value(r, axis) == av and r["c_status"] in ("pass", "fail")]
                p = sum(r["c_status"] == "pass" for r in grp)
                cells[av] = {"pass": p, "n": len(grp), "rate": round(p / len(grp), 4) if grp else None}
            matrix.append({"q": m, "cells": cells, "stats": stats.get(m["qkey"], {})})
        return render(request, "group.html", case_id=case_id, matrix=matrix,
                      axis=axis, axis_values=axis_values)

    # ---------------- 复核队列（§8）
    @app.get("/queue")
    async def queue(request: Request, filter_name: str = "undecided", run_id: str = "", page: int = 0):
        if filter_name not in QUEUE_FILTERS:
            filter_name = "undecided"
        size = 20
        cards: list[dict] = []
        total = 0
        if filter_name == "undecided":
            sql = """SELECT v.answer_id, v.item_id, v.kind, v.turn, v.value AS machine_value,
                     v.source AS machine_source, v.reason AS machine_reason, v.status,
                     q.uid, a.run_id, a.episode_id, a.mode, a.trial, a.qkey, q.current_judge_fp
                     FROM verdicts v JOIN answers a USING(answer_id) JOIN questions q ON a.qkey=q.qkey
                     WHERE q.current_judge_fp = v.judge_fp AND v.chain_sha = ?
                       AND (v.status != 'ok' OR v.value IS NULL)"""
            args: list = [chain_sha]
            if run_id:
                sql += " AND a.run_id = ?"
                args.append(run_id)
            sql += " ORDER BY v.answer_id, v.item_id"
            vr = [dict(r) for r in store.conn.execute(sql, tuple(args))]
            total = len(vr)
            page = max(0, min(page, (max(total, 1) - 1) // size))
            for r in vr[page * size:(page + 1) * size]:
                mat = card_material(r["answer_id"], r["current_judge_fp"], r["item_id"])
                cards.append({"r": r, **mat, "scale": SCALES.get(r["kind"], ())})
        elif filter_name == "fail":
            fr = [r for r in rows() if r["c_status"] == "fail" and (not run_id or r["run_id"] == run_id)]
            total = len(fr)
            page = max(0, min(page, (max(total, 1) - 1) // size))
            cards = [{"episode": r} for r in fr[page * size:(page + 1) * size]]
        elif filter_name == "mismatch":
            cand = [r for r in rows() if (not run_id or r["run_id"] == run_id) and r["current_judge_fp"]
                    and store.reviews_for(r["answer_id"])]
            mism = []
            for r in cand:
                for er in store.effective_rows(r["answer_id"], r["current_judge_fp"], chain_sha):
                    v, eff = er["row"], er["eff"]
                    if eff.source == "human" and v["value"] != eff.value:
                        mism.append({"r": {**{k: v[k] for k in
                                              ("answer_id", "item_id", "kind", "turn")},
                                          "machine_value": v["value"], "machine_source": v["source"],
                                          "machine_reason": v["reason"], "status": v["status"],
                                          "uid": r["uid"], "run_id": r["run_id"],
                                          "episode_id": r["episode_id"], "mode": r["mode"], "trial": r["trial"],
                                          "current_judge_fp": r["current_judge_fp"], "eff_value": eff.value},
                                     **card_material(r["answer_id"], r["current_judge_fp"], v["item_id"]),
                                     "scale": SCALES.get(v["kind"], ())})
            total = len(mism)
            page = max(0, min(page, (max(total, 1) - 1) // size))
            cards = mism[page * size:(page + 1) * size]
        else:                                                  # pending：待重判
            pend = store.pending_rejudge(chain_sha, run_id=run_id or None)
            total = len(pend)
            page = max(0, min(page, (max(total, 1) - 1) // size))
            cards = [{"pending": p} for p in pend[page * size:(page + 1) * size]]
        run_ids = [r["run_id"] for r in store.runs()]
        return render(request, "queue.html", filter_name=filter_name, q_run=run_id, cards=cards,
                      page=page, pages=max(1, (total + size - 1) // size), total=total, run_ids=run_ids)

    # ---------------- 人工评价写回（§8：校验 → 写入 → 重算，先校验后落盘）
    @app.post("/api/review")
    async def review(body: ReviewIn):
        try:
            a = store.answer(body.answer_id)
            if a is None:
                return JSONResponse({"error": "答案不存在"}, status_code=400)
            cur = store.current_spec(a["qkey"])
            fp = cur[0] if cur else a["judge_fp_at_run"]
            status, reason = store.submit_review(body.answer_id, body.item_id, body.value, body.reason,
                                                 body.reviewer or "?", fp, chain_sha, policy, policy_sha)
        except StoreError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {"episode_status": status, "reason": reason}

    # ---------------- 比较（§7 比较轴）
    @app.get("/compare")
    async def compare(request: Request, axis: str = "profile", fix: str = "", any: str = ""):
        rep = error = None
        fixes = {}
        for kv in (fix or "").split(","):
            if "=" in kv:
                k, _, v = kv.partition("=")
                fixes[k.strip()] = v.strip()
        anys = tuple(a.strip() for a in (any or "").split(",") if a.strip())
        try:
            rep = views.compare_axis(rows(with_records=True), axis=axis, fixes=fixes,
                                     anys=anys, run_order=run_order())
        except ValueError as e:
            error = str(e)
        return render(request, "compare.html", axis=axis, fix=fix, any=any, rep=rep, error=error,
                      attrs=views.ATTRS)

    # ---------------- 质量视图（§8：含 C5 清单与作废改判）
    @app.get("/quality")
    async def quality(request: Request):
        rs = rows(with_records=True)
        q = views.quality_from_db(store, rs, chain_sha)
        voided = []
        for r in rs:
            for v in store.voided_reviews(r["answer_id"]):
                voided.append({**v, "uid": r["uid"]})
        return render(request, "quality.html", q=q, voided=voided[:100])

    return app


def run_server(db: str, host: str = "127.0.0.1", port: int = 8765,
               policy_path: str | None = None, operators_path: str | None = None):
    import uvicorn

    app = create_app(db, policy_path, operators_path)
    print(f"复核服务：http://{host}:{port}（评测库 {db}，仅本机）")
    uvicorn.run(app, host=host, port=port, log_level="warning")
