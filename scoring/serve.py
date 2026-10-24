"""复核服务（doc/10 复核服务改造方案）：以对话为单位的百分制复核。

四个页面各答一个问题：用例 /（复核队列、复核页）/ 结果对比 / 判定质量。
全部视图从评测库取数（store.review_conversations / conversation_view /
answer_view_rows + views 聚合）；服务不触发算子，缺判定时给出 rejudge 命令。
改判按「校验 → 同一事务追加 → 重算一次结论」完成（store.submit_reviews）。
依赖 FastAPI / Jinja2 / markdown / bleach，缺任一即启动报错（不再静默退化）。
"""
from __future__ import annotations

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
from .policy import default_policy_path, load_policy
from .store import Store, StoreError

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"
TAB_LABELS = {"undecided": "结论待定", "unjudged": "有未判条目", "spot": "模型抽检",
              "mismatch": "人工≠机器", "failed": "会话失败"}
AXIS_LABELS = {"profile": "被测配置（profile）", "model": "被测模型（model）", "mode": "干预模式（mode）",
               "skills_base": "知识库（skills_base）", "evalset_version": "期望版本",
               "trial": "采样次数（trial）", "run": "run"}
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


def _ev_dict(v) -> dict:
    return {"status": v.status, "score": v.score, "score_min": v.score_min, "score_max": v.score_max,
            "auto_score": v.auto_score, "score_source": v.score_source,
            "vetoes": v.vetoes, "reason": v.invalid_reason or v.error or "", "parts": v.parts}


# ---------------------------------------------------------------- 应用

def create_app(db: str | Path, policy_path: str | Path | None = None,
               operators_path: str | Path | None = None) -> FastAPI:
    app = FastAPI(title="Agent_Eval 复核服务", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    store = Store(db)
    policy, policy_sha = load_policy(policy_path or default_policy_path())
    cfg = load_operator_config(operators_path or default_operators_path())
    default_chain = cfg.chain_sha
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["md"] = md_filter
    templates.env.filters["vcls"] = tone_filter
    templates.env.filters["status_label"] = status_label
    templates.env.filters["kind_label"] = lambda k: KIND_LABELS.get(k, k)
    templates.env.filters["f1"] = lambda x: "" if x is None else f"{x:.1f}".rstrip("0").rstrip(".")
    templates.env.globals["pass_mark"] = policy.score.pass_mark
    templates.env.globals["compare_delta"] = policy.score.compare_delta
    _nav_cache: dict = {}

    def chain_of(request: Request) -> str:
        c = request.query_params.get("chain")
        return c if c and any(ch["chain_sha"] == c for ch in store.chains()) else default_chain

    def rows(request: Request | str, with_records: bool = False) -> list[dict]:
        chain = chain_of(request) if isinstance(request, Request) else request
        return store.answer_view_rows(chain, policy_sha, with_records=with_records)

    def nav_count(chain: str) -> int:
        """顶栏「复核」计数 = 结论待定的对话数（轻量 SQL，不做条目级扫描）。"""
        if _nav_cache.get("chain") != chain:
            n = store.conn.execute(
                "SELECT COUNT(*) FROM conclusions c JOIN answers a ON a.answer_id = c.answer_id"
                " JOIN questions q ON q.qkey = a.qkey"
                " WHERE q.current_judge_fp = c.judge_fp AND c.chain_sha = ? AND c.policy_sha = ?"
                " AND c.status IN ('undecided', 'error')",
                (chain, policy_sha)).fetchone()[0]
            _nav_cache.update({"chain": chain, "n": n})
        return _nav_cache["n"]

    def needs_rescore() -> bool:
        """当前口径下尚无会话结论（改 policy 后未 rescore）：页面提示执行命令。"""
        return store.conn.execute("SELECT 1 FROM conclusions WHERE policy_sha=? LIMIT 1",
                                  (policy_sha,)).fetchone() is None

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
        return templates.TemplateResponse(request=request, name=name, context={
            "db_path": str(store.path), "chain_sha": chain, "chain_short": chain[:8],
            "chains": store.chains(), "nav_count": nav_count(chain),
            "needs_rescore": needs_rescore(), "stale_runs": stale_runs(),
            "policy": policy, "pass_mark": policy.score.pass_mark,
            "tab_labels": TAB_LABELS, "axis_labels": AXIS_LABELS, **ctx})

    def configs_of(rs: list[dict]) -> list[dict]:
        """被测配置分组（配置 = profile/model/skills_mode/skills_base/evalset_version）。"""
        by: dict[tuple, dict] = {}
        for r in rs:
            key = views.config_value(r)
            c = by.setdefault(key, {"key": "|".join(key), "label": views.config_label(r), "n": 0})
            c["n"] += 1
        return sorted(by.values(), key=lambda c: -c["n"])

    # ---------------- 用例页（首页）
    @app.get("/")
    async def cases(request: Request, config: str = "", dim: str = "", topic: str = "",
                    granularity: str = "", sort: str = "pending", flag_only: str = "", page: int = 0):
        all_rows = rows(request)
        configs = configs_of(all_rows)
        sel = next((c for c in configs if c["key"] == config), configs[0] if configs else None)
        if sel is None:
            return render(request, "cases.html", configs=[], summary=None, table=[], total=0,
                          granularities=[], dims=[], topics=[], f_dim=dim, f_topic=topic,
                          f_granularity=granularity, sort=sort, flag_only=flag_only,
                          config_key="", page=0, pages=1)
        sel_key = tuple(sel["key"].split("|"))
        cfg_rows = [r for r in all_rows if views.config_value(r) == sel_key]
        summary = views.score_summary(cfg_rows, policy)
        known_dims = {r["dim"] for r in all_rows if r["dim"]}
        comp = views.composite(summary["dims"], policy, known_dims=known_dims)
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
        for r in cfg_rows:
            c = conv_counts.setdefault(r["case_id"], {"n": 0, "undecided": 0})
            c["n"] += 1
            c["undecided"] += r["c_status"] == "undecided"
        health = views.case_health(cfg_rows, summary["cases"])
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
        return render(request, "cases.html", configs=configs, config_key=sel["key"], summary=summary,
                      composite=comp, known_dims=sorted(known_dims),
                      table=table[page * size:(page + 1) * size], total=total, page=page,
                      pages=max(1, (total + size - 1) // size),
                      dims=sorted({q["dim"] for q in qs if q["dim"]}),
                      topics=sorted({q["topic"] for q in qs if q["topic"]}),
                      granularities=sorted({q["granularity"] for q in qs if q["granularity"]}),
                      f_dim=dim, f_topic=topic, f_granularity=granularity, sort=sort,
                      flag_only=flag_only)

    # ---------------- 用例详情
    @app.get("/case/{case_id}")
    async def case_detail(request: Request, case_id: str, axis: str = "profile", judge_fp: str = "",
                          variant: str = "", pair: Annotated[list[str], Query()] = []):
        members = [q for q in store.questions() if (q["case_id"] or q["uid"]) == case_id]
        if not members:
            return render(request, "error.html", message=f"未知用例：{case_id}")
        members.sort(key=lambda q: (q["variant_index"] or 0, q["uid"]))
        seed = members[0]
        cur = store.current_spec(seed["qkey"])
        fp = judge_fp or (cur[0] if cur else None)
        spec = store.spec_of(seed["qkey"], fp) if fp else {}
        history = [dict(r) for r in store.conn.execute(
            "SELECT judge_fp, evalset_version, imported_at FROM specs WHERE qkey=? ORDER BY imported_at",
            (seed["qkey"],))]
        all_rows = rows(request)
        case_rows = [r for r in all_rows if r["case_id"] == case_id]
        axis_values = sorted({views.attr_value(r, axis) for r in case_rows})
        convs = []
        for r in sorted(case_rows, key=lambda r: (r["answer_id"])):
            concl = store.conclusion(r["answer_id"], r["current_judge_fp"] or "", chain_of(request), policy_sha) or {}
            chips = []
            for er in store.effective_rows(r["answer_id"], r["current_judge_fp"] or "", chain_of(request)):
                chips.append({"id": er["row"]["item_id"], "kind": er["row"]["kind"],
                              "value": er["eff"].value, "score": er["eff"].score,
                              "source": er["eff"].source})
            convs.append({"r": r, "concl": concl, "chips": chips,
                          "axis_label": views.attr_value(r, axis),
                          "pending_items": sum(1 for c in chips if c["value"] is None)})
        health = views.case_health(case_rows, views.case_scores(case_rows, policy)).get(case_id, [])
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
                "judge_fp": q["current_judge_fp"] or "",
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
        if variant:
            convs = [c for c in convs if c["r"]["uid"] == variant]
        def status_of(c):
            return c["concl"].get("status") or c["r"]["c_status"]
        conv_stats = {
            "all": len(convs),
            "undecided": sum(1 for c in convs if status_of(c) == "undecided"),
            "failed": sum(1 for c in convs if status_of(c) in ("invalid", "error")),
        }
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
        return render(request, "case.html", case_id=case_id, members=members, seed=seed, spec=spec,
                      judge_fp=fp or "", history=history, convs=convs, axis=axis,
                      axis_values=axis_values, tags=json.loads(seed["tags_json"] or "[]"),
                      injections=injections, health=health, variant_cards=variant_cards,
                      variant_stats=variant_stats, conv_stats=conv_stats,
                      variant=variant, pair=pair, pair_data=pair_data, pair_rows=pair_rows,
                      headline=policy.headline(seed["dim"] or "?"),
                      summary_dim=(spec.get("dim") or "?"))

    # ---------------- 复核队列
    @app.get("/review")
    async def review_queue(request: Request, tab: str = "undecided", run_id: str = "",
                           dim: str = "", op: str = "", spot: int = 10, page: int = 0):
        if tab not in TAB_LABELS:
            tab = "undecided"
        spot = spot if spot in (10, 20, 100) else 10
        chain = chain_of(request)
        convs, meta = store.review_conversations(chain, policy, policy_sha, tab=tab,
                                                 run_id=run_id or None, dim=dim or None,
                                                 spot_ratio=spot / 100, operator=op or None, page=page)
        operators = [r[0] for r in store.conn.execute(
            "SELECT DISTINCT operator FROM verdicts WHERE chain_sha=? AND operator IS NOT NULL"
            " ORDER BY operator", (chain,))]
        keep = {"run_id": run_id, "dim": dim, "op": op, **({"spot": spot} if tab == "spot" else {})}
        return render(request, "review_queue.html", tab=tab, convs=convs, meta=meta,
                      run_ids=[r["run_id"] for r in store.runs()],
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
            v = store.conversation_view(answer_id, chain, policy, policy_sha)
        except StoreError as e:
            return render(request, "error.html", message=str(e))
        if tab not in TAB_LABELS:
            tab = "undecided"
        spot = spot if spot in (10, 20, 100) else 10
        convs, _ = store.review_conversations(chain, policy, policy_sha, tab=tab, size=10 ** 6,
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
        m = policy.score.map
        return render(request, "review.html", v=v, tab=tab, pos=pos, total_q=total,
                      prev_url=prev_url, next_url=next_url, score_map=m,
                      extra_qs=extra,
                      scales={k: list(v_) for k, v_ in SCALES.items()},
                      pass_mark=policy.score.pass_mark,
                      detail=(v["conclusion"] or {}).get("detail") or {})

    # ---------------- 结果对比
    @app.get("/compare")
    async def compare(request: Request, axis: str = "skills_base", base: str = "", target: str = "",
                      any: str = "", fix: str = "", factors: str = "", run: str = ""):
        rep = error = shap = None
        rs = rows(request, with_records=True)
        run_order = {r["run_id"]: i for i, r in enumerate(store.runs())}
        axis_values = sorted({views.attr_value(r, axis) for r in rs})
        anys = tuple(a.strip() for a in (any or "").split(",") if a.strip())
        fixes: dict[str, str] = {}
        for part in (fix or "").split(","):
            k, sep, v = part.partition("=")
            if sep and k.strip() and v.strip():
                fixes[k.strip()] = v.strip()
        if base and target:
            try:
                rep = views.compare_scores(rs, axis=axis, base=base, target=target, policy=policy,
                                           fixes=fixes, anys=anys, run_order=run_order)
            except ValueError as e:
                error = str(e)
        if factors and not error:
            try:
                facs = [f.strip() for f in factors.split(",") if f.strip()]
                cells, shared = views.shapley_cells(rs, facs, policy)
                shap = views.shapley(cells, facs)
            except ValueError as e:
                error = str(e)
        return render(request, "compare.html", axis=axis, axis_values=axis_values,
                      base=base, target=target, any=any, fix=fix, factors=factors,
                      rep=rep, error=error, shap=shap, attrs=[a for a in views.ATTRS if a != "skills_mode"],
                      axis_labels=AXIS_LABELS)

    # ---------------- 判定质量
    @app.get("/judging")
    async def judging(request: Request):
        chain = chain_of(request)
        agree = views.judge_agreement(store, chain, policy)
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
                      chains=store.chains(), policy_sha=policy_sha[:8])

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
                                         [e.model_dump() for e in body.entries], body.total, fp, chain, policy)
        except StoreError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return _ev_dict(v)

    @app.post("/api/reviews")
    async def review_submit(body: ReviewBatchIn):
        try:
            chain, fp = _batch_target(body)
            c = store.submit_reviews(body.answer_id, [e.model_dump() for e in body.entries], body.total,
                                     body.reviewer or "?", fp, chain, policy, policy_sha)
        except StoreError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        _nav_cache.clear()
        tab = "undecided"
        convs, _ = store.review_conversations(chain, policy, policy_sha, tab=tab, size=10 ** 6,
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
                                                 fp, default_chain, policy, policy_sha)
        except (StoreError, KeyError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        _nav_cache.clear()
        return {"episode_status": status, "reason": reason}

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
               policy_path: str | None = None, operators_path: str | None = None):
    import uvicorn

    app = create_app(db, policy_path, operators_path)
    print(f"复核服务：http://{host}:{port}（评测库 {db}，仅本机）")
    uvicorn.run(app, host=host, port=port, log_level="warning")
