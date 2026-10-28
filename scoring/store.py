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

from .items import (SCALES, build_context, episode_error, extract_items, input_fp_of, spec_weights_any,
                    standard_fp_of, spec_weights_to_percent, strip_spec_weights, qkey_of,
                    variant_leak_suspects, TOTAL_ITEM, conversation_sha, version_label, weight_of)
from .operators import Verdict
from .policy import Policy, rules_dict_of_policy, method_dict_of_policy
from .rules import EpisodeVerdict, Rules, canonical_sha, session_conclusion
from .subject import subject_fp_of_manifest

SCHEMA_VERSION = 8

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


def _migrate_granularity(conn) -> None:
    """v5：题目粒度（环节/细节）。已有题从当前期望的编译行回填。"""
    conn.execute("ALTER TABLE questions ADD COLUMN granularity TEXT")
    rows = conn.execute(
        "SELECT q.qkey, s.spec_json FROM questions q"
        " LEFT JOIN specs s ON s.qkey=q.qkey AND s.judge_fp=q.current_judge_fp").fetchall()
    for row in rows:
        try:
            g = (json.loads(row["spec_json"] or "{}") or {}).get("granularity")
        except (json.JSONDecodeError, TypeError):
            g = None
        if g:
            conn.execute("UPDATE questions SET granularity=? WHERE qkey=?", (g, row["qkey"]))


_MIGRATIONS[5] = [_migrate_granularity]


def _migrate_weight_percent(conn) -> None:
    """v6：给权重在线调整早期落库的期望补 weight_percent 标记。标记只影响用例页
    回显（带标记按原值显示、剩余为未分配；缺标记会被归一化，未分配丢失），不进
    judge_fp/item_sha。evalset_version='权重调整' 的行只可能来自 revise_weights，
    权值必为百分比语义。"""
    rows = conn.execute(
        "SELECT qkey, judge_fp, spec_json FROM specs WHERE evalset_version='权重调整'"
        " AND spec_json NOT LIKE '%\"weight_percent\"%'").fetchall()
    for row in rows:
        try:
            spec = json.loads(row["spec_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        spec["weight_percent"] = True
        conn.execute("UPDATE specs SET spec_json=? WHERE qkey=? AND judge_fp=?",
                     (json.dumps(spec, ensure_ascii=False), row["qkey"], row["judge_fp"]))


_MIGRATIONS[6] = [_migrate_weight_percent]


def _migrate_run_title(conn) -> None:
    """v7：执行记录命名（runs.title，纯展示层）。缺省标题由 run_labels() 按已知
    条件现拼（评测集版本 · profile · modes×trials），不落库、不进任何键。"""
    conn.execute("ALTER TABLE runs ADD COLUMN title TEXT")


_MIGRATIONS[7] = [_migrate_run_title]


# v8（doc/15 §2 配置重构）：标准指纹去权重、被测指纹、快照/测法/规则、条目权重表。
_MIGRATION_8_SQL = """
    ALTER TABLE runs ADD COLUMN subject_fp TEXT;
    ALTER TABLE runs ADD COLUMN subject_json TEXT;
    ALTER TABLE runs ADD COLUMN subject_incomplete INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE runs ADD COLUMN method_sha TEXT;
    ALTER TABLE runs ADD COLUMN diagnostic INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE runs ADD COLUMN scope_json TEXT;
    ALTER TABLE answers ADD COLUMN subject_fp TEXT;
    ALTER TABLE answers ADD COLUMN model TEXT;
    ALTER TABLE answers ADD COLUMN reasoning_effort TEXT;
    ALTER TABLE answers ADD COLUMN agent TEXT;
    ALTER TABLE questions ADD COLUMN off_snapshot INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE questions ADD COLUMN current_weight_fp TEXT;
    CREATE TABLE snapshots(
      evalset_version TEXT PRIMARY KEY, snapshot_sha TEXT NOT NULL,
      method_json TEXT NOT NULL, method_sha TEXT NOT NULL,
      include TEXT, name TEXT, imported_at TEXT NOT NULL);
    CREATE TABLE item_weights(
      case_id TEXT NOT NULL, item_id TEXT NOT NULL, weight REAL NOT NULL,
      label TEXT, source TEXT NOT NULL, set_by TEXT, set_at TEXT NOT NULL,
      active INTEGER NOT NULL DEFAULT 1);
    CREATE INDEX idx_item_weights ON item_weights(case_id, active, item_id);
    CREATE TABLE rules(
      rules_sha TEXT PRIMARY KEY, rules_json TEXT NOT NULL, name TEXT, created_at TEXT NOT NULL);
    ALTER TABLE conclusions RENAME TO conclusions_legacy;
    CREATE TABLE conclusions(
      answer_id TEXT, standard_fp TEXT, chain_sha TEXT, rules_sha TEXT, weight_fp TEXT,
      status TEXT, reason TEXT, detail_json TEXT,
      score REAL, score_min REAL, score_max REAL, auto_score REAL, score_source TEXT,
      PRIMARY KEY(answer_id, standard_fp, chain_sha, rules_sha, weight_fp));
    """


def _v8_percent_weights(spec: dict) -> dict[str, float]:
    """旧期望 → 百分制权重：weight_percent 标记的权值本就是百分制（revise_weights
    语义，取 facts 原值）；否则按相对值换算（doc/15 §2.4，全精度不取整）。"""
    if spec.get("weight_percent"):
        return {f["id"]: float(f["weight"]) for f in spec.get("facts") or []
                if f.get("id") and "weight" in f}
    return spec_weights_any(spec)


def _migrate_v8(conn) -> None:
    """v8 数据迁移（doc/15 §6，单事务由 _migrate 保证）：
    1) specs 去权重重算 standard_fp 合并（当前期望那份优先，B7）；
    2) 权重落 item_weights（种子题当前期望为有效行；变体不一致进报告）；
    3) verdicts/spec_versions/questions/@total 键改写；伪版本「权重调整」不生成快照（S3）；
    4) runs/answers 回填被测指纹；
    5) 由 policy.yaml + anchors 生成各快照的测法与 snapshot_sha；
    6) policy.yaml 生成计分规则；meta.current_snapshot 落位、偏离标记。
    报告写 meta.v8_report（migrate-verify 读取）。空库（全新测试库）各步自然为空操作。"""
    import pathlib
    repo = pathlib.Path(__file__).resolve().parents[1]

    # ---------- 0) 版本名规整：早期库的 evalset_version 存的是 dist 全路径，
    # 迁移时统一为短名（version_label：basename 去 .jsonl 去 evalset- 前缀），
    # 从此库内即短名（此前只在展示层规整）。
    seen_versions = {r[0] for tbl in ("specs", "spec_versions", "runs", "answers")
                     for r in conn.execute(f"SELECT DISTINCT evalset_version FROM {tbl}"
                                           " WHERE evalset_version IS NOT NULL")}
    for v in sorted(seen_versions):
        nv = version_label(v)
        if nv != v:
            for tbl in ("specs", "spec_versions", "runs", "answers"):
                conn.execute(f"UPDATE {tbl} SET evalset_version=? WHERE evalset_version=?", (nv, v))

    # ---------- 1) specs 重建：去权重 → standard_fp，合并重复行
    old_current = {r["qkey"]: r["current_judge_fp"] for r in
                   conn.execute("SELECT qkey, current_judge_fp FROM questions") if r["current_judge_fp"]}
    old_rows = conn.execute("SELECT qkey, judge_fp, spec_json, evalset_version, imported_at FROM specs").fetchall()
    kept: dict[tuple, dict] = {}            # (qkey, standard_fp) -> 行（当前期望优先）
    weights_by_old: dict[tuple, dict] = {}  # (qkey, 旧fp) -> 百分制权重（快照初始值/承接用）
    fp_map: dict[tuple, str] = {}           # (qkey, 旧fp) -> 新 standard_fp
    case_of_q: dict[str, str] = {}
    for r in old_rows:
        spec = json.loads(r["spec_json"] or "{}")
        weights_by_old[(r["qkey"], r["judge_fp"])] = _v8_percent_weights(spec)
        std = strip_spec_weights(spec)
        std.pop("weight_percent", None)
        fp = standard_fp_of(std)
        fp_map[(r["qkey"], r["judge_fp"])] = fp
        case_of_q[r["qkey"]] = std.get("case_id") or std.get("id") or "?"
        is_cur = r["judge_fp"] == old_current.get(r["qkey"])
        key = (r["qkey"], fp)
        if key not in kept or (is_cur and not kept[key]["cur"]):
            kept[key] = {"cur": is_cur, "spec": std, "version": r["evalset_version"],
                         "imported_at": r["imported_at"]}
    conn.execute("ALTER TABLE specs RENAME TO specs_legacy")
    conn.execute("CREATE TABLE specs(qkey TEXT, standard_fp TEXT, spec_json TEXT,"
                 " evalset_version TEXT, imported_at TEXT, PRIMARY KEY(qkey, standard_fp))")
    for (qkey, fp), k in kept.items():
        conn.execute("INSERT INTO specs VALUES(?,?,?,?,?)",
                     (qkey, fp, json.dumps(k["spec"], ensure_ascii=False), k["version"], k["imported_at"]))

    # ---------- 2) 权重 → item_weights（按用例，种子题当前期望为准，B1/§6.2）
    seed_q: dict[str, str] = {}             # case_id -> 种子 qkey（uid 最小 / variant_index=0）
    for r in conn.execute("SELECT qkey, uid, case_id FROM questions"):
        cid = r["case_id"] or (r["uid"].split("#")[0] if r["uid"] else "?")
        if cid not in seed_q or r["uid"] < conn.execute(
                "SELECT uid FROM questions WHERE qkey=?", (seed_q[cid],)).fetchone()["uid"]:
            seed_q[cid] = r["qkey"]
    ts = now()
    variant_mismatch: list[str] = []
    for cid, q in seed_q.items():
        cur_fp = old_current.get(q)
        base = weights_by_old.get((q, cur_fp)) if cur_fp else None
        if not base:
            continue
        for item_id, w in base.items():
            conn.execute("INSERT INTO item_weights VALUES(?,?,?,?,?,?,?,?)",
                         (cid, item_id, w, None, "legacy", None, ts, 1))
        for r in conn.execute("SELECT qkey FROM questions WHERE case_id=?", (cid,)):
            vfp = old_current.get(r["qkey"])
            if vfp and r["qkey"] != q and weights_by_old.get((r["qkey"], vfp)) != base:
                variant_mismatch.append(cid)

    # ---------- 2b) 回填 questions.current_weight_fp（§6.2）：有权重行的用例算
    # weight_fp 缓存列，无权重用例留 NULL（查询侧按 'equal' 兜底）。
    latest_w: dict[str, dict[str, float]] = {}
    for row in conn.execute(
            "SELECT case_id, item_id, weight FROM item_weights WHERE active=1"
            " ORDER BY set_at, rowid").fetchall():
        latest_w.setdefault(row["case_id"], {})[row["item_id"]] = row["weight"]
    for cid, wmap in latest_w.items():
        conn.execute("UPDATE questions SET current_weight_fp=? WHERE case_id=?",
                     (canonical_sha(wmap), cid))

    # ---------- 3) 键改写：verdicts / spec_versions / questions / reviews@total
    conn.execute("ALTER TABLE verdicts RENAME TO verdicts_legacy")
    conn.execute("CREATE TABLE verdicts(answer_id TEXT, standard_fp TEXT, chain_sha TEXT, item_id TEXT,"
                 " kind TEXT, turn INTEGER, item_sha TEXT, answer_sha TEXT, value TEXT, source TEXT,"
                 " operator TEXT, reason TEXT, trace_json TEXT, status TEXT, judged_at TEXT, score REAL,"
                 " PRIMARY KEY(answer_id, standard_fp, chain_sha, item_id))")
    qkey_of_answer = {r["answer_id"]: r["qkey"] for r in conn.execute("SELECT answer_id, qkey FROM answers")}
    discarded_diff: list[dict] = []
    cur_first: dict[tuple, list] = {}
    for r in conn.execute("SELECT * FROM verdicts_legacy"):
        new_fp = fp_map.get((qkey_of_answer.get(r["answer_id"]), r["judge_fp"]), r["judge_fp"])
        is_cur = old_current.get(qkey_of_answer.get(r["answer_id"])) == r["judge_fp"]
        cur_first.setdefault((r["answer_id"], new_fp, r["chain_sha"], r["item_id"]), []).append(
            {"row": r, "cur": is_cur})
    for key, group in cur_first.items():
        group.sort(key=lambda g: not g["cur"])          # 当前期望那份优先（B7）
        win = group[0]["row"]
        conn.execute(
            "INSERT INTO verdicts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (win["answer_id"], key[1], key[2], key[3], win["kind"], win["turn"], win["item_sha"],
             win["answer_sha"], win["value"], win["source"], win["operator"], win["reason"],
             win["trace_json"], win["status"], win["judged_at"], win["score"]))
        for g in group[1:]:
            l = g["row"]
            if (l["value"], l["source"], l["status"], l["score"]) != \
                    (win["value"], win["source"], win["status"], win["score"]):
                discarded_diff.append({"answer_id": l["answer_id"], "item_id": l["item_id"],
                                       "kept": win["value"], "dropped": l["value"]})
    conn.execute("DROP TABLE verdicts_legacy")
    conn.execute("DROP TABLE specs_legacy")

    conn.execute("ALTER TABLE spec_versions RENAME TO spec_versions_legacy")
    conn.execute("CREATE TABLE spec_versions(qkey TEXT, standard_fp TEXT, evalset_version TEXT,"
                 " imported_at TEXT, PRIMARY KEY(qkey, standard_fp, evalset_version))")
    versions: dict[str, list] = {}
    for r in conn.execute("SELECT * FROM spec_versions_legacy"):
        if r["evalset_version"] == "权重调整":       # 伪版本（S3）：不进快照，也不保留映射
            continue
        new_fp = fp_map.get((r["qkey"], r["judge_fp"]))
        if new_fp:
            conn.execute("INSERT OR IGNORE INTO spec_versions VALUES(?,?,?,?)",
                         (r["qkey"], new_fp, r["evalset_version"], r["imported_at"]))
        versions.setdefault(r["evalset_version"], []).append((r["qkey"], r["judge_fp"]))
    conn.execute("DROP TABLE spec_versions_legacy")

    conn.execute("ALTER TABLE questions RENAME TO questions_legacy")
    conn.execute("CREATE TABLE questions(qkey TEXT PRIMARY KEY,"
                 " uid TEXT, case_id TEXT, variant_index INTEGER, rewrite TEXT,"
                 " dim TEXT, topic TEXT, tags_json TEXT, input_fp TEXT,"
                 " current_standard_fp TEXT, active INTEGER, first_seen TEXT,"
                 " granularity TEXT, off_snapshot INTEGER NOT NULL DEFAULT 0, current_weight_fp TEXT)")
    for r in conn.execute("SELECT * FROM questions_legacy"):
        new_fp = fp_map.get((r["qkey"], r["current_judge_fp"]), r["current_judge_fp"]) \
            if r["current_judge_fp"] else None
        conn.execute("INSERT INTO questions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (r["qkey"], r["uid"], r["case_id"], r["variant_index"], r["rewrite"], r["dim"],
                      r["topic"], r["tags_json"], r["input_fp"], new_fp, r["active"], r["first_seen"],
                      r["granularity"], 0, None))
    conn.execute("DROP TABLE questions_legacy")

    for r in conn.execute("SELECT review_id, answer_id, item_sha FROM reviews WHERE item_id=?",
                          (TOTAL_ITEM,)):
        new_fp = fp_map.get((qkey_of_answer.get(r["answer_id"]), r["item_sha"]), r["item_sha"])
        conn.execute("UPDATE reviews SET item_sha=? WHERE review_id=?", (new_fp, r["review_id"]))

    # ---------- 4) 被测指纹回填（runs + answers）
    for r in conn.execute("SELECT run_id, snapshot_json FROM runs").fetchall():
        try:
            manifest = json.loads(r["snapshot_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            manifest = {}
        fp, js, incomplete = subject_fp_of_manifest(manifest)
        conn.execute("UPDATE runs SET subject_fp=?, subject_json=?, subject_incomplete=? WHERE run_id=?",
                     (fp, js, incomplete, r["run_id"]))
    run_subject = {r["run_id"]: (r["subject_fp"], r["subject_json"]) for r in
                   conn.execute("SELECT run_id, subject_fp, subject_json FROM runs")}
    for a in conn.execute("SELECT answer_id, run_id, models_json FROM answers").fetchall():
        sfp, sjs = run_subject.get(a["run_id"], (None, None))
        subject = json.loads(sjs) if sjs else {}
        agent = subject.get("agent") or {}
        conn.execute("UPDATE answers SET subject_fp=?, model=?, reasoning_effort=?, agent=? WHERE answer_id=?",
                     (sfp, subject.get("model") or (json.loads(a["models_json"] or "[]") or [None])[0],
                      subject.get("reasoning_effort"),
                      f"{agent.get('name')}" + (f"@{agent['version']}" if agent.get("version") else ""),
                      a["answer_id"]))

    # ---------- 5) 快照与测法（由 policy.yaml + anchors 生成，doc/15 §3.9 P1）
    try:
        from .policy import default_policy_path, load_policy
        policy, _ = load_policy(default_policy_path())
    except Exception:
        policy = Policy()
    try:
        import yaml
        mapping = yaml.safe_load((repo / "evalset" / "mapping.yaml").read_text(encoding="utf-8")) or {}
        vocab = list(mapping.get("rewrites") or [])
    except Exception:
        vocab = ["口语", "极简", "语序", "诱导"]
    anchors_all: list[str] = []
    anchors_file = repo / "runner" / "anchors.txt"
    if anchors_file.exists():
        anchors_all = [x.strip() for x in anchors_file.read_text(encoding="utf-8").splitlines() if x.strip()]
    method_file = repo / "evalset" / "method.yaml"
    if method_file.exists():
        import yaml as _yaml
        method = _yaml.safe_load(method_file.read_text(encoding="utf-8")) or {}
        if not method.get("modes"):
            method = method_dict_of_policy(policy, vocab, anchors_all)
    else:                                  # 首次迁移：由 policy + anchors 生成并落盘（§3.9 P1）
        method = method_dict_of_policy(policy, vocab, anchors_all)
        try:
            import yaml as _yaml
            method_file.write_text(_yaml.safe_dump(method, allow_unicode=True, sort_keys=False),
                                   encoding="utf-8")
        except Exception:
            pass
    method_js = json.dumps(method, ensure_ascii=False, sort_keys=True)
    method_sha = canonical_sha(method)
    uid_of_q = {r["qkey"]: r["uid"] for r in conn.execute("SELECT qkey, uid FROM questions")}
    case_by_version: dict[str, dict] = {}
    for version, members in sorted(versions.items()):
        rows_v, wmap = [], {}
        for qkey, old_fp in members:
            new_fp = fp_map.get((qkey, old_fp))
            qrow = conn.execute("SELECT uid, input_fp FROM questions WHERE qkey=?", (qkey,)).fetchone()
            w = weights_by_old.get((qkey, old_fp)) or {}
            if w:
                wmap[case_of_q.get(qkey, "?")] = w
            rows_v.append([qrow["uid"] if qrow else "?",
                           (qrow["input_fp"] if qrow else "") or "", new_fp or ""])
        snap_sha = canonical_sha({"rows": sorted(rows_v), "weights": wmap, "method_sha": method_sha})
        conn.execute("INSERT OR REPLACE INTO snapshots VALUES(?,?,?,?,?,?,?)",
                     (version, snap_sha, method_js, method_sha, "admitted", None, ts))
    latest = conn.execute("SELECT evalset_version FROM spec_versions"
                          " ORDER BY imported_at DESC, evalset_version DESC LIMIT 1").fetchone()
    current_snapshot = latest["evalset_version"] if latest else None
    if current_snapshot:
        conn.execute("INSERT OR REPLACE INTO meta VALUES('current_snapshot', ?)", (current_snapshot,))
        std_in_snap = {r["qkey"]: r["standard_fp"] for r in conn.execute(
            "SELECT qkey, standard_fp FROM spec_versions WHERE evalset_version=?", (current_snapshot,))}
        for q in conn.execute("SELECT qkey, current_standard_fp FROM questions").fetchall():
            if q["current_standard_fp"] and std_in_snap.get(q["qkey"]) != q["current_standard_fp"]:
                conn.execute("UPDATE questions SET off_snapshot=1 WHERE qkey=?", (q["qkey"],))

    # ---------- 6) 计分规则（policy.yaml 生成）+ 迁移报告
    rules_d = rules_dict_of_policy(policy)
    rules_js = json.dumps(rules_d, ensure_ascii=False, sort_keys=True)
    rules_sha = canonical_sha(rules_d)
    conn.execute("INSERT OR IGNORE INTO rules VALUES(?,?,?,?)", (rules_sha, rules_js, "迁移生成", ts))
    conn.execute("INSERT OR REPLACE INTO meta VALUES('rules_sha', ?)", (rules_sha,))
    report = {"merged_specs": len(old_rows) - len(kept),
              "discarded_verdict_diff": discarded_diff,
              "variant_weight_mismatch": sorted(set(variant_mismatch)),
              "snapshots": sorted(versions), "rules_sha": rules_sha,
              "current_snapshot": current_snapshot}
    conn.execute("INSERT OR REPLACE INTO meta VALUES('v8_report', ?)",
                 (json.dumps(report, ensure_ascii=False),))


_MIGRATIONS[8] = [_MIGRATION_8_SQL, _migrate_v8]


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
    """环境快照 evalset → 版本串（优先 version，其次 file，最后整体串化）。
    file 是全路径时规整为短名（items.version_label），避免比较轴与页面上出现路径。"""
    ev = ((manifest or {}).get("snapshot") or {}).get("evalset")
    if isinstance(ev, dict):
        if ev.get("version"):
            return str(ev["version"])
        return version_label(ev.get("file") or "?")
    return version_label(str(ev or "?"))


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
                        current_standard_fp: str | None = None,
                        current_if_absent: str | None = None) -> str:
        """按编译行登记题目。active/current_standard_fp 缺省不改动（specs import 时统一
        刷新）；current_if_absent：该题尚无当前标准时设置之并置活跃（ingest 路径，R5）。"""
        qkey = qkey_of(case)
        variant = case.get("variant") or {}
        tags = [t.get("tag") if isinstance(t, dict) else t for t in (case.get("tags") or [])]
        sets = ["uid=?", "case_id=?", "variant_index=?", "rewrite=?", "dim=?", "topic=?",
                "granularity=?", "tags_json=?", "input_fp=?"]
        args: list = [case.get("uid", "?"), case.get("case_id") or case.get("id"),
                      case.get("variant_index", 0), variant.get("rewrite"),
                      case.get("dim"), case.get("topic"), case.get("granularity"),
                      json.dumps(tags, ensure_ascii=False), input_fp_of(case)]
        if active is not None:
            sets.append("active=?")
            args.append(1 if active else 0)
        if current_standard_fp is not None:
            sets.append("current_standard_fp=?")
            args.append(current_standard_fp)
        with self.lock, self.conn:
            exists = self.conn.execute("SELECT current_standard_fp FROM questions WHERE qkey=?",
                                       (qkey,)).fetchone()
            if exists and current_if_absent is not None and current_standard_fp is None \
                    and not exists["current_standard_fp"]:
                sets.append("current_standard_fp=?")
                args.append(current_if_absent)
                if active is None:
                    sets.append("active=?")
                    args.append(1)
            args.append(qkey)
            if exists:
                self.conn.execute(f"UPDATE questions SET {', '.join(sets)} WHERE qkey=?", tuple(args))
            else:
                fp = current_standard_fp if current_standard_fp is not None else current_if_absent
                self.conn.execute(
                    "INSERT INTO questions(qkey, uid, case_id, variant_index, rewrite, dim, topic, granularity,"
                    " tags_json, input_fp, current_standard_fp, active, first_seen)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (qkey, args[0], args[1], args[2], args[3], args[4], args[5], args[6], args[7], args[8],
                     fp, 1 if (active is None and fp) or active else 0, now()))
        return qkey

    def register_spec(self, qkey: str, standard_fp: str, spec_json: dict, evalset_version: str) -> None:
        """登记一份标准（同一 qkey × standard_fp 只存一份，首次登记的版本标签不再改写），
        并写版本映射 spec_versions（R1：标准按指纹寻址，版本与标准多对多）。
        spec_json 落库前剥除权重（v8：权重只活在 item_weights，doc/15 §2.2）。"""
        spec_json = strip_spec_weights(dict(spec_json))
        spec_json.pop("weight_percent", None)
        spec_json.pop("weights", None)          # 新 dist 顶层 weights 只作导入初始值，不入标准
        ts = now()
        with self.lock, self.conn:
            self.conn.execute("INSERT OR IGNORE INTO specs VALUES(?,?,?,?,?)",
                              (qkey, standard_fp, json.dumps(spec_json, ensure_ascii=False),
                               evalset_version, ts))
            self.conn.execute("INSERT OR IGNORE INTO spec_versions VALUES(?,?,?,?)",
                              (qkey, standard_fp, evalset_version, ts))

    # ---------------------------------------------------------------- 条目权重（doc/15 §2.4/§3.5）

    def _case_seed(self, case_id: str) -> str | None:
        """用例的种子题 qkey（uid 最小者；页面改权重作用于整个用例，B1）。"""
        row = self.conn.execute(
            "SELECT qkey FROM questions WHERE case_id=? ORDER BY uid LIMIT 1", (case_id,)).fetchone()
        return row["qkey"] if row else None

    def _register_case_weights(self, case_id: str, seed_case: dict, version: str,
                               keep_weights: bool = False) -> dict:
        """导入时的权重冲突规则（B3，doc/15 §3.5）：与上一次导入的初始值相同 → 不写入
        （保留页面上改过的权重）；不同/首次 → 写入新的有效行并报告覆盖。返回报告项。"""
        new_w = spec_weights_any(seed_case)
        cid = case_id or seed_case.get("case_id") or seed_case.get("id") or "?"
        sub = self.conn.execute(
            "SELECT MAX(set_at) AS s FROM item_weights WHERE case_id=? AND source='import'", (cid,)
        ).fetchone()["s"]
        prev = {r["item_id"]: r["weight"] for r in self.conn.execute(
            "SELECT item_id, weight FROM item_weights WHERE case_id=? AND source='import' AND set_at=?",
            (cid, sub or ""))} if sub else {}
        if new_w and (not prev or prev != new_w):
            if keep_weights:
                return {"case_id": cid, "action": "kept (--keep-weights)", "changed": True}
            overwritten = self.conn.execute(
                "SELECT COUNT(*) AS n FROM item_weights WHERE case_id=? AND active=1 AND source != 'import'",
                (cid,)).fetchone()["n"]
            with self.lock, self.conn:
                self.conn.execute("UPDATE item_weights SET active=0 WHERE case_id=?", (cid,))
                for item_id, w in new_w.items():
                    self.conn.execute("INSERT INTO item_weights VALUES(?,?,?,?,?,?,?,?)",
                                      (cid, item_id, w, None, "import", version, now(), 1))
            self._refresh_weight_fp(cid)
            return {"case_id": cid, "action": "written", "overwrote_manual": overwritten}
        return {"case_id": cid, "action": "unchanged"}

    def _refresh_weight_fp(self, case_id: str) -> str:
        """刷新用例全部变体的 questions.current_weight_fp（S4 缓存列）。"""
        _, wfp, _ = self.effective_weights(case_id)
        with self.lock, self.conn:
            self.conn.execute("UPDATE questions SET current_weight_fp=? WHERE case_id=?",
                              (wfp, case_id))
        return wfp

    def effective_weights(self, case_id: str, standard_fp: str | None = None,
                          default_weight: float = 1.0) -> tuple[dict, str, bool]:
        """有效权重（doc/15 §2.4）→ (权重表, weight_fp, 权重不完整)。
        只取当前标准里还存在的条目；新标准多出的计分条目记 0 并标不完整；
        无任何权重行的用例 → ({}, 'equal', False)，按 default_weight 等权计分。"""
        seed = self._case_seed(case_id)
        if seed is None:
            return {}, "equal", False
        q = self.question(seed) or {}
        fp = standard_fp or q.get("current_standard_fp")
        spec = self.spec_of(seed, fp) if fp else None
        items: list[tuple[str, str]] = []
        if spec:
            items += [(p.get("id"), "precond") for p in spec.get("preconditions") or []]
            items += [(f"path#{i}", "path") for i in range(1, len(spec.get("path") or []) + 1)]
            items += [(f.get("id"), f.get("kind")) for f in spec.get("facts") or []]
        latest: dict[str, float] = {}
        for r in self.conn.execute(
                "SELECT item_id, weight FROM item_weights WHERE case_id=? AND active=1"
                " ORDER BY set_at, rowid", (case_id,)):
            latest[r["item_id"]] = float(r["weight"])       # 后写的覆盖先写的
        if not latest:
            return {}, "equal", False
        known = {i for i, _ in items}
        weights: dict[str, float] = {}
        incomplete = False
        for item_id, kind in items:
            if item_id in latest:
                weights[item_id] = latest[item_id]
            elif kind in ("required", "expected"):
                weights[item_id] = 0.0                       # 新条目没分到权重（记 0 + 标记）
                incomplete = True
        return weights, (canonical_sha(weights) if weights else "equal"), incomplete

    def revise_weights(self, case_id: str, weights: dict[str, float], label: str = "",
                       by: str = "web") -> dict:
        """页面上按用例保存权重（B1：同一用例各变体共用；doc/15 §3.5）：校验（只收
        计分条目、0–100、总和 ≤100、必须覆盖全部计分条目）→ 追加有效行 → 刷新
        weight_fp。只重算本用例（rescore_cases），不再产生新标准、不再需要 rejudge。"""
        seed = self._case_seed(case_id)
        if seed is None:
            raise StoreError(f"用例不存在：{case_id}")
        q = self.question(seed) or {}
        fp = q.get("current_standard_fp")
        spec = self.spec_of(seed, fp) if fp else None
        if spec is None:
            raise StoreError(f"用例没有当前标准：{case_id}")
        scored = {f.get("id") for f in spec.get("facts") or []
                  if f.get("kind") in ("required", "expected")}
        unknown = [k for k in weights if k not in scored]
        missing = sorted(scored - set(weights))
        if unknown:
            raise StoreError(f"不可调权重的条目：{unknown}（只有必要/期望事实按权重计分）")
        if missing:
            raise StoreError(f"必须覆盖全部计分条目（缺 {missing}）；不想计分的条目写 0")
        for k, v in weights.items():
            if not isinstance(v, (int, float)) or not 0 <= float(v) <= 100:
                raise StoreError(f"权重应为 0–100 的百分比：{k} = {v!r}")
        total = sum(float(v) for v in weights.values())
        if total > 100 + 1e-9:
            raise StoreError(f"权重总和 {total:.0f}% 超过 100%")
        lab = (label or "").strip()[:40] or None
        with self.lock, self.conn:
            self.conn.execute("UPDATE item_weights SET active=0 WHERE case_id=?", (case_id,))
            ts = now()
            for item_id, w in weights.items():
                self.conn.execute("INSERT INTO item_weights VALUES(?,?,?,?,?,?,?,?)",
                                  (case_id, item_id, round(float(w), 4), lab, "web", by, ts, 1))
        wfp = self._refresh_weight_fp(case_id)
        return {"case_id": case_id, "weight_fp": wfp, "changed": True}

    def weights_touched_after(self, answer_id: str) -> bool:
        """人工总分之后权重或计分规则改过（C4 提示）。"""
        a = self.answer(answer_id)
        if a is None:
            return False
        at = self.conn.execute(
            "SELECT MAX(at) AS at FROM reviews WHERE answer_id=? AND item_id=?",
            (answer_id, TOTAL_ITEM)).fetchone()["at"]
        if at is None:
            return False
        q_row = self.question(a["qkey"]) or {}
        w = self.conn.execute(
            "SELECT MAX(set_at) AS s FROM item_weights WHERE case_id=?",
            (q_row.get("case_id") or "",)).fetchone()["s"]
        rules_at = self.conn.execute("SELECT MAX(created_at) AS s FROM rules").fetchone()["s"]
        return any(x and x > at for x in (w, rules_at))

    def weight_history(self, case_id: str) -> list[dict]:
        """按用例列出历次权重保存（最新在前）：同一 set_at+标签+来源+操作人视为
        一次保存。每次给出该组原始权重和「若此刻生效的 weight_fp」（与
        effective_weights 同式：按当前标准条目过滤，计分条目缺行记 0）。"""
        seed = self._case_seed(case_id)
        if seed is None:
            return []
        q = self.question(seed) or {}
        spec = self.spec_of(seed, q.get("current_standard_fp")) if q.get("current_standard_fp") else None
        items: list[tuple[str, str]] = []
        if spec:
            items += [(p.get("id"), "precond") for p in spec.get("preconditions") or []]
            items += [(f"path#{i}", "path") for i in range(1, len(spec.get("path") or []) + 1)]
            items += [(f.get("id"), f.get("kind")) for f in spec.get("facts") or []]
        known = {i for i, _ in items}
        groups: dict[tuple, dict] = {}
        order: list[tuple] = []
        for r in self.conn.execute(
                "SELECT item_id, weight, label, source, set_by, set_at, active FROM item_weights"
                " WHERE case_id=? ORDER BY set_at DESC, rowid DESC", (case_id,)):
            k = (r["set_at"], r["label"] or "", r["source"], r["set_by"] or "")
            if k not in groups:
                groups[k] = {"set_at": r["set_at"], "label": r["label"], "source": r["source"],
                             "set_by": r["set_by"], "active": False, "weights": {}}
                order.append(k)
            g = groups[k]
            g["weights"][r["item_id"]] = round(float(r["weight"]), 4)
            g["active"] = g["active"] or bool(r["active"])
        out = []
        for k in order:
            g = dict(groups[k])
            w = {i: v for i, v in g["weights"].items() if i in known}
            for item_id, kind in items:
                if kind in ("required", "expected") and item_id not in w:
                    w[item_id] = 0.0
            g["weight_fp"] = canonical_sha(w) if w else "equal"
            out.append(g)
        return out

    def case_conclusions_under_weight(self, case_id: str, weight_fp: str, chain_sha: str,
                                      rules_sha: str) -> list[dict]:
        """旧权重口径下本用例的会话结论（直读 conclusions 现存行，不重算）：
        标准按各题当前标准联接——标准或链/规则已变的题在该权重下没有现成
        结论（status 为 NULL），由页面提示需要重算。"""
        rows = self.conn.execute(
            "SELECT a.answer_id, a.mode, a.trial, q.uid, q.variant_index, q.rewrite,"
            " c.status, c.score, c.score_source, c.score_min, c.score_max"
            " FROM answers a JOIN questions q ON q.qkey=a.qkey"
            " LEFT JOIN conclusions c ON c.answer_id=a.answer_id AND c.chain_sha=?"
            "  AND c.rules_sha=? AND c.weight_fp=? AND c.standard_fp=q.current_standard_fp"
            " WHERE q.case_id=? ORDER BY q.variant_index, a.mode, a.trial",
            (chain_sha, rules_sha, weight_fp, case_id)).fetchall()
        return [dict(r) for r in rows]

    def _load_method(self, method: dict | None) -> tuple[dict, str]:
        """测法：调用方给定 → 用之；否则读仓库 evalset/method.yaml（P1 导入即读，doc/15
        §3.9）；再缺省给最小测法。返回 (method_json 字符串, method_sha)。"""
        if method is None:
            import pathlib
            try:
                import yaml
                f = pathlib.Path(__file__).resolve().parents[1] / "evalset" / "method.yaml"
                method = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            except Exception:
                method = {}
            if not method.get("modes"):
                method = {"modes": {"default": "natural"}, "run_rewrites": [], "anchors": []}
        method = {"modes": method.get("modes") or {"default": "natural"},
                  "run_rewrites": list(method.get("run_rewrites") or []),
                  "anchors": list(method.get("anchors") or [])}
        js = json.dumps(method, ensure_ascii=False, sort_keys=True)
        return js, canonical_sha(method)

    def _register_snapshot(self, version: str, qkeys: list[str], method: dict | None = None,
                           include: str = "admitted",
                           weights_by_case: dict[str, dict] | None = None) -> str:
        """登记/刷新快照（snapshot_sha 单一算法，S2：迁移与导入同式，doc/15 §1）。
        weights_by_case：导入原始行的权重（specs 表已剥除，不能从表里取）。"""
        method_js, method_sha = self._load_method(method)
        rows_v, wmap = [], dict(weights_by_case or {})
        for qkey in qkeys:
            q = self.question(qkey) or {}
            sv = self.conn.execute(
                "SELECT standard_fp, spec_json FROM specs WHERE qkey=? AND evalset_version=?",
                (qkey, version)).fetchone()
            if sv is None:
                continue
            rows_v.append([q.get("uid") or "?", q.get("input_fp") or "", sv["standard_fp"]])
            if not wmap and (q.get("variant_index") or 0) == 0:
                w = spec_weights_any(json.loads(sv["spec_json"] or "{}"))
                if w:
                    wmap[q.get("case_id") or "?"] = w
        snap_sha = canonical_sha({"rows": sorted(rows_v), "weights": wmap, "method_sha": method_sha})
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO snapshots VALUES(?,?,?,?,?,?,?)"
                " ON CONFLICT(evalset_version) DO UPDATE SET snapshot_sha=excluded.snapshot_sha,"
                " method_json=excluded.method_json, method_sha=excluded.method_sha,"
                " include=excluded.include",
                (version, snap_sha, method_js, method_sha, include, None, now()))
        return snap_sha

    def import_evalset(self, rows: list[dict], version: str, keep_weights: bool = False,
                       method: dict | None = None) -> dict:
        """specs import：登记全部标准并把该版本设为当前快照；未出现的题保留旧标准、
        标记非活跃。权重按冲突规则处理（B3）；报告覆盖手工修改/无归属/不完整的用例。"""
        qkeys: dict[str, str] = {}
        seeds: dict[str, dict] = {}
        with self.lock, self.conn:
            for case in rows:
                qkey = qkey_of(case)
                fp = standard_fp_of(case)
                self.upsert_question(case)
                self.register_spec(qkey, fp, case, version)
                qkeys[qkey] = fp
                cid = case.get("case_id") or case.get("id") or "?"
                if (case.get("variant_index") or 0) == 0 or cid not in seeds:
                    seeds[cid] = case
            self.conn.execute("UPDATE questions SET active=0")
            for qkey, fp in qkeys.items():
                self.conn.execute("UPDATE questions SET active=1, current_standard_fp=?, off_snapshot=0"
                                  " WHERE qkey=?", (fp, qkey))
            weight_report = [self._register_case_weights(cid, seed, version, keep_weights)
                             for cid, seed in seeds.items()]
            seed_weights = {cid: spec_weights_any(seed) for cid, seed in seeds.items()}
            seed_weights = {cid: w for cid, w in seed_weights.items() if w}
            for q in self.questions():
                if q["qkey"] in qkeys:
                    self._refresh_weight_fp(q["case_id"])
            self._register_snapshot(version, list(qkeys), method, weights_by_case=seed_weights)
            self.conn.execute("INSERT OR REPLACE INTO meta VALUES('current_snapshot', ?)", (version,))
        incomplete = []
        for cid in seeds:
            _, _, inc = self.effective_weights(cid)
            if inc:
                incomplete.append(cid)
        return {"questions": len(qkeys), "version": version, "weights": weight_report,
                "weights_incomplete": incomplete}

    def set_current(self, version: str, qkey: str | None = None) -> int:
        """specs set-current（语义收窄，doc/15 §3.8）：给 --question 时做逐题偏离实验
        （该题当前标准改为该版本的标准，off_snapshot=1）；不给 qkey 时等价 snapshot use。"""
        with self.lock, self.conn:
            rows = self.conn.execute(
                "SELECT qkey, standard_fp FROM spec_versions WHERE evalset_version=?", (version,)).fetchall()
            if not rows:
                raise StoreError(f"未知评测集版本：{version}")
            if qkey is None:
                return self.snapshot_use(version)
            fp = next((r["standard_fp"] for r in rows if r["qkey"] == qkey), None)
            if fp is None:
                raise StoreError(f"版本 {version} 不含题目：{qkey}")
            self.conn.execute("UPDATE questions SET current_standard_fp=?, off_snapshot=1, active=1"
                              " WHERE qkey=?", (fp, qkey))
            self._refresh_weight_fp(self.question(qkey)["case_id"])
            return 1

    def snapshot_use(self, version: str) -> int:
        """把某快照设为当前（doc/15 §1.1）：逐题当前标准对齐该版本、清偏离标记；
        不在版本里的题保留旧标准、置非活跃。"""
        with self.lock, self.conn:
            rows = self.conn.execute(
                "SELECT qkey, standard_fp FROM spec_versions WHERE evalset_version=?", (version,)).fetchall()
            if not rows:
                raise StoreError(f"未知快照：{version}")
            snap = self.conn.execute("SELECT 1 FROM snapshots WHERE evalset_version=?",
                                     (version,)).fetchone()
            if snap is None:                     # 旧库导入过但没生成快照行（迁移前的版本）
                self._register_snapshot(version, [r["qkey"] for r in rows])
            self.conn.execute("UPDATE questions SET active=0, off_snapshot=0")
            for r in rows:
                self.conn.execute("UPDATE questions SET active=1, current_standard_fp=? WHERE qkey=?",
                                  (r["standard_fp"], r["qkey"]))
            for cid in {self.question(r["qkey"])["case_id"] for r in rows}:
                if cid:
                    self._refresh_weight_fp(cid)
            self.conn.execute("INSERT OR REPLACE INTO meta VALUES('current_snapshot', ?)", (version,))
            return len(rows)

    def current_snapshot(self) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key='current_snapshot'").fetchone()
        return row["value"] if row else None

    def snapshots(self) -> list[dict]:
        out = []
        for r in self.conn.execute("SELECT * FROM snapshots ORDER BY imported_at, evalset_version"):
            d = dict(r)
            d["method"] = json.loads(d.pop("method_json") or "{}")
            d["questions"] = self.conn.execute(
                "SELECT COUNT(*) AS n FROM spec_versions WHERE evalset_version=?",
                (d["evalset_version"],)).fetchone()["n"]
            out.append(d)
        return out

    def snapshot(self, version: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM snapshots WHERE evalset_version=?", (version,)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["method"] = json.loads(d.pop("method_json") or "{}")
        return d

    def rename_snapshot(self, version: str, name: str) -> None:
        n = str(name or "").strip()[:60]
        with self.lock, self.conn:
            cur = self.conn.execute("UPDATE snapshots SET name=? WHERE evalset_version=?",
                                    (n or None, version))
            if cur.rowcount == 0:
                raise StoreError(f"快照不存在：{version}")

    def rules_rows(self) -> list[dict]:
        out = []
        for r in self.conn.execute("SELECT * FROM rules ORDER BY created_at, rules_sha"):
            d = dict(r)
            d["rules"] = json.loads(d.pop("rules_json") or "{}")
            out.append(d)
        return out

    def register_rules(self, rules_sha: str, rules_json: dict, name: str | None = None) -> None:
        with self.lock, self.conn:
            self.conn.execute("INSERT OR IGNORE INTO rules VALUES(?,?,?,?)",
                              (rules_sha, json.dumps(rules_json, ensure_ascii=False, sort_keys=True),
                               name, now()))

    def rename_rules(self, rules_sha: str, name: str) -> None:
        n = str(name or "").strip()[:60]
        with self.lock, self.conn:
            cur = self.conn.execute("UPDATE rules SET name=? WHERE rules_sha=?", (n or None, rules_sha))
            if cur.rowcount == 0:
                raise StoreError(f"计分规则不存在：{rules_sha}")

    def meta_get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

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
        """(standard_fp, 编译行) of 当前标准。"""
        q = self.question(qkey)
        if not q or not q["current_standard_fp"]:
            return None
        spec = self.spec_of(qkey, q["current_standard_fp"])
        if spec is None:
            return None
        return q["current_standard_fp"], spec

    def spec_of(self, qkey: str, standard_fp: str) -> dict | None:
        row = self.conn.execute("SELECT spec_json FROM specs WHERE qkey=? AND standard_fp=?",
                                (qkey, standard_fp)).fetchone()
        return json.loads(row["spec_json"]) if row else None

    # ---------------------------------------------------------------- run 与答案（C8）

    def register_run(self, run_id: str, run_dir: str | Path, manifest: dict | None) -> None:
        snap = (manifest or {}).get("snapshot") or {}
        profile_id = profile_id_of(manifest)
        snapshot_json = json.dumps(manifest or {}, ensure_ascii=False)
        # started_at 只认 run.json 的真实值；缺失时首次导入以导入时间回填落列（与 v3
        # 迁移同语义），重导沿用已落列的值、真实值一旦出现即覆盖——不会被反复刷新。
        # title（下发时 --title 命名）同理：manifest 带标题就以它为准，不带则保留
        # 已有值——页面上手工改的名不会被无标题的重导冲掉。
        started_at = (manifest or {}).get("started_at")
        title = str((manifest or {}).get("title") or "").strip() or None
        # 被测指纹（doc/15 §3.1）：runner P1 起在 run.json 带 subject 段（TS 算）；
        # 缺 subject 段的旧格式在 Python 侧现算，保证两端一致（fixture 对拍）。
        subject = (manifest or {}).get("subject")
        if subject:
            sfp = subject.get("subject_fp")
            sjs = json.dumps(subject.get("fields") or {}, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"))
            incomplete = 1 if subject.get("incomplete") else 0
        else:
            sfp, sjs, incomplete = subject_fp_of_manifest(manifest or {})
        method_sha = ((manifest or {}).get("method") or {}).get("method_sha")
        with self.lock, self.conn:
            if started_at is None:
                row = self.conn.execute("SELECT started_at FROM runs WHERE run_id=?", (run_id,)).fetchone()
                started_at = row["started_at"] if row else now()
            self.conn.execute(
                "INSERT INTO runs(run_id, run_dir, evalset_version, profile_id, snapshot_json, imported_at,"
                " started_at, title, subject_fp, subject_json, subject_incomplete, method_sha, diagnostic)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(run_id) DO UPDATE SET run_dir=excluded.run_dir,"
                " evalset_version=excluded.evalset_version, profile_id=excluded.profile_id,"
                " snapshot_json=excluded.snapshot_json, started_at=excluded.started_at,"
                " title=COALESCE(excluded.title, title), subject_fp=excluded.subject_fp,"
                " subject_json=excluded.subject_json, subject_incomplete=excluded.subject_incomplete,"
                " method_sha=excluded.method_sha",
                (run_id, str(run_dir), evalset_version_of(manifest), profile_id, snapshot_json, now(),
                 started_at, title, sfp, sjs, incomplete, method_sha,
                 1 if (manifest or {}).get("diagnostic") else 0))

    def ingest_answer(self, record: dict, run_id: str, manifest: dict | None) -> str:
        """入库一条答案：完整 EpisodeRecord 随行保存（run 目录移走后仍可重判），
        并登记执行时所用的标准（含权重初始行，R5 空库路径）。answer_id = run_id/episode_id。
        题目尚无当前标准时以执行时标准设为当前并置活跃（R5）。
        注入 found>1 与变体泄露嫌疑在入库时预计算存列（R7）；
        被测指纹与拆字段列从 run 的 subject 回填（对比页免 join，doc/15 §2.3）。"""
        case = record.get("case") or {}
        qkey = qkey_of(case)
        fp = standard_fp_of(case)
        self.upsert_question(case, current_if_absent=fp)
        self.register_spec(qkey, fp, case, evalset_version_of(manifest))
        if (case.get("variant_index") or 0) == 0:
            self._register_case_weights(case.get("case_id") or case.get("id") or "?", case,
                                         evalset_version_of(manifest))
            self._refresh_weight_fp(case.get("case_id") or case.get("id") or "?")
        snap = (manifest or {}).get("snapshot") or {}
        models = snap.get("models_observed")
        run = self.run(run_id) or {}
        subject = json.loads(run.get("subject_json") or "{}") if run.get("subject_json") else {}
        agent = subject.get("agent") or {}
        episode_id = record.get("episode_id") or f"{record.get('uid', '?')}-{record.get('mode', '?')}-t{record.get('trial', 0)}"
        answer_id = answer_id_of(run_id, episode_id)
        found_gt1 = any((a or {}).get("found", 1) > 1 for a in record.get("injections_applied") or [])
        leak_json = json.dumps(variant_leak_suspects([record]), ensure_ascii=False)
        observed = record.get("injection_observed")
        self._exec(
            "INSERT OR REPLACE INTO answers(answer_id, qkey, run_id, episode_id, judge_fp_at_run,"
            " profile_id, models_json, mode, trial, skills_mode, skills_base, evalset_version,"
            " run_status, record_json, injection_found_gt1, variant_leak_json, injection_observed_json,"
            " subject_fp, model, reasoning_effort, agent)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (answer_id, qkey, run_id, episode_id, fp,
             profile_id_of(manifest),
             json.dumps(models, ensure_ascii=False) if models is not None else None,
             record.get("mode", "?"), record.get("trial", 0),
             snap.get("skills_mode"), snap.get("skills_base"), evalset_version_of(manifest),
             record.get("status", "?"), json.dumps(record, ensure_ascii=False),
             1 if found_gt1 else 0, leak_json,
             json.dumps(observed, ensure_ascii=False) if observed is not None else None,
             run.get("subject_fp"),
             subject.get("model") or (models or [None])[0],
             subject.get("reasoning_effort"),
             f"{agent.get('name')}" + (f"@{agent['version']}" if agent.get("version") else "")))
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

    def run_labels(self) -> dict[str, str]:
        """run 的展示名（命名改造）：自定义标题优先；否则按已知条件拼默认
        「评测集版本 · profile · modes×trials」。标题是纯展示层元数据，
        不参与任何键与比较轴；run_id 始终是机器身份。"""
        out: dict[str, str] = {}
        for r in self.conn.execute("SELECT run_id, title, evalset_version, profile_id, snapshot_json FROM runs"):
            title = (r["title"] or "").strip()
            if title:
                out[r["run_id"]] = title
                continue
            try:
                m = json.loads(r["snapshot_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                m = {}
            # run.json 的 modes/trials 在顶层（真实 run）；缺省夹具/旧数据只有 snapshot 内的
            inner = m.get("snapshot") if isinstance(m.get("snapshot"), dict) else {}
            modes = "+".join(str(x) for x in (m.get("modes") or inner.get("modes") or []))
            trials = m.get("trials") or inner.get("trials")
            mode_part = f"{modes}×{trials}" if modes and trials else (modes or None)
            parts = [version_label(r["evalset_version"]), r["profile_id"], mode_part]
            out[r["run_id"]] = " · ".join(p for p in parts if p) or r["run_id"]
        return out

    def rename_run(self, run_id: str, title: str) -> None:
        """改执行记录标题；空串 = 清除（回默认拼接标题）。run_id 不变。"""
        t = str(title or "").strip()[:60]
        with self.lock, self.conn:
            cur = self.conn.execute("UPDATE runs SET title=? WHERE run_id=?",
                                    (t or None, run_id))
            if cur.rowcount == 0:
                raise StoreError(f"run 不存在：{run_id}")

    def rename_chain(self, chain_sha: str, name: str) -> None:
        """改判定配置（评分链）名称；空串 = 清除。chain_sha 不变。"""
        n = str(name or "").strip()[:60]
        with self.lock, self.conn:
            cur = self.conn.execute("UPDATE chains SET name=? WHERE chain_sha=?",
                                    (n or None, chain_sha))
            if cur.rowcount == 0:
                raise StoreError(f"判定配置不存在：{chain_sha}")

    # ---------------------------------------------------------------- 判定入库（C8/C10）

    def write_verdicts(self, answer_id: str, standard_fp: str, chain_sha: str, rows: list[dict],
                       judged_at: str | None = None) -> int:
        """按 (答案, 标准, 链配置) 写入一批机器判定行（整会话覆盖写，可重复执行）。"""
        ts = judged_at or now()
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM verdicts WHERE answer_id=? AND standard_fp=? AND chain_sha=?",
                              (answer_id, standard_fp, chain_sha))
            for r in rows:
                self.conn.execute(
                    "INSERT OR REPLACE INTO verdicts(answer_id, standard_fp, chain_sha, item_id, kind, turn,"
                    " item_sha, answer_sha, value, source, operator, reason, trace_json, status, judged_at, score)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (answer_id, standard_fp, chain_sha, r["item"]["id"], r["item"]["kind"], r["item"]["turn"],
                     r.get("item_sha"), r["answer_sha"], r["machine"].get("value"), r["machine"].get("source", ""),
                     r["machine"].get("operator", ""), r["machine"].get("reason", ""),
                     json.dumps(r.get("trace") or [], ensure_ascii=False), r["status"], ts,
                     r["machine"].get("score")))
        return len(rows)

    def verdicts(self, answer_id: str, standard_fp: str, chain_sha: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM verdicts WHERE answer_id=? AND standard_fp=? AND chain_sha=? ORDER BY item_id",
            (answer_id, standard_fp, chain_sha))]

    def has_verdicts(self, answer_id: str, standard_fp: str, chain_sha: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM verdicts WHERE answer_id=? AND standard_fp=? AND chain_sha=? LIMIT 1",
            (answer_id, standard_fp, chain_sha)).fetchone()
        return row is not None

    def pending_rejudge(self, chain_sha: str, qkey: str | None = None, run_id: str | None = None) -> list[dict]:
        """缺"当前期望 + 当前链配置"判定的答案。无条目的会话（运行错误）不需要判定，
        直接写结论，不算待重判。一条 SQL 找出缺判定的答案，只对它们解析 record。"""
        sql = """SELECT a.answer_id, a.qkey, a.run_id, a.episode_id, q.current_standard_fp AS fp,
                 a.record_json, s.spec_json
                 FROM answers a JOIN questions q ON a.qkey = q.qkey
                 JOIN specs s ON s.qkey = a.qkey AND s.standard_fp = q.current_standard_fp
                 WHERE q.current_standard_fp IS NOT NULL
                   AND NOT EXISTS (SELECT 1 FROM verdicts v WHERE v.answer_id = a.answer_id
                        AND v.standard_fp = q.current_standard_fp AND v.chain_sha = ?)"""
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
                out.append({"answer_id": r["answer_id"], "qkey": r["qkey"], "standard_fp": r["fp"],
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
                      standard_fp: str, chain_sha: str, rules: Rules, rules_sha: str,
                      score: float | None = None) -> tuple[str, str]:
        """单条改判（兼容入口）：等价于只含一条的 submit_reviews。"""
        c = self.submit_reviews(answer_id, [{"item_id": item_id, "value": value, "score": score,
                                             "reason": reason}], None, reviewer, standard_fp,
                                chain_sha, rules, rules_sha)
        return c["status"], c.get("reason") or ""

    def _validate_entries(self, answer_id: str, entries: list[dict], total: dict | None,
                          standard_fp: str, chain_sha: str) -> list[tuple]:
        """校验一批人工打分（不写库）；返回待写入行 (item_id, value, score, reason, item_sha, answer_sha)。
        任一条不合法抛 StoreError（带 item_id），整批不写。"""
        a = self.answer(answer_id)
        if a is None:
            raise StoreError("答案不存在")
        vrows = {r["item_id"]: r for r in self.verdicts(answer_id, standard_fp, chain_sha)}
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
            out.append((TOTAL_ITEM, None, float(score), total["reason"].strip(), standard_fp,
                        self._conversation_sha(a, standard_fp)))
        return out

    def submit_reviews(self, answer_id: str, entries: list[dict], total: dict | None, reviewer: str,
                       standard_fp: str, chain_sha: str, rules: Rules, rules_sha: str) -> dict:
        """一段对话的批量人工打分（复核服务改造方案 §6.4）：全部校验 → 同一事务追加 →
        重算一次该答案的结论。任一条不合法则整体不写入；重算失败时追加的记录一并
        回滚，不留人工记录 + 旧结论的中间态。返回结论行。"""
        rows = self._validate_entries(answer_id, entries, total, standard_fp, chain_sha)
        ts = now()
        with self.lock, self.conn:
            for item_id, value, score, reason, item_sha, ans_sha in rows:
                self.conn.execute(
                    "INSERT INTO reviews(answer_id, item_id, item_sha, answer_sha, value, reason, reviewer, at, score)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (answer_id, item_id, item_sha, ans_sha, value, reason, reviewer, ts, score))
            v, wfp = self._conclude(answer_id, standard_fp, chain_sha, rules)
            self._write_conclusion(self.conn, answer_id, standard_fp, chain_sha, rules_sha, wfp, v)
        return self.conclusion(answer_id, standard_fp, chain_sha, rules_sha, wfp) or {}

    def preview_conclusion(self, answer_id: str, entries: list[dict], total: dict | None,
                           standard_fp: str, chain_sha: str, rules: Rules) -> EpisodeVerdict:
        """按草稿重算结论（不落库）：草稿视作排在全部已有改判之后的人工记录。"""
        rows = self._validate_entries(answer_id, entries, total, standard_fp, chain_sha)
        drafts = [{"item_id": item_id, "value": value, "score": score, "reason": reason,
                   "item_sha": item_sha, "answer_sha": ans_sha, "review_id": float("inf")}
                  for item_id, value, score, reason, item_sha, ans_sha in rows]
        v, _ = self._conclude(answer_id, standard_fp, chain_sha, rules, extra_reviews=drafts)
        return v

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

    def _conversation_sha(self, a: dict, standard_fp: str) -> str:
        spec = self.spec_of(a["qkey"], standard_fp)
        return conversation_sha(build_context(json.loads(a["record_json"]), spec))

    @staticmethod
    def total_override(reviews: list[dict], standard_fp: str, conv_sha: str) -> dict | None:
        """有效人工会话分：最后一条 item_id=@total、item_sha=standard_fp、answer_sha=整段回答
        指纹、分数合法且有理由的记录（标准或回答变了即作废；权重不再使其作废，C4）。"""
        valid = [r for r in reviews
                 if r["item_id"] == TOTAL_ITEM and r["item_sha"] == standard_fp
                 and r["answer_sha"] == conv_sha
                 and r.get("score") is not None and _valid_score(r["score"]) and (r["reason"] or "").strip()]
        if not valid:
            return None
        r = valid[-1]
        return {"score": float(r["score"]), "reason": r["reason"], "reviewer": r.get("reviewer"), "at": r.get("at")}

    def effective_rows(self, answer_id: str, standard_fp: str, chain_sha: str) -> list[dict]:
        """(verdict 行, 有效结论) 对：所有展示与统计的唯一取数口。"""
        reviews = self.reviews_for(answer_id)
        out = []
        for vrow in self.verdicts(answer_id, standard_fp, chain_sha):
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
                    out.append({"review": r, "reason": "标准已修订（人工会话分作废）"})
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

    def _chain_shas_of(self, answer_id: str, standard_fp: str) -> list[str]:
        return [r["chain_sha"] for r in self.conn.execute(
            "SELECT DISTINCT chain_sha FROM verdicts WHERE answer_id=? AND standard_fp=?",
            (answer_id, standard_fp))]

    # ---------------------------------------------------------------- 复核队列取数（R2）

    def undecided_items(self, chain_sha: str, run_id: str | None = None,
                        page: int = 0, size: int = 20) -> tuple[list[dict], int]:
        """未决队列（§6.3：按有效结论取数）：机器结论为空或异常、且没有效人工改判
        （answer_sha + item_sha 匹配、取值在 scale 内）的参与条目。已被短路的条目
        （前提判不成立/路径违反后的事实、forced 会话的路径、权重 0 的事实）不进队列。
        期望事实按权重计分（doc/10 §4.2），未决时同样进队列。"""
        sql = """SELECT v.answer_id, v.item_id, v.kind, v.turn, v.value AS machine_value,
                 v.source AS machine_source, v.reason AS machine_reason, v.status,
                 q.uid, q.case_id, a.run_id, a.episode_id, a.mode, a.trial, a.qkey,
                 q.current_standard_fp,
                 (SELECT r.value FROM reviews r WHERE r.answer_id = v.answer_id AND r.item_id = v.item_id
                    AND r.answer_sha = v.answer_sha AND r.item_sha = v.item_sha
                    ORDER BY r.review_id DESC LIMIT 1) AS review_value
                 FROM verdicts v JOIN answers a USING(answer_id) JOIN questions q ON a.qkey = q.qkey
                 WHERE q.current_standard_fp = v.standard_fp AND v.chain_sha = ?
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
                    f"SELECT answer_id, standard_fp, item_id, kind, value FROM verdicts"
                    f" WHERE answer_id IN ({qmarks_a}) AND chain_sha = ?", (*aids, chain_sha)):
                vr_by_answer.setdefault(v["answer_id"], []).append(dict(v))
            qkeys = {c["qkey"] for c in cands}
            qmarks_q = ",".join("?" * len(qkeys))
            specs = {(r["qkey"], r["standard_fp"]): json.loads(r["spec_json"])
                     for r in self.conn.execute(
                         f"SELECT qkey, standard_fp, spec_json FROM specs WHERE qkey IN ({qmarks_q})",
                         tuple(qkeys))}
            zero_weight: set[str] = set()
            for cid in {c.get("case_id") for c in cands if c.get("case_id")}:
                w, _, _ = self.effective_weights(cid)
                zero_weight |= {i for i, x in w.items() if x <= 0}
            for a_id, items_ in by_answer.items():
                first = items_[0]
                forced = first["mode"] == "forced"
                fp = first["current_standard_fp"]
                spec = specs.get((first["qkey"], fp)) or {}
                answerable = spec.get("answerable", True) is not False
                rows_ = [v for v in vr_by_answer.get(a_id, []) if v["standard_fp"] == fp]
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

    def human_machine_compare(self, chain_sha: str, operator: str | None = None) -> tuple[list[dict], int, int]:
        """人工与机器不一致清单 + 规则一致率（一条 SQL 聚合，R7）：
        只看有人工改判的条目；每条目取最后一条有效改判（sha 匹配 + 取值合法）。
        operator 非空时只看该算子的条目（判定质量页分歧链接）。返回 (mismatch 列表, rule_agree, rule_total)。"""
        sql = """SELECT v.answer_id, v.item_id, v.kind, v.value AS machine_value, v.source AS machine_source,
                 v.operator, q.uid, r.value AS review_value, r.review_id
                 FROM verdicts v
                 JOIN answers a ON a.answer_id = v.answer_id
                 JOIN questions q ON q.qkey = a.qkey AND q.current_standard_fp = v.standard_fp
                 JOIN reviews r ON r.answer_id = v.answer_id AND r.item_id = v.item_id
                    AND r.answer_sha = v.answer_sha AND r.item_sha = v.item_sha
                 WHERE v.chain_sha = ?"""
        args: tuple = (chain_sha,)
        if operator:
            sql += " AND v.operator = ?"
            args += (operator,)
        best: dict[tuple, dict] = {}
        for row in self.conn.execute(sql, args):
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

    def _conclude(self, answer_id: str, standard_fp: str, chain_sha: str, rules: Rules,
                  extra_reviews: list[dict] | None = None) -> tuple[EpisodeVerdict, str]:
        """计算一个 (答案, 标准, 链配置, 计分规则, 权重) 的会话结论与会话分（不写库、
        不调用算子）。有效权重按用例解析（doc/15 §3.5），返回 (结论, weight_fp)。"""
        a = self.answer(answer_id)
        if a is None:
            raise StoreError(f"答案不存在：{answer_id}")
        spec_row = self.conn.execute("SELECT spec_json FROM specs WHERE qkey=? AND standard_fp=?",
                                     (a["qkey"], standard_fp)).fetchone()
        if spec_row is None:
            raise StoreError(f"标准不存在：qkey={a['qkey']} standard_fp={standard_fp}")
        record = json.loads(a["record_json"])
        spec = json.loads(spec_row["spec_json"])
        ctx = build_context(record, spec)
        manifest = json.loads((self.run(a["run_id"]) or {}).get("snapshot_json") or "{}")
        routing = ((manifest.get("profile") or {}).get("content") or {}).get("routing")
        err = episode_error(record, ctx)
        if err is not None:
            return EpisodeVerdict(uid=record.get("uid", "?"), mode=record.get("mode", "?"),
                                  trial=record.get("trial", 0), dim=spec.get("dim", "?"),
                                  status="error", error=err), "equal"
        items = extract_items(ctx)
        reviews = self.reviews_for(answer_id) + list(extra_reviews or [])
        vrows = {r["item_id"]: r for r in self.verdicts(answer_id, standard_fp, chain_sha)}
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
        override = self.total_override(reviews, standard_fp, conversation_sha(ctx))
        q_row = self.question(a["qkey"]) or {}
        weights, weight_fp, _ = self.effective_weights(q_row.get("case_id") or "", standard_fp)
        return session_conclusion(record, ctx, items, eff, rules, routing, statuses=statuses,
                                  override=override, weights=weights or None), weight_fp

    @staticmethod
    def _write_conclusion(conn, answer_id: str, standard_fp: str, chain_sha: str,
                          rules_sha: str, weight_fp: str, v: EpisodeVerdict) -> None:
        """物化一条结论行（不开事务，供 submit_reviews 在同一事务内复用）。
        键 = (答案, 标准, 链配置, 计分规则, 权重) 五段（doc/15 §2.2）。"""
        conn.execute("INSERT OR REPLACE INTO conclusions(answer_id, standard_fp, chain_sha, rules_sha,"
                     " weight_fp, status, reason, detail_json,"
                     " score, score_min, score_max, auto_score, score_source)"
                     " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (answer_id, standard_fp, chain_sha, rules_sha, weight_fp, v.status,
                      v.invalid_reason or v.error or "", json.dumps(v.as_dict(), ensure_ascii=False),
                      v.score, v.score_min, v.score_max, v.auto_score, v.score_source))

    def recompute_conclusion(self, answer_id: str, standard_fp: str, chain_sha: str,
                             rules: Rules, rules_sha: str) -> str:
        """重算并物化一个 (答案, 标准, 链配置, 规则, 权重) 的会话结论（不调用任何算子）。"""
        v, wfp = self._conclude(answer_id, standard_fp, chain_sha, rules)
        with self.lock, self.conn:
            self._write_conclusion(self.conn, answer_id, standard_fp, chain_sha, rules_sha, wfp, v)
        return v.status

    def rescore(self, rules: Rules, rules_sha: str, current_chain_sha: str | None = None,
                case_ids: list[str] | None = None) -> int:
        """只重算会话结论表（rescore）：对已有判定的全部 (答案, 标准, 链) 组合与当前
        标准下的无条目会话（运行错误）重算，不调用任何算子。case_ids 给定时只重算
        这些用例（rescore --case / 页面保存权重后，S5：所有有判定的链一起算）。"""
        keys = [(r["answer_id"], r["standard_fp"], r["chain_sha"])
                for r in self.conn.execute(
                    "SELECT DISTINCT answer_id, standard_fp, chain_sha FROM verdicts")]
        if case_ids is not None:
            case_by_answer = {}
            for a in self.conn.execute("SELECT answer_id, qkey FROM answers"):
                q = self.question(a["qkey"]) or {}
                case_by_answer[a["answer_id"]] = q.get("case_id")
            want = set(case_ids)
            keys = [k for k in keys if case_by_answer.get(k[0]) in want]
        n = 0
        for answer_id, fp, chain in keys:
            self.recompute_conclusion(answer_id, fp, chain, rules, rules_sha)
            n += 1
        if current_chain_sha is not None and case_ids is None:
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
                self.recompute_conclusion(a["answer_id"], fp, current_chain_sha, rules, rules_sha)
                n += 1
        return n

    def rescore_cases(self, case_ids: list[str], rules: Rules, rules_sha: str) -> int:
        """按用例重算（doc/15 §3.5 S5）：该用例的会话在所有有判定的链下、按当前计分
        规则重算。页面保存权重与 CLI rescore --case 走这里。"""
        return self.rescore(rules, rules_sha, case_ids=case_ids)

    def standard_compare(self, version_a: str, version_b: str, chain_sha: str,
                          rules_sha: str) -> dict:
        """标准对比（doc/15 §4.7 P2）：两快照共有题目上，同答案在两份标准下的结论
        diff。标准相同 → 提示无差异；某侧缺结论（未在该标准下判过）→ 待补判清单。"""
        std_of = {}
        for v in (version_a, version_b):
            std_of[v] = {r["qkey"]: r["standard_fp"] for r in self.conn.execute(
                "SELECT qkey, standard_fp FROM spec_versions WHERE evalset_version=?", (v,))}
        shared = sorted(set(std_of[version_a]) & set(std_of[version_b]))
        same = [q for q in shared if std_of[version_a][q] == std_of[version_b][q]]
        rows, pending = [], []
        for q in [x for x in shared if x not in same]:
            for a in self.answers(qkey=q):
                ca = self.conclusion(a["answer_id"], std_of[version_a][q], chain_sha, rules_sha)
                cb = self.conclusion(a["answer_id"], std_of[version_b][q], chain_sha, rules_sha)
                if ca is None or cb is None:
                    pending.append({"uid": self.question(q)["uid"], "answer_id": a["answer_id"],
                                    "missing": "A" if ca is None else "B"})
                    continue
                rows.append({"answer_id": a["answer_id"], "uid": self.question(q)["uid"],
                             "run_label": self.run_labels().get(a["run_id"], a["run_id"]),
                             "mode": a["mode"], "trial": a["trial"],
                             "a": {"status": ca["status"], "score": ca["score"]},
                             "b": {"status": cb["status"], "score": cb["score"]},
                             "delta": None if None in (ca["score"], cb["score"])
                                      else round(cb["score"] - ca["score"], 1)})
        return {"shared": len(shared), "same_standard": len(same),
                "rows": rows, "pending": pending,
                "fp_a": std_of[version_a], "fp_b": std_of[version_b]}

    def conclusion(self, answer_id: str, standard_fp: str, chain_sha: str, rules_sha: str,
                   weight_fp: str | None = None) -> dict | None:
        if weight_fp is None:
            a = self.answer(answer_id)
            q = self.question(a["qkey"]) if a else None
            weight_fp = (q or {}).get("current_weight_fp") or "equal"
        row = self.conn.execute(
            "SELECT * FROM conclusions WHERE answer_id=? AND standard_fp=? AND chain_sha=?"
            " AND rules_sha=? AND weight_fp=?",
            (answer_id, standard_fp, chain_sha, rules_sha, weight_fp)).fetchone()
        if not row:
            return None
        out = dict(row)
        out["detail"] = json.loads(row["detail_json"] or "{}")
        return out

    def answer_view_rows(self, chain_sha: str, rules_sha: str, with_records: bool = False,
                         snapshot: str | None = None) -> list[dict]:
        """answers × questions × (标准, 链, 规则, 权重) 下的结论（统计与比较的取数口；
        缺判定的 c_status 为 NULL → 待重判）。snapshot 给定时按该快照逐题解析标准
        （doc/15 §1.1），缺省当前标准；被测指纹与拆字段列随行（对比页免 join）。
        record_json 较大，仅在 with_records=True（锚点绊线）时随行带出。"""
        record_col = "a.record_json, " if with_records else ""
        sql = f"""
        SELECT a.answer_id, a.qkey, a.run_id, a.episode_id, a.judge_fp_at_run, a.profile_id,
               a.models_json, a.mode, a.trial, a.skills_mode, a.skills_base, a.evalset_version,
               a.run_status, {record_col}a.injection_found_gt1, a.variant_leak_json,
               a.subject_fp, a.model, a.reasoning_effort, a.agent,
               CASE WHEN r0.diagnostic = 1 THEN 1 ELSE 0 END AS diagnostic,
               q.uid, q.case_id, q.variant_index, q.rewrite,
               q.dim, q.topic, q.current_standard_fp, q.current_weight_fp, q.off_snapshot, q.active,
               c.standard_fp AS c_standard, c.weight_fp AS c_weight,
               c.status AS c_status, c.reason AS c_reason, c.detail_json,
               c.score AS c_score, c.score_min AS c_score_min, c.score_max AS c_score_max,
               c.score_source AS c_score_source
        FROM answers a JOIN questions q ON a.qkey = q.qkey
        LEFT JOIN runs r0 ON r0.run_id = a.run_id
        LEFT JOIN conclusions c ON c.answer_id = a.answer_id AND c.chain_sha = ?
             AND c.rules_sha = ?
             AND c.standard_fp = q.current_standard_fp
             AND c.weight_fp = COALESCE(q.current_weight_fp, 'equal')
        ORDER BY a.answer_id"""
        rows = [dict(r) for r in self.conn.execute(sql, (chain_sha, rules_sha))]
        if snapshot:                       # 按快照逐题解析标准（§1.1）并过滤不在快照内的题
            std_of = {r["qkey"]: r["standard_fp"] for r in self.conn.execute(
                "SELECT qkey, standard_fp FROM spec_versions WHERE evalset_version=?", (snapshot,))}
            out = []
            for r in rows:
                fp = std_of.get(r["qkey"])
                if fp is None:
                    continue                # 这一题不在快照里：不活跃，不参与统计
                c = self.conclusion(r["answer_id"], fp, chain_sha, rules_sha) or {}
                r["current_standard_fp"] = fp
                r["c_status"], r["c_reason"] = c.get("status"), c.get("reason") or ""
                r["c_score"], r["c_score_min"] = c.get("score"), c.get("score_min")
                r["c_score_max"], r["c_score_source"] = c.get("score_max"), c.get("score_source")
                r["c_standard"], r["c_weight"] = c.get("standard_fp"), c.get("weight_fp")
                out.append(r)
            rows = out
        labels = self.run_labels()          # 展示名随行（命名改造）：runtag/队列/对比用
        for r in rows:
            r["run_label"] = labels.get(r["run_id"]) or r["run_id"]
        return rows

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

    def review_conversations(self, chain_sha: str, rules: Rules, rules_sha: str,
                             tab: str = "undecided", run_id: str | None = None, dim: str | None = None,
                             spot_ratio: float = 0.1, operator: str | None = None,
                             page: int = 0, size: int = 20,
                             first_input: bool = True) -> tuple[list[dict], dict]:
        """复核队列（以对话为单位，五个标签）：SQL 聚合 + 分页。
        undecided=结论待定（区间跨及格线/前提未判，含判定异常）；unjudged=结论已定仍有未判；
        spot=模型判过且无人工（按比例抽样）；mismatch=人工≠机器；failed=会话失败。
        first_input=False 供复核页取上/下一段：免解析全部 record（大库性能）。"""
        rows = self.answer_view_rows(chain_sha, rules_sha)
        if run_id:
            rows = [r for r in rows if r["run_id"] == run_id]
        if dim:
            rows = [r for r in rows if (r["dim"] or "") == dim]
        pend_items, _ = self.undecided_items(chain_sha, run_id=run_id, page=0, size=10 ** 9)
        pend_by_answer: dict[str, list[dict]] = defaultdict(list)
        for c in pend_items:
            pend_by_answer[c["answer_id"]].append(c)
        mismatch_ids = {m["answer_id"] for m in self.human_machine_compare(chain_sha, operator)[0]}
        judge_ids = {r["answer_id"] for r in self.conn.execute(
            "SELECT DISTINCT answer_id FROM verdicts WHERE chain_sha=? AND source='judge'", (chain_sha,))}
        human_ids = {r["answer_id"] for r in self.conn.execute("SELECT DISTINCT answer_id FROM reviews")}
        pass_mark = rules.score.pass_mark
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
                "run_label": r.get("run_label") or r["run_id"],
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

    def conversation_view(self, answer_id: str, chain_sha: str, rules: Rules, rules_sha: str,
                          standard_fp: str | None = None) -> dict:
        """复核页一次取齐（§5.3）：record、当前标准、条目（按判定顺序分节）、机器栏、
        人工栏、有效结论、人工会话分、作废记录、轮次观测。条目权重显示用例有效权重
        （item_weights，恒百分制；无权重用例不显示数值）。"""
        a = self.answer(answer_id)
        if a is None:
            raise StoreError(f"答案不存在：{answer_id}")
        cur = self.current_spec(a["qkey"])
        fp = standard_fp or (cur[0] if cur else None)
        if fp is None:
            fp = a["judge_fp_at_run"]
        spec = self.spec_of(a["qkey"], fp) or {}
        q_row = self.question(a["qkey"]) or {}
        weights, _, _ = self.effective_weights(q_row.get("case_id") or "", fp)
        record = json.loads(a["record_json"])
        ctx = build_context(record, spec)
        items = extract_items(ctx)
        reviews = self.reviews_for(answer_id)
        vrows = {r["item_id"]: r for r in self.verdicts(answer_id, fp, chain_sha)}
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
                    "weight": weights.get(it.id), "weight_is_pct": bool(weights),
                    "group": it.attrs.get("group"),
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
        concl = self.conclusion(answer_id, fp, chain_sha, rules_sha) or {}
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
            "run_id": a["run_id"], "run_label": self.run_labels().get(a["run_id"]) or a["run_id"],
            "mode": a["mode"], "trial": a["trial"], "run_status": a["run_status"],
            "profile": a["profile_id"], "models": json.loads(a["models_json"] or "[]"),
            "skills_mode": a["skills_mode"], "evalset_version": a["evalset_version"],
            "reasoning_effort": profile_content.get("reasoning_effort"),
            "standard_fp": fp, "is_current": cur is not None and fp == cur[0],
            "weights_touched_after": self.weights_touched_after(answer_id),
            "sections": sections, "turns": turns,
            "injections_applied": record.get("injections_applied") or [],
            "injection_observed": json.loads(a.get("injection_observed_json") or "null"),
            "conclusion": concl, "voided": voided, "override": override,
            "spec": spec, "pending_rejudge": len(self.pending_rejudge(chain_sha, qkey=a["qkey"])),
        }
