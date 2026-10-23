"""评测库存储（评测结果管理改造方案 §6/§9）：SQLite 单文件，判定与人工数据的
唯一事实来源；原始运行产物仍是不可变文件。

三层存储：原始层（run 目录文件，不可变）→ 评测库（本模块）→ 导出层（jsonl/CSV，
可随时重新生成）。本模块是唯一访问评测库的模块，其他模块不直接拼 SQL；
只用标准 SQL（INTEGER PRIMARY KEY 自增除外），以便日后迁移 PostgreSQL。

会话结论（conclusions）为派生表，可随时重算（rescore 不调用任何算子）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from .items import (SCALES, build_context, episode_error, extract_items,
                    input_fp_of, judge_fp_of, qkey_of, variant_leak_suspects)
from .operators import Verdict
from .policy import EpisodeVerdict, Policy, session_conclusion

SCHEMA_VERSION = 2

# 建表语句（§6.2）：只用标准 SQL 类型与语法
_SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE runs(
  run_id TEXT PRIMARY KEY, run_dir TEXT, evalset_version TEXT, profile_id TEXT,
  snapshot_json TEXT, imported_at TEXT);

CREATE TABLE questions(
  qkey TEXT PRIMARY KEY,
  uid TEXT, case_id TEXT, variant_index INTEGER, rewrite TEXT,
  dim TEXT, topic TEXT, tags_json TEXT, input_fp TEXT,
  current_judge_fp TEXT, active INTEGER, first_seen TEXT);

CREATE TABLE specs(
  qkey TEXT, judge_fp TEXT, spec_json TEXT, evalset_version TEXT, imported_at TEXT,
  PRIMARY KEY(qkey, judge_fp));

CREATE TABLE answers(
  answer_id TEXT PRIMARY KEY,
  qkey TEXT, run_id TEXT, episode_id TEXT, judge_fp_at_run TEXT,
  profile_id TEXT, models_json TEXT, mode TEXT, trial INTEGER,
  skills_mode TEXT, skills_base TEXT, evalset_version TEXT,
  run_status TEXT, record_json TEXT);

CREATE TABLE verdicts(
  answer_id TEXT, judge_fp TEXT, chain_sha TEXT, item_id TEXT,
  kind TEXT, turn INTEGER, item_sha TEXT, answer_sha TEXT,
  value TEXT, source TEXT, operator TEXT, reason TEXT, trace_json TEXT,
  status TEXT, judged_at TEXT,
  PRIMARY KEY(answer_id, judge_fp, chain_sha, item_id));

CREATE TABLE reviews(
  review_id INTEGER PRIMARY KEY,
  answer_id TEXT, item_id TEXT, item_sha TEXT, answer_sha TEXT,
  value TEXT, reason TEXT, reviewer TEXT, at TEXT);

CREATE TABLE verdict_cache(
  cache_key TEXT PRIMARY KEY, value TEXT, source TEXT, operator TEXT, reason TEXT);

CREATE TABLE conclusions(
  answer_id TEXT, judge_fp TEXT, chain_sha TEXT, policy_sha TEXT,
  status TEXT, reason TEXT, detail_json TEXT,
  PRIMARY KEY(answer_id, judge_fp, chain_sha, policy_sha));

CREATE INDEX idx_answers_qkey ON answers(qkey);
CREATE INDEX idx_answers_run ON answers(run_id);
CREATE INDEX idx_verdicts_lookup ON verdicts(answer_id, judge_fp, chain_sha);
CREATE INDEX idx_reviews_answer ON reviews(answer_id, item_id);
CREATE INDEX idx_conclusions_key ON conclusions(judge_fp, chain_sha, policy_sha);
"""

_MIGRATION_2_SQL = """
    CREATE TABLE IF NOT EXISTS spec_versions(
      qkey TEXT, judge_fp TEXT, evalset_version TEXT, imported_at TEXT,
      PRIMARY KEY(qkey, judge_fp, evalset_version));
    INSERT OR IGNORE INTO spec_versions SELECT qkey, judge_fp, evalset_version, imported_at FROM specs;
    ALTER TABLE answers ADD COLUMN injection_found_gt1 INTEGER;
    ALTER TABLE answers ADD COLUMN variant_leak_json TEXT;
    ALTER TABLE runs ADD COLUMN started_at TEXT;
    ALTER TABLE reviews ADD COLUMN legacy_key TEXT;
    CREATE UNIQUE INDEX IF NOT EXISTS idx_reviews_legacy ON reviews(legacy_key);
    """


def _migrate_chain_sha(conn) -> None:
    """R4 迁移：把"当前默认 operators.yaml 原文哈希"名下的判定/结论行改写到规范化
    chain_sha 名下（同一份配置内容一一映射）。旧配置内容产生的其他旧哈希保留原值——
    它们不再匹配任何当前链，会按待重判处理，历史不丢。"""
    try:
        from .chain import default_operators_path, load_operator_config
        cfg = load_operator_config(default_operators_path())
        if cfg.config_sha and cfg.config_sha != cfg.chain_sha:
            for table in ("verdicts", "conclusions"):
                conn.execute(f"UPDATE {table} SET chain_sha=? WHERE chain_sha=?",
                             (cfg.chain_sha, cfg.config_sha))
    except Exception:                                   # 默认配置不可用时跳过（无行可改写）
        pass


# 每个版本一组步骤：字符串按 SQL 脚本执行，可调用对象收到连接
_MIGRATIONS: dict[int, list] = {1: [_SCHEMA], 2: [_MIGRATION_2_SQL, _migrate_chain_sha]}


class StoreError(Exception):
    """评测库操作错误。"""


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def answer_id_of(run_id: str, episode_id: str) -> str:
    return f"{run_id}/{episode_id}"


def evalset_version_of(manifest: dict | None) -> str:
    """环境快照 evalset → 版本串（优先 version，其次 file，最后整体串化）。"""
    ev = ((manifest or {}).get("snapshot") or {}).get("evalset")
    if isinstance(ev, dict):
        return str(ev.get("version") or ev.get("file") or "?")
    return str(ev or "?")


class Store:
    """面向业务的评测库接口（§9）：入库答案、写入判定、追加改判、查询与聚合。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False：命令行写入与复核服务读取共用一个库文件（WAL），
        # 服务请求串行；写操作经 self.lock 互斥。RLock：组合操作（如 import_evalset）
        # 内部会调用持同一把锁的子操作
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def close(self) -> None:
        self.conn.close()

    # ---------------------------------------------------------------- 基础

    def _migrate(self) -> None:
        with self.lock, self.conn:
            row = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
            current = 0
            if row:
                v = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
                current = int(v["value"]) if v else 0
            for version in sorted(_MIGRATIONS):
                if version > current:
                    for step in _MIGRATIONS[version]:
                        if callable(step):
                            step(self.conn)
                        else:
                            self.conn.executescript(step)
                    self.conn.execute("INSERT OR REPLACE INTO meta VALUES('schema_version', ?)", (str(version,)))

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self.lock, self.conn:
            return self.conn.execute(sql, args)

    # ---------------------------------------------------------------- 题目与期望（C6/C7）

    def upsert_question(self, case: dict, active: bool | None = None,
                        current_judge_fp: str | None = None,
                        current_if_absent: str | None = None) -> str:
        """按编译行登记题目。active/current_judge_fp 缺省不改动（specs import 时统一刷新）；
        current_if_absent：该题尚无当前期望时设置之并置活跃（ingest 路径，整改清单 R5）。"""
        qkey = qkey_of(case)
        variant = case.get("variant") or {}
        tags = [t.get("tag") if isinstance(t, dict) else t for t in (case.get("tags") or [])]
        sets = ["uid=?", "case_id=?", "variant_index=?", "rewrite=?", "dim=?", "topic=?",
                "tags_json=?", "input_fp=?"]
        args: list = [case.get("uid", "?"), case.get("case_id") or case.get("id"),
                      case.get("variant_index", 0), variant.get("rewrite"),
                      case.get("dim"), case.get("topic"),
                      json.dumps(tags, ensure_ascii=False), input_fp_of(case)]
        if active is not None:
            sets.append("active=?")
            args.append(1 if active else 0)
        if current_judge_fp is not None:
            sets.append("current_judge_fp=?")
            args.append(current_judge_fp)
        with self.lock, self.conn:
            exists = self.conn.execute("SELECT current_judge_fp FROM questions WHERE qkey=?", (qkey,)).fetchone()
            if exists and current_if_absent is not None and current_judge_fp is None and not exists["current_judge_fp"]:
                sets.append("current_judge_fp=?")
                args.append(current_if_absent)
                if active is None:
                    sets.append("active=?")
                    args.append(1)
            args.append(qkey)
            if exists:
                self.conn.execute(f"UPDATE questions SET {', '.join(sets)} WHERE qkey=?", tuple(args))
            else:
                fp = current_judge_fp if current_judge_fp is not None else current_if_absent
                self.conn.execute(
                    "INSERT INTO questions(qkey, uid, case_id, variant_index, rewrite, dim, topic, tags_json,"
                    " input_fp, current_judge_fp, active, first_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (qkey, args[0], args[1], args[2], args[3], args[4], args[5], args[6], args[7],
                     fp, 1 if (active is None and fp) or active else 0, now()))
        return qkey

    def register_spec(self, qkey: str, judge_fp: str, spec_json: dict, evalset_version: str) -> None:
        """登记一份期望（同一 qkey × judge_fp 只存一份，首次登记的版本标签不再改写），
        并写版本映射 spec_versions（整改清单 R1：期望按指纹寻址，版本与期望多对多）。"""
        ts = now()
        with self.lock, self.conn:
            self.conn.execute("INSERT OR IGNORE INTO specs VALUES(?,?,?,?,?)",
                              (qkey, judge_fp, json.dumps(spec_json, ensure_ascii=False), evalset_version, ts))
            self.conn.execute("INSERT OR IGNORE INTO spec_versions VALUES(?,?,?,?)",
                              (qkey, judge_fp, evalset_version, ts))

    def import_evalset(self, rows: list[dict], version: str) -> dict:
        """specs import：登记全部期望并成为当前期望；未出现的题保留旧期望、标记非活跃。"""
        qkeys: dict[str, str] = {}
        with self.lock, self.conn:
            for case in rows:
                qkey = qkey_of(case)
                fp = judge_fp_of(case)
                self.upsert_question(case)
                self.register_spec(qkey, fp, case, version)
                qkeys[qkey] = fp
            self.conn.execute("UPDATE questions SET active=0")
            for qkey, fp in qkeys.items():
                self.conn.execute("UPDATE questions SET active=1, current_judge_fp=? WHERE qkey=?", (fp, qkey))
        return {"questions": len(qkeys), "version": version}

    def set_current(self, version: str) -> int:
        """specs set-current：把某评测集版本的期望设为当前期望。版本不存在时抛
        StoreError 且不做任何改动（整改清单 R1）。"""
        with self.lock, self.conn:
            rows = self.conn.execute(
                "SELECT qkey, judge_fp FROM spec_versions WHERE evalset_version=?", (version,)).fetchall()
            if not rows:
                raise StoreError(f"未知评测集版本：{version}")
            self.conn.execute("UPDATE questions SET active=0")
            for r in rows:
                self.conn.execute("UPDATE questions SET active=1, current_judge_fp=? WHERE qkey=?",
                                  (r["judge_fp"], r["qkey"]))
            return len(rows)

    def question(self, qkey: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM questions WHERE qkey=?", (qkey,)).fetchone()
        return dict(row) if row else None

    def questions(self, active: bool | None = None) -> list[dict]:
        sql = "SELECT * FROM questions"
        args: tuple = ()
        if active is not None:
            sql += " WHERE active=?"
            args = (1 if active else 0,)
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY uid", args)]

    def current_spec(self, qkey: str) -> tuple[str, dict] | None:
        """(judge_fp, 编译行) of 当前期望。"""
        q = self.question(qkey)
        if not q or not q["current_judge_fp"]:
            return None
        spec = self.spec_of(qkey, q["current_judge_fp"])
        if spec is None:
            return None
        return q["current_judge_fp"], spec

    def spec_of(self, qkey: str, judge_fp: str) -> dict | None:
        row = self.conn.execute("SELECT spec_json FROM specs WHERE qkey=? AND judge_fp=?",
                                (qkey, judge_fp)).fetchone()
        return json.loads(row["spec_json"]) if row else None

    # ---------------------------------------------------------------- run 与答案（C8）

    def register_run(self, run_id: str, run_dir: str | Path, manifest: dict | None) -> None:
        snap = (manifest or {}).get("snapshot") or {}
        profile_id = ((manifest or {}).get("profile") or {}).get("id") or "?"
        snapshot_json = json.dumps(manifest or {}, ensure_ascii=False)
        started_at = (manifest or {}).get("started_at") or now()
        self._exec(
            "INSERT INTO runs(run_id, run_dir, evalset_version, profile_id, snapshot_json, imported_at, started_at)"
            " VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(run_id) DO UPDATE SET run_dir=excluded.run_dir,"
            " evalset_version=excluded.evalset_version, profile_id=excluded.profile_id,"
            " snapshot_json=excluded.snapshot_json, started_at=excluded.started_at",
            (run_id, str(run_dir), evalset_version_of(manifest), profile_id, snapshot_json, now(), started_at))

    def ingest_answer(self, record: dict, run_id: str, manifest: dict | None) -> str:
        """入库一条答案：完整 EpisodeRecord 随行保存（run 目录移走后仍可重判），
        并登记执行时所用的期望。answer_id = run_id/episode_id。
        题目尚无当前期望时以执行时期望设为当前期望并置活跃（R5：空库首次 score 即可判定）。
        注入 found>1 与变体泄露嫌疑在入库时预计算存列（R7：质量页免逐答案解析）。"""
        case = record.get("case") or {}
        qkey = qkey_of(case)
        fp = judge_fp_of(case)
        self.upsert_question(case, current_if_absent=fp)
        self.register_spec(qkey, fp, case, evalset_version_of(manifest))
        snap = (manifest or {}).get("snapshot") or {}
        models = snap.get("models_observed")
        episode_id = record.get("episode_id") or f"{record.get('uid', '?')}-{record.get('mode', '?')}-t{record.get('trial', 0)}"
        answer_id = answer_id_of(run_id, episode_id)
        found_gt1 = any((a or {}).get("found", 1) > 1 for a in record.get("injections_applied") or [])
        leak_json = json.dumps(variant_leak_suspects([record]), ensure_ascii=False)
        self._exec(
            "INSERT OR REPLACE INTO answers(answer_id, qkey, run_id, episode_id, judge_fp_at_run,"
            " profile_id, models_json, mode, trial, skills_mode, skills_base, evalset_version,"
            " run_status, record_json, injection_found_gt1, variant_leak_json)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (answer_id, qkey, run_id, episode_id, fp,
             ((manifest or {}).get("profile") or {}).get("id") or "?",
             json.dumps(models, ensure_ascii=False) if models is not None else None,
             record.get("mode", "?"), record.get("trial", 0),
             snap.get("skills_mode"), snap.get("skills_base"), evalset_version_of(manifest),
             record.get("status", "?"), json.dumps(record, ensure_ascii=False),
             1 if found_gt1 else 0, leak_json))
        return answer_id

    def answer(self, answer_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM answers WHERE answer_id=?", (answer_id,)).fetchone()
        return dict(row) if row else None

    def answers(self, qkey: str | None = None, run_id: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM answers", []
        if qkey:
            sql += " WHERE qkey=?"
            args.append(qkey)
        if run_id:
            sql += (" AND" if args else " WHERE") + " run_id=?"
            args.append(run_id)
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY answer_id", tuple(args))]

    def runs(self) -> list[dict]:
        """按 run 开始时间排序（R8：导入顺序会把旧 run 提前，绊线方向会判反）。"""
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM runs ORDER BY started_at, run_id")]

    def run(self, run_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    # ---------------------------------------------------------------- 判定入库（C8/C10）

    def write_verdicts(self, answer_id: str, judge_fp: str, chain_sha: str, rows: list[dict],
                       judged_at: str | None = None) -> int:
        """按 (答案, 期望, 链配置) 写入一批机器判定行（整会话覆盖写，可重复执行）。"""
        ts = judged_at or now()
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM verdicts WHERE answer_id=? AND judge_fp=? AND chain_sha=?",
                              (answer_id, judge_fp, chain_sha))
            for r in rows:
                self.conn.execute(
                    "INSERT OR REPLACE INTO verdicts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (answer_id, judge_fp, chain_sha, r["item"]["id"], r["item"]["kind"], r["item"]["turn"],
                     r.get("item_sha"), r["answer_sha"], r["machine"].get("value"), r["machine"].get("source", ""),
                     r["machine"].get("operator", ""), r["machine"].get("reason", ""),
                     json.dumps(r.get("trace") or [], ensure_ascii=False), r["status"], ts))
        return len(rows)

    def verdicts(self, answer_id: str, judge_fp: str, chain_sha: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM verdicts WHERE answer_id=? AND judge_fp=? AND chain_sha=? ORDER BY item_id",
            (answer_id, judge_fp, chain_sha))]

    def has_verdicts(self, answer_id: str, judge_fp: str, chain_sha: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM verdicts WHERE answer_id=? AND judge_fp=? AND chain_sha=? LIMIT 1",
            (answer_id, judge_fp, chain_sha)).fetchone()
        return row is not None

    def pending_rejudge(self, chain_sha: str, qkey: str | None = None, run_id: str | None = None) -> list[dict]:
        """缺"当前期望 + 当前链配置"判定的答案。无条目的会话（运行错误）不需要判定，
        直接写结论，不算待重判。"""
        out: list[dict] = []
        for a in self.answers(qkey=qkey, run_id=run_id):
            cur = self.current_spec(a["qkey"])
            if cur is None:
                continue                       # 题目没有当前期望（未 specs import）：不判
            fp, spec = cur
            if self.has_verdicts(a["answer_id"], fp, chain_sha):
                continue
            record = json.loads(a["record_json"])
            ctx = build_context(record, spec)
            if episode_error(record, ctx) is None:
                out.append({"answer_id": a["answer_id"], "qkey": a["qkey"], "judge_fp": fp,
                            "run_id": a["run_id"], "episode_id": a["episode_id"]})
        return out

    # ---------------------------------------------------------------- 判定缓存（§6.2）

    def cache_get(self, key: str) -> Verdict | None:
        row = self.conn.execute("SELECT * FROM verdict_cache WHERE cache_key=?", (key,)).fetchone()
        if not row:
            return None
        return Verdict(row["value"], row["source"] or "judge", row["operator"] or "", reason=row["reason"] or "")

    def cache_put(self, key: str, v: Verdict) -> None:
        self._exec("INSERT OR REPLACE INTO verdict_cache VALUES(?,?,?,?,?)",
                   (key, v.value, v.source, v.operator, v.reason))

    def cache_bulk(self, rows: list[dict]) -> int:
        with self.lock, self.conn:
            for d in rows:
                self.conn.execute("INSERT OR REPLACE INTO verdict_cache VALUES(?,?,?,?,?)",
                                  (d["key"], d.get("value"), d.get("source", "judge"),
                                   d.get("operator", ""), d.get("reason", "")))
        return len(rows)

    # ---------------------------------------------------------------- 人工改判（C9）

    def add_review(self, answer_id: str, item_id: str, value: str, reason: str, reviewer: str,
                   item_sha: str | None, answer_sha: str) -> int:
        with self.lock, self.conn:
            cur = self.conn.execute(
                "INSERT INTO reviews(answer_id, item_id, item_sha, answer_sha, value, reason, reviewer, at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (answer_id, item_id, item_sha, answer_sha, value, reason, reviewer, now()))
            return int(cur.lastrowid)

    def add_legacy_review(self, answer_id: str, item_id: str, value: str, reason: str, reviewer: str,
                          item_sha: str | None, answer_sha: str, legacy_key: str) -> bool:
        """import-legacy 的幂等写入（R9）：legacy_key 唯一，重复导入不再追加。"""
        with self.lock, self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO reviews(answer_id, item_id, item_sha, answer_sha, value, reason,"
                " reviewer, at, legacy_key) VALUES(?,?,?,?,?,?,?,?,?)",
                (answer_id, item_id, item_sha, answer_sha, value, reason, reviewer, now(), legacy_key))
            return cur.rowcount > 0

    def reviews_for(self, answer_id: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM reviews WHERE answer_id=? ORDER BY review_id", (answer_id,))]

    def submit_review(self, answer_id: str, item_id: str, value: str, reason: str, reviewer: str,
                      judge_fp: str, chain_sha: str, policy: Policy, policy_sha: str) -> tuple[str, str]:
        """改判提交（§8）：校验 → 追加改判 → 重算该答案的结论行。
        校验失败抛 StoreError，不落任何数据（先校验后写入，改造方案 §3.4 C3）。"""
        if self.answer(answer_id) is None:
            raise StoreError("答案不存在")
        vrow = self.conn.execute(
            "SELECT * FROM verdicts WHERE answer_id=? AND judge_fp=? AND chain_sha=? AND item_id=?",
            (answer_id, judge_fp, chain_sha, item_id)).fetchone()
        if vrow is None:
            raise StoreError("条目不存在（该期望 + 链配置下没有判定行，先 rejudge）")
        if value not in SCALES.get(vrow["kind"], ()):
            raise StoreError(f"取值不合法：{value}")
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO reviews(answer_id, item_id, item_sha, answer_sha, value, reason, reviewer, at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (answer_id, item_id, vrow["item_sha"], vrow["answer_sha"], value, reason, reviewer, now()))
        status = self.recompute_conclusion(answer_id, judge_fp, chain_sha, policy, policy_sha)
        c = self.conclusion(answer_id, judge_fp, chain_sha, policy_sha)
        return status, (c or {}).get("reason") or ""

    # ---------------------------------------------------------------- 有效结论与物化（C9/C10）

    @staticmethod
    def effective(vrow: dict, reviews: list[dict]) -> Verdict:
        """有效结论（§6.3）＝最后一条有效人工改判 ?? 机器结论。
        改判有效：answer_sha 与该条目当前回答一致、item_sha 与该期望下条目指纹一致、
        取值在条目类型 scale 内。改判不绑定期望或链配置。"""
        valid = [r for r in reviews
                 if r["item_id"] == vrow["item_id"]
                 and r["answer_sha"] == vrow["answer_sha"]
                 and r["item_sha"] is not None and r["item_sha"] == vrow["item_sha"]
                 and r["value"] in SCALES.get(vrow["kind"], ())]
        if valid:
            r = valid[-1]
            return Verdict(r["value"], "human", "review", reason=r["reason"] or "")
        return Verdict(vrow["value"], vrow["source"] or "", vrow["operator"] or "", reason=vrow["reason"] or "")

    def effective_rows(self, answer_id: str, judge_fp: str, chain_sha: str) -> list[dict]:
        """(verdict 行, 有效结论) 对：所有展示与统计的唯一取数口。"""
        reviews = self.reviews_for(answer_id)
        out = []
        for vrow in self.verdicts(answer_id, judge_fp, chain_sha):
            out.append({"row": vrow, "eff": self.effective(vrow, reviews)})
        return out

    def voided_reviews(self, answer_id: str) -> list[dict]:
        """作废改判清单（§6）：相对当前期望（缺省回退执行时期望），条目不存在、
        answer_sha / item_sha 不匹配或取值不合法的改判。"""
        a = self.answer(answer_id)
        if a is None:
            return []
        cur = self.current_spec(a["qkey"])
        fp = cur[0] if cur else a["judge_fp_at_run"]
        refs: dict[str, dict] = {}
        for chain in self._chain_shas_of(answer_id, fp):
            for vrow in self.verdicts(answer_id, fp, chain):
                refs[vrow["item_id"]] = vrow
        out = []
        for r in self.reviews_for(answer_id):
            ref = refs.get(r["item_id"])
            if ref is None:
                why = "条目不存在"
            elif r["answer_sha"] != ref["answer_sha"]:
                why = "answer_sha 不匹配（回答已变化）"
            elif r["item_sha"] is None or r["item_sha"] != ref["item_sha"]:
                why = "item_sha 不匹配（期望已修订）"
            elif r["value"] not in SCALES.get(ref["kind"], ()):
                why = f"取值不合法：{r['value']!r}"
            else:
                continue
            out.append({"review": r, "reason": why})
        return out

    def _chain_shas_of(self, answer_id: str, judge_fp: str) -> list[str]:
        return [r["chain_sha"] for r in self.conn.execute(
            "SELECT DISTINCT chain_sha FROM verdicts WHERE answer_id=? AND judge_fp=?",
            (answer_id, judge_fp))]

    # ---------------------------------------------------------------- 复核队列取数（R2）

    def undecided_items(self, chain_sha: str, run_id: str | None = None,
                        page: int = 0, size: int = 20) -> tuple[list[dict], int]:
        """未决队列（§6.3：按有效结论取数）：机器结论为空或异常、且没有效人工改判
        （answer_sha + item_sha 匹配、取值在 scale 内）的参与条目。已被短路的条目
        （前提判不成立/路径违反后的事实、forced 会话的路径、期望事实）不进队列。"""
        sql = """SELECT v.answer_id, v.item_id, v.kind, v.turn, v.value AS machine_value,
                 v.source AS machine_source, v.reason AS machine_reason, v.status,
                 q.uid, a.run_id, a.episode_id, a.mode, a.trial, a.qkey, q.current_judge_fp,
                 (SELECT r.value FROM reviews r WHERE r.answer_id = v.answer_id AND r.item_id = v.item_id
                    AND r.answer_sha = v.answer_sha AND r.item_sha = v.item_sha
                    ORDER BY r.review_id DESC LIMIT 1) AS review_value
                 FROM verdicts v JOIN answers a USING(answer_id) JOIN questions q ON a.qkey = q.qkey
                 WHERE q.current_judge_fp = v.judge_fp AND v.chain_sha = ?
                   AND v.kind != 'expected' AND (v.status != 'ok' OR v.value IS NULL)"""
        args: list = [chain_sha]
        if run_id:
            sql += " AND a.run_id = ?"
            args.append(run_id)
        sql += " ORDER BY v.answer_id, v.item_id"
        cands = [dict(r) for r in self.conn.execute(sql, tuple(args))]
        # 已有有效人工改判（取值在 scale 内）→ 不再需要复核
        cands = [c for c in cands
                 if c["review_value"] is None or c["review_value"] not in SCALES.get(c["kind"], ())]
        # 参与门与 policy.session_conclusion 的分类门一致；批量为候选答案取判定行与期望
        by_answer: dict[str, list[dict]] = {}
        for c in cands:
            by_answer.setdefault(c["answer_id"], []).append(c)
        out: list[dict] = []
        if cands:
            aids = list(by_answer)
            qmarks_a = ",".join("?" * len(aids))
            vr_by_answer: dict[str, list[dict]] = {}
            for v in self.conn.execute(
                    f"SELECT answer_id, judge_fp, item_id, kind, value FROM verdicts"
                    f" WHERE answer_id IN ({qmarks_a}) AND chain_sha = ?", (*aids, chain_sha)):
                vr_by_answer.setdefault(v["answer_id"], []).append(dict(v))
            qkeys = {c["qkey"] for c in cands}
            qmarks_q = ",".join("?" * len(qkeys))
            specs = {(r["qkey"], r["judge_fp"]): json.loads(r["spec_json"])
                     for r in self.conn.execute(
                         f"SELECT qkey, judge_fp, spec_json FROM specs WHERE qkey IN ({qmarks_q})", tuple(qkeys))}
            for a_id, items_ in by_answer.items():
                first = items_[0]
                forced = first["mode"] == "forced"
                fp = first["current_judge_fp"]
                answerable = (specs.get((first["qkey"], fp)) or {}).get("answerable", True) is not False
                rows_ = [v for v in vr_by_answer.get(a_id, []) if v["judge_fp"] == fp]
                precond_fail = any(v["kind"] == "precond" and v["value"] == "不成立" for v in rows_)
                path_violate = (not forced and any(v["kind"] == "path" and v["value"] == "违反" for v in rows_))
                facts_live = not (precond_fail or path_violate or not answerable)
                for c in items_:
                    if c["kind"] == "precond":
                        out.append(c)
                    elif c["kind"] == "path" and not forced:
                        out.append(c)
                    elif c["kind"] == "refusal":
                        out.append(c)
                    elif c["kind"] in ("required", "forbidden") and facts_live:
                        out.append(c)
        out.sort(key=lambda c: (c["answer_id"], c["item_id"]))
        total = len(out)
        page = max(0, min(page, (max(total, 1) - 1) // size))
        return out[page * size:(page + 1) * size], total

    # ---------------------------------------------------------------- 质量聚合（R7）

    def human_machine_compare(self, chain_sha: str) -> tuple[list[dict], int, int]:
        """人工与机器不一致清单 + 规则一致率（一条 SQL 聚合，R7）：
        只看有人工改判的条目；每条目取最后一条有效改判（sha 匹配 + 取值合法）。
        返回 (mismatch 列表, rule_agree, rule_total)。"""
        sql = """SELECT v.answer_id, v.item_id, v.kind, v.value AS machine_value, v.source AS machine_source,
                 q.uid, r.value AS review_value, r.review_id
                 FROM verdicts v
                 JOIN answers a ON a.answer_id = v.answer_id
                 JOIN questions q ON q.qkey = a.qkey AND q.current_judge_fp = v.judge_fp
                 JOIN reviews r ON r.answer_id = v.answer_id AND r.item_id = v.item_id
                    AND r.answer_sha = v.answer_sha AND r.item_sha = v.item_sha
                 WHERE v.chain_sha = ?"""
        best: dict[tuple, dict] = {}
        for row in self.conn.execute(sql, (chain_sha,)):
            d = dict(row)
            if d["review_value"] not in SCALES.get(d["kind"], ()):
                continue
            key = (d["answer_id"], d["item_id"])
            if key not in best or d["review_id"] > best[key]["review_id"]:
                best[key] = d
        mismatch, agree, total = [], 0, 0
        for d in best.values():
            if d["machine_source"] == "rule" and d["machine_value"] is not None:
                total += 1
                agree += d["machine_value"] == d["review_value"]
            if d["machine_value"] != d["review_value"]:
                mismatch.append({"answer_id": d["answer_id"], "uid": d["uid"], "kind": d["kind"],
                                 "item": d["item_id"], "machine": d["machine_value"],
                                 "human": d["review_value"]})
        return mismatch, agree, total

    def recompute_conclusion(self, answer_id: str, judge_fp: str, chain_sha: str,
                             policy: Policy, policy_sha: str) -> str:
        """重算并物化一个 (答案, 期望, 链配置, 口径) 的会话结论（不调用任何算子）。"""
        a = self.answer(answer_id)
        if a is None:
            raise StoreError(f"答案不存在：{answer_id}")
        spec_row = self.conn.execute("SELECT spec_json FROM specs WHERE qkey=? AND judge_fp=?",
                                     (a["qkey"], judge_fp)).fetchone()
        if spec_row is None:
            raise StoreError(f"期望不存在：qkey={a['qkey']} judge_fp={judge_fp}")
        record = json.loads(a["record_json"])
        spec = json.loads(spec_row["spec_json"])
        ctx = build_context(record, spec)
        manifest = json.loads((self.run(a["run_id"]) or {}).get("snapshot_json") or "{}")
        routing = ((manifest.get("profile") or {}).get("content") or {}).get("routing")
        err = episode_error(record, ctx)
        if err is not None:
            v = EpisodeVerdict(uid=record.get("uid", "?"), mode=record.get("mode", "?"),
                               trial=record.get("trial", 0), dim=spec.get("dim", "?"),
                               status="error", error=err)
        else:
            items = extract_items(ctx)
            reviews = self.reviews_for(answer_id)
            eff: dict[str, Verdict] = {}
            statuses: dict[str, str] = {}
            for item in items:
                vrow = next((r for r in self.verdicts(answer_id, judge_fp, chain_sha)
                             if r["item_id"] == item.id), None)
                if vrow is None:
                    eff[item.id] = Verdict(None, "", "")
                    statuses[item.id] = "undecided"
                else:
                    eff[item.id] = self.effective(vrow, reviews)
                    # 人工改判覆盖后该条目已决（R10：异常按条目 status 判，单一判据）
                    statuses[item.id] = "ok" if eff[item.id].source == "human" else vrow["status"]
            v = session_conclusion(record, ctx, items, eff, policy, routing, statuses=statuses)
        self._exec("INSERT OR REPLACE INTO conclusions VALUES(?,?,?,?,?,?,?)",
                   (answer_id, judge_fp, chain_sha, policy_sha, v.status,
                    v.invalid_reason or v.error or "", json.dumps(v.as_dict(), ensure_ascii=False)))
        return v.status

    def rescore(self, policy: Policy, policy_sha: str, current_chain_sha: str | None = None) -> int:
        """只重算会话结论表（§10 rescore）：对已有判定的全部 (答案, 期望, 链) 组合
        与当前期望下的无条目会话（运行错误）重算，不调用任何算子。
        无条目会话的结论记在 current_chain_sha 名下（与 answer_view_rows 的取数键一致）。"""
        keys = [(r["answer_id"], r["judge_fp"], r["chain_sha"]) for r in self.conn.execute(
            "SELECT DISTINCT answer_id, judge_fp, chain_sha FROM verdicts")]
        n = 0
        for answer_id, fp, chain in keys:
            self.recompute_conclusion(answer_id, fp, chain, policy, policy_sha)
            n += 1
        if current_chain_sha is not None:
            for a in self.answers():                      # 运行错误的答案没有判定行，直接物化结论
                cur = self.current_spec(a["qkey"])
                if cur is None:
                    continue
                fp = cur[0]
                if self.has_verdicts(a["answer_id"], fp, current_chain_sha):
                    continue
                record = json.loads(a["record_json"])
                spec = cur[1]
                if episode_error(record, build_context(record, spec)) is None:
                    continue                              # 有条目但未判定：待重判，不产出结论
                self.recompute_conclusion(a["answer_id"], fp, current_chain_sha, policy, policy_sha)
                n += 1
        return n

    def conclusion(self, answer_id: str, judge_fp: str, chain_sha: str, policy_sha: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM conclusions WHERE answer_id=? AND judge_fp=? AND chain_sha=? AND policy_sha=?",
            (answer_id, judge_fp, chain_sha, policy_sha)).fetchone()
        if not row:
            return None
        out = dict(row)
        out["detail"] = json.loads(row["detail_json"] or "{}")
        return out

    def answer_view_rows(self, chain_sha: str, policy_sha: str, with_records: bool = False) -> list[dict]:
        """answers × questions × 当前期望下的结论（统计与比较的取数口；
        缺判定的 c_status 为 NULL → 待重判）。record_json 较大，仅在
        with_records=True（锚点绊线）时随行带出；质量预计算列常带（R7）。"""
        record_col = "a.record_json, " if with_records else ""
        sql = f"""
        SELECT a.answer_id, a.qkey, a.run_id, a.episode_id, a.judge_fp_at_run, a.profile_id,
               a.models_json, a.mode, a.trial, a.skills_mode, a.skills_base, a.evalset_version,
               a.run_status, {record_col}a.injection_found_gt1, a.variant_leak_json,
               q.uid, q.case_id, q.variant_index, q.rewrite,
               q.dim, q.topic, q.current_judge_fp, q.active,
               c.status AS c_status, c.reason AS c_reason, c.detail_json
        FROM answers a JOIN questions q ON a.qkey = q.qkey
        LEFT JOIN conclusions c ON c.answer_id = a.answer_id AND c.judge_fp = q.current_judge_fp
             AND c.chain_sha = ? AND c.policy_sha = ?
        ORDER BY a.answer_id"""
        return [dict(r) for r in self.conn.execute(sql, (chain_sha, policy_sha))]

    # ---------------------------------------------------------------- 备份

    def backup(self, target: str | Path) -> Path:
        """SQLite 原生备份接口复制评测库（§10 backup）。"""
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        dst = sqlite3.connect(str(target))
        with self.lock:
            self.conn.backup(dst)
        dst.close()
        return target
