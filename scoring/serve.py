"""复核服务（方案 §9）：展示会话内容与判定、人工评价、结果汇总与统计。

只负责结果处理阶段；编译、执行、算子判定仍是命令行流程。文件是唯一数据源，
SQLite 只做索引（可随时删除重建），回答正文按需从文件读取。并发复核不考虑。
仅本文件依赖 FastAPI / Jinja2 / markdown / bleach（可选安装，见 requirements.txt）。
"""
from __future__ import annotations

import csv
import html
import io
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from pydantic import BaseModel

from . import cli, views
from .chain import episode_id_of, load_verdicts
from .effective import Review, append_review, effective_final, load_reviews, voided_reviews
from .items import SCALES, build_context, extract_items
from .policy import default_policy_path, load_policy, session_conclusion
from .views import RunBundle

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


# ---------------------------------------------------------------- 渲染过滤器（净化后渲染）

try:
    import bleach
    import markdown as md_lib

    _ALLOWED_TAGS = ["p", "br", "strong", "em", "code", "pre", "ul", "ol", "li", "h1", "h2", "h3", "h4",
                     "table", "thead", "tbody", "tr", "th", "td", "blockquote", "hr", "a"]

    def md_filter(text) -> Markup:
        """markdown 渲染，渲染前净化 HTML（§9.3）。"""
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


# ---------------------------------------------------------------- run 装载（带 mtime 缓存）

@dataclass
class RunInfo:
    run_id: str
    dir: Path
    manifest: dict | None

    @property
    def evalset_version(self) -> str:
        if self.manifest:
            ev = (self.manifest.get("snapshot") or {}).get("evalset")
            if isinstance(ev, dict):
                return ev.get("file") or "?"
            if ev:
                return str(ev)
        return "?"

    @property
    def profile_id(self) -> str:
        if self.manifest:
            p = self.manifest.get("profile") or {}
            return p.get("id") or "?"
        return "?"


class RunStore:
    """扫描 root 下的 run；results.jsonl 按 mtime 缓存，其余产物按需读取。"""

    def __init__(self, root: Path, policy_path: Path | None = None):
        self.root = root
        self.policy_path = policy_path or default_policy_path()
        self.policy, self.policy_sha = load_policy(self.policy_path)
        self._runs: dict[str, RunInfo] = {}
        self._records: dict[str, tuple[float, list[dict]]] = {}

    def scan(self) -> list[RunInfo]:
        self._runs = {}
        for results in sorted(self.root.rglob("results.jsonl")):
            d = results.parent
            if ".work" in d.parts:
                continue
            manifest = None
            mj = d / "run.json"
            if mj.exists():
                try:
                    manifest = json.loads(mj.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    manifest = None
            run_id = (manifest or {}).get("run_id") or d.name
            if run_id in self._runs:                       # 目录名兜底防撞
                run_id = f"{run_id}@{d.name}"
            self._runs[run_id] = RunInfo(run_id=run_id, dir=d, manifest=manifest)
        return list(self._runs.values())

    @property
    def runs(self) -> dict[str, RunInfo]:
        return self._runs

    def info(self, run_id: str) -> RunInfo | None:
        return self._runs.get(run_id)

    def records(self, run_id: str) -> list[dict]:
        info = self.info(run_id)
        if info is None:
            return []
        p = info.dir / "results.jsonl"
        mtime = p.stat().st_mtime
        cached = self._records.get(run_id)
        if cached and cached[0] == mtime:
            return cached[1]
        records = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
        self._records[run_id] = (mtime, records)
        return records

    def record_by_episode(self, run_id: str) -> dict[str, dict]:
        return {episode_id_of(r): r for r in self.records(run_id)}

    def rows(self, run_id: str) -> list[dict]:
        info = self.info(run_id)
        return load_verdicts(info.dir) if info else []

    def reviews(self, run_id: str) -> list[Review]:
        info = self.info(run_id)
        return load_reviews(info.dir) if info else []

    def bundle(self, run_id: str) -> RunBundle | None:
        info = self.info(run_id)
        if info is None:
            return None
        records = self.records(run_id)
        try:
            rows = self.rows(run_id)
        except FileNotFoundError:                       # 尚未跑 score 的 run：只登记，不判定
            return RunBundle(run_id=run_id, run_dir=str(info.dir), records=records,
                             manifest=info.manifest, rows=[], reviews=[], verdicts=[], scored=False)
        reviews = self.reviews(run_id)
        routing = cli.routing_of(info.manifest)
        return RunBundle(run_id=run_id, run_dir=str(info.dir), records=records, manifest=info.manifest,
                         rows=rows, reviews=reviews,
                         verdicts=cli.conclusions(records, rows, reviews, self.policy, routing))


def effective_rows(rows: list[dict], reviews: list[Review]) -> list[dict]:
    """(row, 有效结论) 对：所有展示与统计的唯一取数口（§6）。"""
    by_ep: dict[str, list[Review]] = {}
    for r in reviews:
        by_ep.setdefault(r.episode_id, []).append(r)
    out = []
    for row in rows:
        eff = effective_final(row, by_ep.get(row["episode_id"], []), row["answer_sha"])
        out.append({"row": row, "eff": eff})
    return out


# ---------------------------------------------------------------- SQLite 索引（可随时删除重建）

QUEUE_FILTERS = ("undecided", "fail", "mismatch")


class Index:
    def __init__(self, db_path: Path | None = None):
        # check_same_thread=False：服务单用户、请求串行，TestClient/线程池下共用一个连接
        import threading
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(str(db_path) if db_path else ":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._create()

    def _create(self):
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY, dir TEXT, evalset TEXT, profile TEXT);
        CREATE TABLE IF NOT EXISTS episodes(run_id TEXT, episode_id TEXT, uid TEXT, mode TEXT, trial INT,
            dim TEXT, status TEXT, reason TEXT, ask_user INT, PRIMARY KEY(run_id, episode_id));
        CREATE TABLE IF NOT EXISTS items(run_id TEXT, episode_id TEXT, uid TEXT, item_id TEXT, kind TEXT,
            turn INT, answer_sha TEXT, status TEXT, machine_value TEXT, machine_source TEXT,
            eff_value TEXT, eff_source TEXT, reason TEXT,
            PRIMARY KEY(run_id, episode_id, item_id));
        """)

    def rebuild(self, store: RunStore) -> int:
        with self.lock:
            return self._rebuild(store)

    def _rebuild(self, store: RunStore) -> int:
        for t in ("runs", "episodes", "items"):
            self.conn.execute(f"DELETE FROM {t}")
        n = 0
        for run_id, info in store.runs.items():
            b = store.bundle(run_id)
            if b is None:
                continue
            self.conn.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?)",
                              (run_id, str(info.dir), info.evalset_version, info.profile_id))
            if not b.scored:
                continue
            rec_by_ep = store.record_by_episode(run_id)
            routing = cli.routing_of(info.manifest)
            eff_rows = effective_rows(b.rows, b.reviews)
            by_ep: dict[str, list[dict]] = {}
            for er in eff_rows:
                er2 = er["row"]
                by_ep.setdefault(er2["episode_id"], []).append(er)
            for record, v in zip(b.records, b.verdicts):
                ep = episode_id_of(record)
                self.conn.execute("INSERT OR REPLACE INTO episodes VALUES (?,?,?,?,?,?,?,?,?)",
                                  (run_id, ep, v.uid, v.mode, v.trial, v.dim, v.status,
                                   v.invalid_reason or v.error or "", v.ask_user))
                for er in by_ep.get(ep, []):
                    row, eff = er["row"], er["eff"]
                    self.conn.execute("INSERT OR REPLACE INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                      (run_id, ep, row["uid"], row["item"]["id"], row["item"]["kind"],
                                       row["item"]["turn"], row["answer_sha"], row["status"],
                                       row["machine"].get("value"), row["machine"].get("source"),
                                       eff.value, eff.source, eff.reason))
                n += 1
        self.conn.commit()
        return n

    def update_episode(self, run_id: str, record: dict, verdict, eff_rows: list[dict]) -> None:
        with self.lock:
            ep = episode_id_of(record)
        self.conn.execute("INSERT OR REPLACE INTO episodes VALUES (?,?,?,?,?,?,?,?,?)",
                          (run_id, ep, verdict.uid, verdict.mode, verdict.trial, verdict.dim,
                           verdict.status, verdict.invalid_reason or verdict.error or "", verdict.ask_user))
        for er in eff_rows:
            row, eff = er["row"], er["eff"]
            self.conn.execute("INSERT OR REPLACE INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              (run_id, ep, row["uid"], row["item"]["id"], row["item"]["kind"],
                               row["item"]["turn"], row["answer_sha"], row["status"],
                               row["machine"].get("value"), row["machine"].get("source"),
                               eff.value, eff.source, eff.reason))
        self.conn.commit()

    def queue(self, filter_name: str, run_id: str | None, page: int, size: int = 20) -> tuple[list, int]:
        cond = {"undecided": "i.status = 'undecided' OR (i.eff_value IS NULL AND e.status != 'error')",
                "fail": "e.status = 'fail'",
                "mismatch": "i.eff_source = 'human' AND i.machine_value IS NOT NULL AND i.machine_value != i.eff_value"}[filter_name]
        where = f"WHERE {cond}" + (f" AND i.run_id = ?" if run_id else "")
        args = [run_id] if run_id else []
        total = self.conn.execute(f"SELECT COUNT(*) c FROM items i JOIN episodes e USING(run_id, episode_id) {where}", args).fetchone()["c"]
        rows = self.conn.execute(
            f"SELECT i.* FROM items i JOIN episodes e USING(run_id, episode_id) {where} "
            f"ORDER BY i.run_id, i.episode_id, i.item_id LIMIT ? OFFSET ?", (*args, size, page * size)).fetchall()
        return rows, total

    def run_ids(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM runs ORDER BY evalset, profile, run_id").fetchall()


# ---------------------------------------------------------------- 应用

class ReviewIn(BaseModel):
    run_id: str
    episode_id: str
    item_id: str
    value: str
    reason: str = ""
    reviewer: str = ""


def create_app(root: str | Path, policy_path: str | Path | None = None) -> FastAPI:
    app = FastAPI(title="Agent_Eval 复核服务", docs_url=None, redoc_url=None)
    store = RunStore(Path(root), Path(policy_path) if policy_path else None)
    index = Index(Path(root) / ".review-index.db")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["md"] = md_filter
    templates.env.filters["highlight"] = highlight_filter

    def value_class(v):
        return {"对": "ok", "满足": "ok", "成立": "ok", "拒答": "ok", "未采信": "ok",
                "错": "bad", "违反": "bad", "不成立": "bad", "采信": "bad", "未拒答": "bad"}.get(v or "", "mid")

    templates.env.filters["vcls"] = value_class

    def render(request: Request, name: str, **ctx):
        return templates.TemplateResponse(request=request, name=name,
                                          context={"runs": [dict(r) for r in index.run_ids()], **ctx})

    def rebuild():
        store.scan()
        return index.rebuild(store)

    rebuild()

    # ---------------- 总览（§9.3 出口 1 + 临时标注）
    @app.get("/")
    async def overview(request: Request):
        per_run = []
        for r in index.run_ids():
            b = store.bundle(r["run_id"])
            if b is None:
                continue
            ability = views.ability_view(b.verdicts, store.policy)
            counts: dict[str, int] = {}
            for v in b.verdicts:
                counts[v.status] = counts.get(v.status, 0) + 1
            provisional = any(c.get("undecided") for c in ability.values())
            per_run.append({"info": r, "ability": ability, "counts": counts,
                            "provisional": provisional, "scored": b.scored})
        return render(request, "overview.html", per_run=per_run)

    # ---------------- run 明细（题目列表）
    @app.get("/run/{run_id}")
    async def run_detail(request: Request, run_id: str):
        b = store.bundle(run_id)
        if b is None:
            return render(request, "error.html", message=f"未知 run：{run_id}")
        episodes = []
        for record, v in zip(b.records, b.verdicts):
            episodes.append({"v": v, "episode_id": episode_id_of(record),
                             "target": v.target_loaded, "other": v.other_loaded})
        return render(request, "run.html", run_id=run_id, episodes=episodes,
                      policy_partial=store.policy.partial_as, policy_veto=store.policy.veto_on_forbidden,
                      policy_undecided=store.policy.undecided)

    # ---------------- 复核队列（§9.3）
    @app.get("/queue")
    async def queue(request: Request, run_id: str = "", filter_name: str = "undecided", page: int = 0):
        if filter_name not in QUEUE_FILTERS:
            filter_name = "undecided"
        rows, total = index.queue(filter_name, run_id or None, page)
        # 补充展示材料：条目文本与回答摘录取自文件（按需读取）
        rec_cache: dict[str, dict[str, dict]] = {}
        items = []
        for r in rows:
            rid = r["run_id"]
            if rid not in rec_cache:
                rec_cache[rid] = store.record_by_episode(rid)
            record = rec_cache[rid].get(r["episode_id"])
            text, answer, keywords = "", "", []
            if record is not None:
                ctx = build_context(record)
                for item in extract_items(ctx):
                    if item.id == r["item_id"]:
                        text = item.text or ""
                        keywords = item.attrs.get("keywords") or []
                        turn = r["turn"]
                        if turn is not None and 1 <= turn <= len(ctx.outputs):
                            answer = ctx.outputs[turn - 1]
                        elif ctx.outputs:
                            answer = ctx.outputs[-1]
                        break
            items.append({"r": dict(r), "text": text, "answer": answer[:400], "keywords": keywords,
                          "scale": SCALES.get(r["kind"], ())})
        pages = max(1, (total + 19) // 20)
        return render(request, "queue.html", items=items, filter_name=filter_name, q_run=run_id,
                      page=page, pages=pages, total=total)

    # ---------------- 人工评价写回（§9.4）
    @app.post("/api/review")
    async def review(body: ReviewIn):
        info = store.info(body.run_id)
        if info is None:
            return JSONResponse({"error": "未知 run"}, status_code=400)
        row = next((r for r in store.rows(body.run_id)
                    if r["episode_id"] == body.episode_id and r["item"]["id"] == body.item_id), None)
        if row is None:
            return JSONResponse({"error": "条目不存在"}, status_code=400)
        if body.value not in SCALES.get(row["item"]["kind"], ()):
            return JSONResponse({"error": f"取值不合法：{body.value}"}, status_code=400)
        append_review(info.dir, Review(run_id=body.run_id, episode_id=body.episode_id,
                                       item_id=body.item_id, value=body.value,
                                       answer_sha=row["answer_sha"], reason=body.reason,
                                       reviewer=body.reviewer or "?"))
        # 只重算该会话：effective_final + 口径（§9.4 第 3 步）
        record = store.record_by_episode(body.run_id).get(body.episode_id)
        if record is None:
            return JSONResponse({"error": "会话不存在"}, status_code=400)
        rows_ep = [r for r in store.rows(body.run_id) if r["episode_id"] == body.episode_id]
        reviews_ep = [r for r in store.reviews(body.run_id) if r.episode_id == body.episode_id]
        eff_rows = effective_rows(rows_ep, reviews_ep)
        ctx = build_context(record)
        eff = {er["row"]["item"]["id"]: er["eff"] for er in eff_rows}
        verdict = session_conclusion(record, ctx, extract_items(ctx), eff, store.policy,
                                     cli.routing_of(info.manifest))
        index.update_episode(body.run_id, record, verdict, eff_rows)
        return {"episode_status": verdict.status, "reason": verdict.invalid_reason or ""}

    # ---------------- 口径重算（§9.5）：只读逐条判定，不调用算子
    @app.post("/api/rescore/{run_id}")
    async def rescore(run_id: str):
        info = store.info(run_id)
        if info is None:
            return JSONResponse({"error": "未知 run"}, status_code=400)
        try:
            out = cli.rescore_run(info.dir, policy_path=str(store.policy_path))
        except FileNotFoundError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        rebuild()
        return {"scoring": str(out)}

    @app.post("/api/rescan")
    async def rescan():
        n = rebuild()
        return {"runs": len(store.runs), "episodes": n}

    # ---------------- 题目视图（§9.3）：期望固定上方，轮次为行、各 run 为列
    @app.get("/case/{run_id}/{uid}")
    async def case_view(request: Request, run_id: str, uid: str):
        anchor = store.info(run_id)
        if anchor is None:
            return render(request, "error.html", message=f"未知 run：{run_id}")
        anchor_bundle = store.bundle(run_id)
        anchor_record = next((r for r in anchor_bundle.records if r.get("uid") == uid), None)
        if anchor_record is None:
            return render(request, "error.html", message=f"run {run_id} 中没有 uid {uid}")
        anchor_ep = episode_id_of(anchor_record)

        # 期望区（来自锚 run 的编译行），含逐轮用户消息（问题/对话）
        case = anchor_record.get("case") or {}
        expectations = {
            "dim": case.get("dim"), "answerable": case.get("answerable", True),
            "dialog": case.get("turns") or [],
            "preconditions": case.get("preconditions") or [],
            "facts": case.get("facts") or [], "path": case.get("path") or [],
            "reference": case.get("reference_answer") or "",
        }

        # 各 run 同 uid 的会话列
        columns = []
        for rid in store.runs:
            b = store.bundle(rid)
            if b is None:
                continue
            for record in b.records:
                if record.get("uid") != uid:
                    continue
                ep = episode_id_of(record)
                rows_ep = [r for r in b.rows if r["episode_id"] == ep]
                reviews_ep = [r for r in b.reviews if r.episode_id == ep]
                eff_rows = effective_rows(rows_ep, reviews_ep)
                v = next((x for x, rec2 in zip(b.verdicts, b.records) if episode_id_of(rec2) == ep), None)
                by_turn: dict[int, list[dict]] = {}
                n_turns = len(record.get("turns") or [])
                for er in eff_rows:
                    row, eff = er["row"], er["eff"]
                    t = row["item"]["turn"]
                    by_turn.setdefault(t if t is not None and 1 <= t <= n_turns else max(n_turns, 1), []).append({
                        "id": row["item"]["id"], "kind": row["item"]["kind"],
                        "value": eff.value, "source": eff.source, "reason": eff.reason,
                        "machine": row["machine"].get("value"), "undecided": row["status"] == "undecided",
                        "keywords": (row["machine"] or {}).get("reason", "").startswith("命中"),
                    })
                    # 键名用 chips：dict 的 items 键会被 Jinja 解析成 .items() 方法
                turns = []
                for t in record.get("turns") or []:
                    obs = t.get("observations") or {}
                    turns.append({"index": t.get("index"), "input": t.get("input", ""),
                                  "output": t.get("output", ""), "status": t.get("status"),
                                  "skills": [s["name"] for s in obs.get("skills", []) if s.get("ok")],
                                  "ask_user": (obs.get("ask_user") or {}).get("count", 0),
                                  "chips": by_turn.get(t.get("index") or 1, [])})
                columns.append({"run_id": rid, "mode": record.get("mode"), "trial": record.get("trial"),
                                "episode_id": ep, "status": v.status if v else "?",
                                "reason": (v.invalid_reason or v.error) if v else "",
                                "turns": turns, "is_anchor": rid == run_id})
        n_turns = max((len(c["turns"]) for c in columns), default=0)
        return render(request, "case.html", run_id=run_id, uid=uid, anchor_ep=anchor_ep,
                      exp=expectations, columns=columns, n_turns=n_turns,
                      scales=SCALES)

    # ---------------- 多 run 对比（§9.5）
    @app.get("/compare")
    async def compare(request: Request, a: str = "", b: str = "", intended: str = "model"):
        rep = None
        error = None
        if a and b:
            try:
                ba, bb = store.bundle(a), store.bundle(b)
                if ba is None or bb is None:
                    error = "请选择两个存在的 run"
                else:
                    rep = views.compare_runs(ba, bb, intended=intended)
            except ValueError as e:
                error = str(e)
        return render(request, "compare.html", a=a, b=b, intended=intended, rep=rep, error=error)

    # ---------------- 导出（§9.5）
    @app.get("/export/{run_id}/episodes.csv")
    async def export_episodes(run_id: str):
        b = store.bundle(run_id)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["run_id", "episode_id", "uid", "mode", "trial", "dim", "status", "reason",
                    "facts", "expected_hit", "forbidden_adopted", "path", "ask_user"])
        if b is not None:
            for record, v in zip(b.records, b.verdicts):
                w.writerow([run_id, episode_id_of(record), v.uid, v.mode, v.trial, v.dim, v.status,
                            v.invalid_reason or v.error or "", json.dumps(v.facts, ensure_ascii=False),
                            json.dumps(v.expected_hit, ensure_ascii=False),
                            json.dumps(v.forbidden_adopted, ensure_ascii=False), v.path, v.ask_user])
        return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                                 headers={"Content-Disposition": f"attachment; filename={run_id}-episodes.csv"})

    @app.get("/export/{run_id}/items.csv")
    async def export_items(run_id: str):
        b = store.bundle(run_id)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["episode_id", "uid", "kind", "item_id", "turn", "status",
                    "machine_value", "machine_source", "eff_value", "eff_source", "reason"])
        if b is not None:
            for er in effective_rows(b.rows, b.reviews):
                row, eff = er["row"], er["eff"]
                w.writerow([row["episode_id"], row["uid"], row["item"]["kind"], row["item"]["id"],
                            row["item"]["turn"], row["status"], row["machine"].get("value"),
                            row["machine"].get("source"), eff.value, eff.source, eff.reason])
        return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                                 headers={"Content-Disposition": f"attachment; filename={run_id}-items.csv"})

    @app.get("/voided/{run_id}")
    async def voided(request: Request, run_id: str):
        info = store.info(run_id)
        if info is None:
            return render(request, "error.html", message=f"未知 run：{run_id}")
        rows = store.rows(run_id)
        return render(request, "voided.html", run_id=run_id,
                      items=voided_reviews(rows, store.reviews(run_id)))

    return app


def run_server(root: str, host: str = "127.0.0.1", port: int = 8765, policy_path: str | None = None):
    import uvicorn

    app = create_app(root, policy_path)
    print(f"复核服务：http://{host}:{port}（root={root}）")
    uvicorn.run(app, host=host, port=port, log_level="warning")
