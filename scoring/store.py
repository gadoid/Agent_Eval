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

from collections import defaultdict

from .items import (SCALES, build_context, episode_error, extract_items, input_fp_of,
                    judge_fp_of, qkey_of, variant_leak_suspects, TOTAL_ITEM, conversation_sha,
                    weight_of)
from .operators import Verdict
from .policy import EpisodeVerdict, Policy, session_conclusion

SCHEMA_VERSION = 4

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


def _migrate_recompute_qkeys(conn) -> None:
    """v3 迁移（v1 库升级不完整的修复）：v2 只加了列/表，没有重算已有数据的派生值。

    - R11 把 topic 加进输入指纹后，旧答案留在不含 topic 的旧 qkey 下、新答案进新
      qkey，同一题被拆成两个。用 answers.record_json 与 specs.spec_json 的编译行
      重算输入指纹与 qkey，改写 questions、specs、spec_versions、answers 四张表；
      新旧 qkey 并存时按题合并（保留目标行、取更早 first_seen、当前期望缺者补之）。
    - 同一旧 qkey 可能分叉出多个新 qkey（uid 与输入未变、仅 topic 改过：不同 run
      或不同评测集版本各记各的 topic）。期望按各自 spec_json 重算归属，不走
      old→new 单值映射；当前期望指针跟着期望所在的新 qkey 走，其余分叉题只继承
      first_seen、不带当前期望。spec_versions 无内容可重算，跟随同 (qkey, judge_fp)
      的期望行；期望行缺失且分叉不明时删除（成员表可由 specs import 重建）。
    - injection_found_gt1 / variant_leak_json 只在入库时计算，这里按 record_json 回填。
    """
    candidates: dict[str, set[str]] = {}
    for row in conn.execute("SELECT answer_id, qkey, record_json FROM answers").fetchall():
        record = json.loads(row["record_json"])
        new_q = qkey_of(record.get("case") or {})
        found = 1 if any((a or {}).get("found", 1) > 1 for a in record.get("injections_applied") or []) else 0
        leak = json.dumps(variant_leak_suspects([record]), ensure_ascii=False)
        conn.execute("UPDATE answers SET qkey=?, injection_found_gt1=?, variant_leak_json=? WHERE answer_id=?",
                     (new_q, found, leak, row["answer_id"]))
        if row["qkey"] != new_q:
            candidates.setdefault(row["qkey"], set()).add(new_q)
    spec_target: dict[tuple[str, str], str] = {}       # (旧qkey, judge_fp) → 新qkey
    for row in conn.execute("SELECT qkey, judge_fp, spec_json FROM specs").fetchall():
        new_q = qkey_of(json.loads(row["spec_json"]))  # 从未跑过的题也在此列
        if new_q != row["qkey"]:
            spec_target[(row["qkey"], row["judge_fp"])] = new_q
            candidates.setdefault(row["qkey"], set()).add(new_q)
    if not candidates:
        return
    with conn:
        for (old_q, fp), new_q in spec_target.items():
            conn.execute("INSERT OR IGNORE INTO specs(qkey, judge_fp, spec_json, evalset_version, imported_at)"
                         " SELECT ?, judge_fp, spec_json, evalset_version, imported_at"
                         " FROM specs WHERE qkey=? AND judge_fp=?", (new_q, old_q, fp))
            conn.execute("DELETE FROM specs WHERE qkey=? AND judge_fp=?", (old_q, fp))
        for row in conn.execute("SELECT qkey, judge_fp FROM spec_versions").fetchall():
            new_q = spec_target.get((row["qkey"], row["judge_fp"]))
            if new_q is None and row["qkey"] in candidates:
                cands = candidates[row["qkey"]]
                new_q = next(iter(cands)) if len(cands) == 1 else False
            if new_q:
                conn.execute("INSERT OR IGNORE INTO spec_versions(qkey, judge_fp, evalset_version, imported_at)"
                             " SELECT ?, judge_fp, evalset_version, imported_at"
                             " FROM spec_versions WHERE qkey=? AND judge_fp=?", (new_q, row["qkey"], row["judge_fp"]))
                conn.execute("DELETE FROM spec_versions WHERE qkey=? AND judge_fp=?",
                             (row["qkey"], row["judge_fp"]))
            elif new_q is False:
                conn.execute("DELETE FROM spec_versions WHERE qkey=? AND judge_fp=?",
                             (row["qkey"], row["judge_fp"]))
        for old_q, cands in candidates.items():
            src = conn.execute("SELECT current_judge_fp, active, first_seen FROM questions WHERE qkey=?",
                               (old_q,)).fetchone()
            if src is None:
                continue
            primary = spec_target.get((old_q, src["current_judge_fp"])) \
                if src["current_judge_fp"] else None
            if primary is None and len(cands) == 1:
                primary = next(iter(cands))
            for new_q in cands:
                is_primary = new_q == primary
                tgt = conn.execute("SELECT current_judge_fp, active, first_seen FROM questions WHERE qkey=?",
                                   (new_q,)).fetchone()
                if tgt is None:
                    conn.execute(
                        "INSERT INTO questions(qkey, uid, case_id, variant_index, rewrite, dim, topic,"
                        " tags_json, input_fp, current_judge_fp, active, first_seen)"
                        " SELECT ?, uid, case_id, variant_index, rewrite, dim, topic, tags_json,"
                        " ?, ?, ?, first_seen FROM questions WHERE qkey=?",
                        (new_q, new_q.split("|", 1)[1],
                         src["current_judge_fp"] if is_primary else None,
                         src["active"] if is_primary else 0, old_q))
                else:
                    first_seen = min(x for x in (src["first_seen"], tgt["first_seen"]) if x) \
                        if any((src["first_seen"], tgt["first_seen"])) else None
                    if is_primary:
                        current = tgt["current_judge_fp"] or src["current_judge_fp"]
                        active = 1 if current else (tgt["active"] or src["active"])
                        conn.execute("UPDATE questions SET first_seen=?, current_judge_fp=?, active=? WHERE qkey=?",
                                     (first_seen, current, active, new_q))
                    else:
                        conn.execute("UPDATE questions SET first_seen=? WHERE qkey=?", (first_seen, new_q))
            conn.execute("DELETE FROM questions WHERE qkey=?", (old_q,))


def _migrate_backfill_started_at(conn) -> None:
    """v3 迁移：旧 run 的 started_at 已在 snapshot_json（run.json 全量）里，直接回填；
    确实没有的回退导入时间（导入时间在重导时保留，不刷新）。"""
    for row in conn.execute("SELECT run_id, snapshot_json, imported_at FROM runs WHERE started_at IS NULL").fetchall():
        started = None
        try:
            started = (json.loads(row["snapshot_json"] or "{}") or {}).get("started_at")
        except (json.JSONDecodeError, TypeError):
            pass
        conn.execute("UPDATE runs SET started_at=? WHERE run_id=?",
                     (started or row["imported_at"], row["run_id"]))


_MIGRATIONS[3] = [_migrate_recompute_qkeys, _migrate_backfill_started_at]

# v4（复核服务改造方案 §5.2，百分制）：只加列与表，不改写已有数据；会话分在下一次 rescore
# 时按新口径（新 policy_sha）物化，旧口径的结论行保留。
_MIGRATION_4_SQL = """
    ALTER TABLE verdicts ADD COLUMN score REAL;
    ALTER TABLE verdict_cache ADD COLUMN score REAL;
    ALTER TABLE reviews ADD COLUMN score REAL;
    ALTER TABLE conclusions ADD COLUMN score REAL;
    ALTER TABLE conclusions ADD COLUMN score_min REAL;
    ALTER TABLE conclusions ADD COLUMN score_max REAL;
    ALTER TABLE conclusions ADD COLUMN auto_score REAL;
    ALTER TABLE conclusions ADD COLUMN score_source TEXT;
    ALTER TABLE answers ADD COLUMN injection_observed_json TEXT;
    CREATE TABLE IF NOT EXISTS chains(
      chain_sha TEXT PRIMARY KEY, name TEXT, operators_json TEXT, judge_models_json TEXT, created_at TEXT);
    INSERT OR IGNORE INTO chains(chain_sha) SELECT DISTINCT chain_sha FROM verdicts;
    """
_MIGRATIONS[4] = [_MIGRATION_4_SQL]


class StoreError(Exception):
    """评测库操作错误。"""


def _valid_score(x) -> bool:
    return not isinstance(x, bool) and isinstance(x, (int, float)) and 0 <= x <= 100


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _spot_picked(answer_id: str, ratio: float) -> bool:
    """抽检抽样（按 answer_id 独立哈希）：某段被人工判过退出候选后，其余段的
    选中与否不变（顺序消费共享随机流的抽样会整体漂移）。"""
    import random
    return random.Random(f"spot:{answer_id}").random() < ratio


def answer_id_of(run_id: str, episode_id: str) -> str:
    return f"{run_id}/{episode_id}"


def evalset_version_of(manifest: dict | None) -> str:
    """环境快照 evalset → 版本串（优先 version，其次 file，最后整体串化）。"""
    ev = ((manifest or {}).get("snapshot") or {}).get("evalset")
    if isinstance(ev, dict):
        return str(ev.get("version") or ev.get("file") or "?")
    return str(ev or "?")


def profile_id_of(manifest: dict | None) -> str:
    """run.json 的 profile 现为 {path, sha256, content}，id 在 content 内；兼容旧的顶层 id。"""
    p = (manifest or {}).get("profile") or {}
    return str(p.get("id") or (p.get("content") or {}).get("id") or "?")


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
        profile_id = profile_id_of(manifest)
        snapshot_json = json.dumps(manifest or {}, ensure_ascii=False)
        # started_at 只认 run.json 的真实值；缺失时首次导入以导入时间回填落列（与 v3
        # 迁移同语义），重导沿用已落列的值、真实值一旦出现即覆盖——不会被反复刷新。
        started_at = (manifest or {}).get("started_at")
        with self.lock, self.conn:
            if started_at is None:
                row = self.conn.execute("SELECT started_at FROM runs WHERE run_id=?", (run_id,)).fetchone()
                started_at = row["started_at"] if row else now()
            self.conn.execute(
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
             profile_id_of(manifest),
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
        """按 run 开始时间排序（R8：导入顺序会把旧 run 提前，绊线方向会判反）。
        无 run.json 的 run 在首次导入时已把导入时间回填进 started_at（重导不刷新）；
        COALESCE 兜底只为兼容未迁移的旧库连接。"""
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM runs ORDER BY COALESCE(started_at, imported_at), run_id")]

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
                    "INSERT OR REPLACE INTO verdicts(answer_id, judge_fp, chain_sha, item_id, kind, turn,"
                    " item_sha, answer_sha, value, source, operator, reason, trace_json, status, judged_at, score)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (answer_id, judge_fp, chain_sha, r["item"]["id"], r["item"]["kind"], r["item"]["turn"],
                     r.get("item_sha"), r["answer_sha"], r["machine"].get("value"), r["machine"].get("source", ""),
                     r["machine"].get("operator", ""), r["machine"].get("reason", ""),
                     json.dumps(r.get("trace") or [], ensure_ascii=False), r["status"], ts,
                     r["machine"].get("score")))
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
        直接写结论，不算待重判。一条 SQL 找出缺判定的答案，只对它们解析 record。"""
        sql = """SELECT a.answer_id, a.qkey, a.run_id, a.episode_id, q.current_judge_fp AS fp,
                 a.record_json, s.spec_json
                 FROM answers a JOIN questions q ON a.qkey = q.qkey
                 JOIN specs s ON s.qkey = a.qkey AND s.judge_fp = q.current_judge_fp
                 WHERE q.current_judge_fp IS NOT NULL
                   AND NOT EXISTS (SELECT 1 FROM verdicts v WHERE v.answer_id = a.answer_id
                        AND v.judge_fp = q.current_judge_fp AND v.chain_sha = ?)"""
        args: list = [chain_sha]
        if qkey:
            sql += " AND a.qkey = ?"
            args.append(qkey)
        if run_id:
            sql += " AND a.run_id = ?"
            args.append(run_id)
        out: list[dict] = []
        for r in self.conn.execute(sql, tuple(args)):
            record = json.loads(r["record_json"])
            if episode_error(record, build_context(record, json.loads(r["spec_json"]))) is None:
                out.append({"answer_id": r["answer_id"], "qkey": r["qkey"], "judge_fp": r["fp"],
                            "run_id": r["run_id"], "episode_id": r["episode_id"]})
        return out

    # ---------------------------------------------------------------- 判定缓存（§6.2）

    def cache_get(self, key: str) -> Verdict | None:
        row = self.conn.execute("SELECT * FROM verdict_cache WHERE cache_key=?", (key,)).fetchone()
        if not row:
            return None
        return Verdict(row["value"], row["source"] or "judge", row["operator"] or "", reason=row["reason"] or "",
                       score=row["score"])

    def cache_put(self, key: str, v: Verdict) -> None:
        self._exec("INSERT OR REPLACE INTO verdict_cache(cache_key, value, source, operator, reason, score)"
                   " VALUES(?,?,?,?,?,?)", (key, v.value, v.source, v.operator, v.reason, v.score))

    def cache_bulk(self, rows: list[dict]) -> int:
        with self.lock, self.conn:
            for d in rows:
                self.conn.execute("INSERT OR REPLACE INTO verdict_cache(cache_key, value, source, operator,"
                                  " reason, score) VALUES(?,?,?,?,?,?)",
                                  (d["key"], d.get("value"), d.get("source", "judge"),
                                   d.get("operator", ""), d.get("reason", ""), d.get("score")))
        return len(rows)

    # ---------------------------------------------------------------- 人工改判（C9）

    def add_review(self, answer_id: str, item_id: str, value: str | None, reason: str, reviewer: str,
                   item_sha: str | None, answer_sha: str, score: float | None = None) -> int:
        with self.lock, self.conn:
            cur = self.conn.execute(
                "INSERT INTO reviews(answer_id, item_id, item_sha, answer_sha, value, reason, reviewer, at, score)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (answer_id, item_id, item_sha, answer_sha, value, reason, reviewer, now(), score))
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
                      judge_fp: str, chain_sha: str, policy: Policy, policy_sha: str,
                      score: float | None = None) -> tuple[str, str]:
        """单条改判（兼容入口）：等价于只含一条的 submit_reviews。"""
        c = self.submit_reviews(answer_id, [{"item_id": item_id, "value": value, "score": score,
                                             "reason": reason}], None, reviewer, judge_fp, chain_sha,
                                policy, policy_sha)
        return c["status"], c.get("reason") or ""

    def _validate_entries(self, answer_id: str, entries: list[dict], total: dict | None,
                          judge_fp: str, chain_sha: str) -> list[tuple]:
        """校验一批人工打分（不写库）；返回待写入行 (item_id, value, score, reason, item_sha, answer_sha)。
        任一条不合法抛 StoreError（带 item_id），整批不写。"""
        a = self.answer(answer_id)
        if a is None:
            raise StoreError("答案不存在")
        vrows = {r["item_id"]: r for r in self.verdicts(answer_id, judge_fp, chain_sha)}
        out: list[tuple] = []
        for e in entries:
            item_id = e.get("item_id")
            vrow = vrows.get(item_id)
            if vrow is None:
                raise StoreError(f"条目不存在（该期望 + 链配置下没有判定行，先 rejudge）：{item_id}")
            value, score = e.get("value"), e.get("score")
            if value not in SCALES.get(vrow["kind"], ()):
                raise StoreError(f"取值不合法：{item_id} = {value!r}")
            if score is not None and not _valid_score(score):
                raise StoreError(f"分数应为 0–100：{item_id} = {score!r}")
            if score is not None and vrow["kind"] in ("precond", "path", "forbidden"):
                raise StoreError(f"前提/路径/禁止条目不记条目分（无权重），不能带分数：{item_id}")
            out.append((item_id, value, None if score is None else float(score), e.get("reason") or "",
                        vrow["item_sha"], vrow["answer_sha"]))
        if total is not None:
            score = total.get("score")
            if score is None or not _valid_score(score):
                raise StoreError(f"人工会话分应为 0–100：{score!r}")
            if not (total.get("reason") or "").strip():
                raise StoreError("人工会话分必须写理由")
            out.append((TOTAL_ITEM, None, float(score), total["reason"].strip(), judge_fp,
                        self._conversation_sha(a, judge_fp)))
        return out

    def submit_reviews(self, answer_id: str, entries: list[dict], total: dict | None, reviewer: str,
                       judge_fp: str, chain_sha: str, policy: Policy, policy_sha: str) -> dict:
        """一段对话的批量人工打分（复核服务改造方案 §6.4）：全部校验 → 同一事务追加 →
        重算一次该答案的结论。任一条不合法则整体不写入；重算失败时追加的记录一并
        回滚，不留人工记录 + 旧结论的中间态。返回结论行。"""
        rows = self._validate_entries(answer_id, entries, total, judge_fp, chain_sha)
        ts = now()
        with self.lock, self.conn:
            for item_id, value, score, reason, item_sha, ans_sha in rows:
                self.conn.execute(
                    "INSERT INTO reviews(answer_id, item_id, item_sha, answer_sha, value, reason, reviewer, at, score)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (answer_id, item_id, item_sha, ans_sha, value, reason, reviewer, ts, score))
            v = self._conclude(answer_id, judge_fp, chain_sha, policy)
            self._write_conclusion(self.conn, answer_id, judge_fp, chain_sha, policy_sha, v)
        return self.conclusion(answer_id, judge_fp, chain_sha, policy_sha) or {}

    def preview_conclusion(self, answer_id: str, entries: list[dict], total: dict | None,
                           judge_fp: str, chain_sha: str, policy: Policy) -> EpisodeVerdict:
        """按草稿重算结论（不落库）：草稿视作排在全部已有改判之后的人工记录。"""
        rows = self._validate_entries(answer_id, entries, total, judge_fp, chain_sha)
        drafts = [{"item_id": item_id, "value": value, "score": score, "reason": reason,
                   "item_sha": item_sha, "answer_sha": ans_sha, "review_id": float("inf")}
                  for item_id, value, score, reason, item_sha, ans_sha in rows]
        return self._conclude(answer_id, judge_fp, chain_sha, policy, extra_reviews=drafts)

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
                 and r["value"] in SCALES.get(vrow["kind"], ())
                 and (r.get("score") is None or _valid_score(r["score"]))]
        if valid:
            r = valid[-1]
            return Verdict(r["value"], "human", "review", reason=r["reason"] or "", score=r.get("score"))
        return Verdict(vrow["value"], vrow["source"] or "", vrow["operator"] or "", reason=vrow["reason"] or "",
                       score=vrow.get("score"))

    def _conversation_sha(self, a: dict, judge_fp: str) -> str:
        spec = self.spec_of(a["qkey"], judge_fp)
        return conversation_sha(build_context(json.loads(a["record_json"]), spec))

    @staticmethod
    def total_override(reviews: list[dict], judge_fp: str, conv_sha: str) -> dict | None:
        """有效人工会话分：最后一条 item_id=@total、item_sha=judge_fp、answer_sha=整段回答指纹、
        分数合法且有理由的记录（期望或回答变了即作废）。"""
        valid = [r for r in reviews
                 if r["item_id"] == TOTAL_ITEM and r["item_sha"] == judge_fp and r["answer_sha"] == conv_sha
                 and r.get("score") is not None and _valid_score(r["score"]) and (r["reason"] or "").strip()]
        if not valid:
            return None
        r = valid[-1]
        return {"score": float(r["score"]), "reason": r["reason"], "reviewer": r.get("reviewer"), "at": r.get("at")}

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
        conv_sha = None
        for r in self.reviews_for(answer_id):
            if r["item_id"] == TOTAL_ITEM:
                conv_sha = conv_sha or self._conversation_sha(a, fp)
                if r["item_sha"] != fp:
                    out.append({"review": r, "reason": "期望已修订（人工会话分作废）"})
                elif r["answer_sha"] != conv_sha:
                    out.append({"review": r, "reason": "回答已变化（人工会话分作废）"})
                continue
            ref = refs.get(r["item_id"])
            if ref is None:
                why = "条目不存在"
            elif r["answer_sha"] != ref["answer_sha"]:
                why = "answer_sha 不匹配（回答已变化）"
            elif r["item_sha"] is None or r["item_sha"] != ref["item_sha"]:
                why = "item_sha 不匹配（期望已修订）"
            elif r["value"] not in SCALES.get(ref["kind"], ()):
                why = f"取值不合法：{r['value']!r}"
            elif r.get("score") is not None and not _valid_score(r["score"]):
                why = f"分数不合法：{r['score']!r}"
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
        （前提判不成立/路径违反后的事实、forced 会话的路径、权重 0 的事实）不进队列。
        期望事实按权重计分（doc/10 §4.2），未决时同样进队列。"""
        sql = """SELECT v.answer_id, v.item_id, v.kind, v.turn, v.value AS machine_value,
                 v.source AS machine_source, v.reason AS machine_reason, v.status,
                 q.uid, a.run_id, a.episode_id, a.mode, a.trial, a.qkey, q.current_judge_fp,
                 (SELECT r.value FROM reviews r WHERE r.answer_id = v.answer_id AND r.item_id = v.item_id
                    AND r.answer_sha = v.answer_sha AND r.item_sha = v.item_sha
                    ORDER BY r.review_id DESC LIMIT 1) AS review_value
                 FROM verdicts v JOIN answers a USING(answer_id) JOIN questions q ON a.qkey = q.qkey
                 WHERE q.current_judge_fp = v.judge_fp AND v.chain_sha = ?
                   AND (v.status != 'ok' OR v.value IS NULL)"""
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
                spec = specs.get((first["qkey"], fp)) or {}
                answerable = spec.get("answerable", True) is not False
                zero_weight = {f.get("id") for f in spec.get("facts") or [] if f.get("weight") == 0}
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
                    elif c["kind"] in ("required", "forbidden", "expected") and facts_live \
                            and c["item_id"] not in zero_weight:
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

    def _conclude(self, answer_id: str, judge_fp: str, chain_sha: str, policy: Policy,
                  extra_reviews: list[dict] | None = None) -> EpisodeVerdict:
        """计算一个 (答案, 期望, 链配置, 口径) 的会话结论与会话分（不写库、不调用算子）。"""
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
            return EpisodeVerdict(uid=record.get("uid", "?"), mode=record.get("mode", "?"),
                                  trial=record.get("trial", 0), dim=spec.get("dim", "?"),
                                  status="error", error=err)
        items = extract_items(ctx)
        reviews = self.reviews_for(answer_id) + list(extra_reviews or [])
        vrows = {r["item_id"]: r for r in self.verdicts(answer_id, judge_fp, chain_sha)}
        eff: dict[str, Verdict] = {}
        statuses: dict[str, str] = {}
        for item in items:
            vrow = vrows.get(item.id)
            if vrow is None:
                eff[item.id] = Verdict(None, "", "")
                statuses[item.id] = "undecided"
            else:
                eff[item.id] = self.effective(vrow, reviews)
                # 人工改判覆盖后该条目已决（R10：异常按条目 status 判，单一判据）
                statuses[item.id] = "ok" if eff[item.id].source == "human" else vrow["status"]
        override = self.total_override(reviews, judge_fp, conversation_sha(ctx))
        return session_conclusion(record, ctx, items, eff, policy, routing, statuses=statuses, override=override)

    @staticmethod
    def _write_conclusion(conn, answer_id: str, judge_fp: str, chain_sha: str,
                          policy_sha: str, v: EpisodeVerdict) -> None:
        """物化一条结论行（不开事务，供 submit_reviews 在同一事务内复用）。"""
        conn.execute("INSERT OR REPLACE INTO conclusions(answer_id, judge_fp, chain_sha, policy_sha, status, reason,"
                     " detail_json, score, score_min, score_max, auto_score, score_source)"
                     " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                     (answer_id, judge_fp, chain_sha, policy_sha, v.status,
                      v.invalid_reason or v.error or "", json.dumps(v.as_dict(), ensure_ascii=False),
                      v.score, v.score_min, v.score_max, v.auto_score, v.score_source))

    def recompute_conclusion(self, answer_id: str, judge_fp: str, chain_sha: str,
                             policy: Policy, policy_sha: str) -> str:
        """重算并物化一个 (答案, 期望, 链配置, 口径) 的会话结论与会话分（不调用任何算子）。"""
        v = self._conclude(answer_id, judge_fp, chain_sha, policy)
        with self.lock, self.conn:
            self._write_conclusion(self.conn, answer_id, judge_fp, chain_sha, policy_sha, v)
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
               c.status AS c_status, c.reason AS c_reason, c.detail_json,
               c.score AS c_score, c.score_min AS c_score_min, c.score_max AS c_score_max,
               c.score_source AS c_score_source
        FROM answers a JOIN questions q ON a.qkey = q.qkey
        LEFT JOIN conclusions c ON c.answer_id = a.answer_id AND c.judge_fp = q.current_judge_fp
             AND c.chain_sha = ? AND c.policy_sha = ?
        ORDER BY a.answer_id"""
        return [dict(r) for r in self.conn.execute(sql, (chain_sha, policy_sha))]

    # ---------------------------------------------------------------- 判定配置（chains，§6.4）

    def register_chain(self, chain_sha: str, name: str | None, operators: list[str],
                       judge_models: list[str] | None = None) -> None:
        """登记一套判定配置（score / rejudge 时写入；已存在则补全名称与算子清单）。"""
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO chains(chain_sha, name, operators_json, judge_models_json, created_at) VALUES(?,?,?,?,?)"
                " ON CONFLICT(chain_sha) DO UPDATE SET name=COALESCE(excluded.name, chains.name),"
                " operators_json=excluded.operators_json, judge_models_json=excluded.judge_models_json,"
                " created_at=COALESCE(chains.created_at, excluded.created_at)",
                (chain_sha, name, json.dumps(operators, ensure_ascii=False),
                 json.dumps(judge_models or [], ensure_ascii=False), now()))

    def chains(self) -> list[dict]:
        out = []
        for r in self.conn.execute("SELECT * FROM chains ORDER BY created_at, chain_sha"):
            d = dict(r)
            d["operators"] = json.loads(d.pop("operators_json") or "[]")
            d["judge_models"] = json.loads(d.pop("judge_models_json") or "[]")
            out.append(d)
        return out

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

    # ---------------------------------------------------------------- 复核队列与复核页（doc/10 §5.2/5.3）

    REVIEW_TABS = ("undecided", "unjudged", "spot", "mismatch", "failed")

    def review_conversations(self, chain_sha: str, policy: Policy, policy_sha: str,
                             tab: str = "undecided", run_id: str | None = None, dim: str | None = None,
                             spot_ratio: float = 0.1, page: int = 0, size: int = 20,
                             first_input: bool = True) -> tuple[list[dict], dict]:
        """复核队列（以对话为单位，五个标签）：SQL 聚合 + 分页。
        undecided=结论待定（区间跨及格线/前提未判，含判定异常）；unjudged=结论已定仍有未判；
        spot=模型判过且无人工（按比例抽样）；mismatch=人工≠机器；failed=会话失败。
        first_input=False 供复核页取上/下一段：免解析全部 record（大库性能）。"""
        rows = self.answer_view_rows(chain_sha, policy_sha)
        if run_id:
            rows = [r for r in rows if r["run_id"] == run_id]
        if dim:
            rows = [r for r in rows if (r["dim"] or "") == dim]
        pend_items, _ = self.undecided_items(chain_sha, run_id=run_id, page=0, size=10 ** 9)
        pend_by_answer: dict[str, list[dict]] = defaultdict(list)
        for c in pend_items:
            pend_by_answer[c["answer_id"]].append(c)
        mismatch_ids = {m["answer_id"] for m in self.human_machine_compare(chain_sha)[0]}
        judge_ids = {r["answer_id"] for r in self.conn.execute(
            "SELECT DISTINCT answer_id FROM verdicts WHERE chain_sha=? AND source='judge'", (chain_sha,))}
        human_ids = {r["answer_id"] for r in self.conn.execute("SELECT DISTINCT answer_id FROM reviews")}
        pass_mark = policy.score.pass_mark
        convs: list[dict] = []
        for r in rows:
            aid = r["answer_id"]
            pend = pend_by_answer.get(aid, [])
            kinds: dict[str, int] = defaultdict(int)
            for c in pend:
                kinds[c["kind"]] += 1
            c = {
                "answer_id": aid, "uid": r["uid"], "case_id": r["case_id"], "qkey": r["qkey"],
                "run_id": r["run_id"], "mode": r["mode"], "trial": r["trial"], "dim": r["dim"] or "?",
                "profile": r["profile_id"] or "?", "models": json.loads(r["models_json"] or "[]"),
                "status": r["c_status"], "reason": r["c_reason"] or "",
                "score": r["c_score"], "score_min": r["c_score_min"], "score_max": r["c_score_max"],
                "run_status": r["run_status"], "pending": len(pend), "pending_kinds": dict(kinds),
            }
            lo, hi = c["score_min"], c["score_max"] if c["score_max"] is not None else c["score"]
            if lo is None and hi is None:
                lo = hi = c["score"]
            c["_range"] = (lo, hi)
            c["_undecided"] = r["c_status"] in ("undecided", "error")
            c["_unjudged"] = r["c_status"] in ("pass", "fail") and len(pend) > 0
            c["_spot"] = r["c_status"] in ("pass", "fail") and aid in judge_ids and aid not in human_ids
            c["_mismatch"] = aid in mismatch_ids
            c["_failed"] = r["run_status"] not in ("ok", None)
            convs.append(c)
        tabs = {t: sum(1 for c in convs if c[f"_{t}"]) for t in self.REVIEW_TABS}
        selected = [c for c in convs if c[f"_{tab}"]]
        if tab == "spot" and spot_ratio < 1:
            selected = [c for c in selected if _spot_picked(c["answer_id"], spot_ratio)]
        if tab == "undecided":
            def near(c):
                lo, hi = c["_range"]
                d = pass_mark
                if lo is not None and hi is not None:
                    d = 0 if lo <= pass_mark <= hi else min(abs(lo - pass_mark), abs(hi - pass_mark))
                return (d, c["pending"], c["answer_id"])
            selected.sort(key=near)
        else:
            selected.sort(key=lambda c: c["answer_id"])
        total = len(selected)
        page = max(0, min(page, (max(total, 1) - 1) // size))
        out = []
        for c in selected[page * size:(page + 1) * size]:
            c = dict(c)
            c.pop("_undecided", None), c.pop("_unjudged", None), c.pop("_spot", None)
            c.pop("_mismatch", None), c.pop("_failed", None)
            if first_input:
                record = json.loads((self.answer(c["answer_id"]) or {}).get("record_json") or "{}")
                # runner 写 source='prefill'（episode-runner.ts）；'preset' 为旧拼写，兼容
                turns = [t for t in record.get("turns") or []
                         if t.get("source") not in ("prefill", "preset")]
                c["first_input"] = (turns[0].get("input") if turns else "") or ""
            out.append(c)
        return out, {"tabs": tabs, "total": total, "page": page}

    def conversation_view(self, answer_id: str, chain_sha: str, policy: Policy, policy_sha: str,
                          judge_fp: str | None = None) -> dict:
        """复核页一次取齐（§5.3）：record、当前期望、条目（按判定顺序分节）、机器栏、
        人工栏、有效结论、人工会话分、作废记录、轮次观测。"""
        a = self.answer(answer_id)
        if a is None:
            raise StoreError(f"答案不存在：{answer_id}")
        cur = self.current_spec(a["qkey"])
        fp = judge_fp or (cur[0] if cur else None)
        if fp is None:
            fp = a["judge_fp_at_run"]
        spec = self.spec_of(a["qkey"], fp) or {}
        record = json.loads(a["record_json"])
        ctx = build_context(record, spec)
        items = extract_items(ctx)
        reviews = self.reviews_for(answer_id)
        vrows = {r["item_id"]: r for r in self.verdicts(answer_id, fp, chain_sha)}
        sp = policy.score
        sections = []
        for kind, label, hint in (("precond", "前提", "门槛：不成立 → 会话无效，不计分"),
                                  ("path", "路径", "否决项：违反 → 总分 0"),
                                  ("forbidden", "禁止事实", "否决项：采信 → 总分 0"),
                                  ("required", "必要事实", "按权重计分；判「错」→ 总分 0"),
                                  ("expected", "期望事实", "按权重计分"),
                                  ("refusal", "拒答", "即会话分")):
            rows_ = []
            for it in items:
                if it.kind != kind:
                    continue
                vrow = vrows.get(it.id)
                eff = self.effective(vrow, reviews) if vrow else None
                saved = None
                if vrow is not None:
                    cands = [r for r in reviews if r["item_id"] == it.id
                             and r["answer_sha"] == vrow["answer_sha"] and r["item_sha"] == vrow["item_sha"]]
                    if cands:
                        saved = cands[-1]
                rows_.append({
                    "id": it.id, "kind": kind, "turn": it.turn, "text": it.text or "",
                    "keywords": it.attrs.get("keywords") or [],
                    "weight": weight_of(it, sp.default_weight), "group": it.attrs.get("group"),
                    "answer": ctx.answer_for(it),
                    "machine": None if vrow is None else {
                        "value": vrow["value"], "score": vrow["score"], "source": vrow["source"],
                        "operator": vrow["operator"], "reason": vrow["reason"] or "",
                        "status": vrow["status"], "trace": vrow["trace_json"] or ""},
                    "eff": {"value": eff.value, "score": eff.score, "source": eff.source} if eff else None,
                    "human": {"value": saved["value"], "score": saved["score"], "reason": saved["reason"],
                              "reviewer": saved["reviewer"], "at": saved["at"]} if saved else None,
                })
            if rows_:
                sections.append({"kind": kind, "label": label, "hint": hint, "rows": rows_})
        turns = []
        for t in record.get("turns") or []:
            obs = t.get("observations") or {}
            turns.append({
                "index": t.get("index"), "input": t.get("input", ""), "output": t.get("output", ""),
                "status": t.get("status"), "source": t.get("source", "live"),
                "skills": [s["name"] for s in obs.get("skills", []) if s.get("ok")],
                "ask_user": (obs.get("ask_user") or {}).get("count", 0),
                "tool_errors": obs.get("tool_errors") or [],
            })
        concl = self.conclusion(answer_id, fp, chain_sha, policy_sha) or {}
        voided = self.voided_reviews(answer_id)
        conv_sha = conversation_sha(ctx)
        override = self.total_override(reviews, fp, conv_sha)
        manifest = json.loads((self.run(a["run_id"]) or {}).get("snapshot_json") or "{}")
        profile_content = ((manifest.get("profile") or {}).get("content") or {})
        q = self.question(a["qkey"]) or {}
        return {
            "answer_id": answer_id, "qkey": a["qkey"], "uid": q.get("uid") or record.get("uid", "?"),
            "case_id": q.get("case_id"), "dim": q.get("dim"),
            "topic": q.get("topic"), "rewrite": q.get("rewrite"), "variant_index": q.get("variant_index"),
            "run_id": a["run_id"], "mode": a["mode"], "trial": a["trial"], "run_status": a["run_status"],
            "profile": a["profile_id"], "models": json.loads(a["models_json"] or "[]"),
            "skills_mode": a["skills_mode"], "evalset_version": a["evalset_version"],
            "reasoning_effort": profile_content.get("reasoning_effort"),
            "judge_fp": fp, "is_current": cur is not None and fp == cur[0],
            "sections": sections, "turns": turns,
            "injections_applied": record.get("injections_applied") or [],
            "injection_observed": json.loads(a.get("injection_observed_json") or "null"),
            "conclusion": concl, "voided": voided, "override": override,
            "spec": spec, "pending_rejudge": len(self.pending_rejudge(chain_sha, qkey=a["qkey"])),
        }
