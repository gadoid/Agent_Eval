"""评测库存储（评测结果管理改造方案 §6/§9 + doc/17 §7）：SQLite 单文件，
判定与人工数据的唯一事实来源；原始运行产物仍是不可变文件。

三层存储：原始层（run 目录文件，不可变）→ 评测库（本模块）→ 导出层（jsonl/CSV，
可随时重新生成）。本模块是唯一访问评测库的模块，其他模块不直接拼 SQL；
只用标准 SQL（INTEGER PRIMARY KEY 自增除外），以便日后迁移 PostgreSQL。

会话结论（conclusions）为派生表，可随时重算（rescore 不调用任何算子）。

v9（doc/17 决定 15）：从空库起步，v1–v8 的迁移链删除，旧库不再可打开；
计分配置（用途 + 权重）以 md 为准——dist 的 scoring 块导入后落 case_scoring 表，
临时权重（temp_weights）只覆盖权重；结论键的 weight_fp 改名 scoring_fp
（md / 临时权重两种口径可并存，doc/17 §5.2）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from collections import defaultdict

from .items import (NODES, SCALES, Item, build_context, episode_error, extract_items, input_fp_of,
                    node_hint, resolve_plan, spec_items, standard_fp_of, strip_scoring, qkey_of,
                    variant_leak_suspects, TOTAL_ITEM, conversation_sha, version_label)
from .operators import Verdict
from .rules import EpisodeVerdict, Rules, canonical_sha, live_items, session_conclusion
from .subject import subject_fp_of_manifest

SCHEMA_VERSION = 9

# v9 全量建表（doc/17 §7）：空库起步；旧库（schema < 9）直接拒绝打开（决定 15）。
_SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE runs(
  run_id TEXT PRIMARY KEY, run_dir TEXT, evalset_version TEXT, profile_id TEXT,
  snapshot_json TEXT, imported_at TEXT, started_at TEXT, title TEXT,
  subject_fp TEXT, subject_json TEXT, subject_incomplete INTEGER NOT NULL DEFAULT 0,
  method_sha TEXT, diagnostic INTEGER NOT NULL DEFAULT 0, scope_json TEXT);

CREATE TABLE questions(
  qkey TEXT PRIMARY KEY,
  uid TEXT, case_id TEXT, variant_index INTEGER, rewrite TEXT,
  dim TEXT, topic TEXT, tags_json TEXT, input_fp TEXT,
  current_standard_fp TEXT, active INTEGER, first_seen TEXT,
  granularity TEXT, off_snapshot INTEGER NOT NULL DEFAULT 0, current_scoring_fp TEXT);

CREATE TABLE specs(
  qkey TEXT, standard_fp TEXT, spec_json TEXT, evalset_version TEXT, imported_at TEXT,
  PRIMARY KEY(qkey, standard_fp));

CREATE TABLE spec_versions(
  qkey TEXT, standard_fp TEXT, evalset_version TEXT, imported_at TEXT,
  PRIMARY KEY(qkey, standard_fp, evalset_version));

CREATE TABLE answers(
  answer_id TEXT PRIMARY KEY,
  qkey TEXT, run_id TEXT, episode_id TEXT, judge_fp_at_run TEXT,
  profile_id TEXT, models_json TEXT, mode TEXT, trial INTEGER,
  skills_mode TEXT, skills_base TEXT, evalset_version TEXT,
  run_status TEXT, record_json TEXT,
  injection_found_gt1 INTEGER, variant_leak_json TEXT, injection_observed_json TEXT,
  subject_fp TEXT, model TEXT, reasoning_effort TEXT, agent TEXT);

CREATE TABLE verdicts(
  answer_id TEXT, standard_fp TEXT, chain_sha TEXT, item_id TEXT,
  kind TEXT, turn INTEGER, item_sha TEXT, answer_sha TEXT,
  value TEXT, source TEXT, operator TEXT, reason TEXT, trace_json TEXT,
  status TEXT, judged_at TEXT, score REAL,
  PRIMARY KEY(answer_id, standard_fp, chain_sha, item_id));

CREATE TABLE reviews(
  review_id INTEGER PRIMARY KEY,
  answer_id TEXT, item_id TEXT, item_sha TEXT, answer_sha TEXT,
  value TEXT, reason TEXT, reviewer TEXT, at TEXT, score REAL, legacy_key TEXT);
CREATE UNIQUE INDEX idx_reviews_legacy ON reviews(legacy_key);

CREATE TABLE verdict_cache(
  cache_key TEXT PRIMARY KEY, value TEXT, source TEXT, operator TEXT, reason TEXT, score REAL);

CREATE TABLE conclusions(
  answer_id TEXT, standard_fp TEXT, chain_sha TEXT, rules_sha TEXT, scoring_fp TEXT,
  status TEXT, reason TEXT, detail_json TEXT,
  score REAL, score_min REAL, score_max REAL, auto_score REAL, score_source TEXT,
  PRIMARY KEY(answer_id, standard_fp, chain_sha, rules_sha, scoring_fp));

CREATE TABLE snapshots(
  evalset_version TEXT PRIMARY KEY, snapshot_sha TEXT NOT NULL,
  method_json TEXT NOT NULL, method_sha TEXT NOT NULL,
  include TEXT, name TEXT, imported_at TEXT NOT NULL);

CREATE TABLE case_scoring(
  case_id TEXT PRIMARY KEY, scoring_json TEXT NOT NULL, imported_at TEXT NOT NULL);

CREATE TABLE temp_weights(
  case_id TEXT PRIMARY KEY,
  weights_json TEXT NOT NULL,
  in_use INTEGER NOT NULL DEFAULT 0,
  set_by TEXT, set_at TEXT NOT NULL);

CREATE TABLE rules(
  rules_sha TEXT PRIMARY KEY, rules_json TEXT NOT NULL, name TEXT, created_at TEXT NOT NULL);

CREATE TABLE chains(
  chain_sha TEXT PRIMARY KEY, name TEXT, operators_json TEXT, judge_models_json TEXT, created_at TEXT);

CREATE INDEX idx_answers_qkey ON answers(qkey);
CREATE INDEX idx_answers_run ON answers(run_id);
CREATE INDEX idx_verdicts_lookup ON verdicts(answer_id, standard_fp, chain_sha);
CREATE INDEX idx_reviews_answer ON reviews(answer_id, item_id);
CREATE INDEX idx_conclusions_key ON conclusions(standard_fp, chain_sha, rules_sha, scoring_fp);
"""

_MIGRATIONS: dict[int, list] = {SCHEMA_VERSION: [_SCHEMA]}


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
            if 0 < current < SCHEMA_VERSION:
                raise StoreError(
                    f"旧版评测库（schema v{current}）不再支持：doc/17 决定 15 起 v9 从空库起步，"
                    "请按 §9 用新代码重新导入重建（旧库已废弃归档）")
            for version in sorted(_MIGRATIONS):
                if version > current:
                    for step in _MIGRATIONS[version]:
                        if callable(step):
                            step(self.conn)
                        else:
                            self.conn.executescript(step)
                    self.conn.execute("INSERT OR REPLACE INTO meta VALUES('schema_version', ?)", (str(version),))

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
        spec_json 落库前剥除计分配置（doc/17 §3.2：scoring 不进标准——standard_fp 不含
        计分，只改用途/权重复用同一指纹，spec 行不可变，计分配置另落 case_scoring）。"""
        spec_json = strip_scoring(dict(spec_json))
        ts = now()
        with self.lock, self.conn:
            self.conn.execute("INSERT OR IGNORE INTO specs VALUES(?,?,?,?,?)",
                              (qkey, standard_fp, json.dumps(spec_json, ensure_ascii=False),
                               evalset_version, ts))
            self.conn.execute("INSERT OR IGNORE INTO spec_versions VALUES(?,?,?,?)",
                              (qkey, standard_fp, evalset_version, ts))

    # ---------------------------------------------------------------- 计分配置（doc/17 §3/§5.2）

    def _case_seed(self, case_id: str) -> str | None:
        """用例的种子题 qkey（uid 最小者；计分配置按用例生效，B1）。"""
        row = self.conn.execute(
            "SELECT qkey FROM questions WHERE case_id=? ORDER BY uid LIMIT 1", (case_id,)).fetchone()
        return row["qkey"] if row else None

    def _register_case_scoring(self, case_id: str, scoring: dict | None) -> None:
        """导入/入库时落库 md 计分配置（dist 的 scoring 块原样，按用例覆盖写，doc/17 §7）。"""
        cid = case_id or "?"
        with self.lock, self.conn:
            self.conn.execute("INSERT OR REPLACE INTO case_scoring VALUES(?,?,?)",
                              (cid, json.dumps(scoring or {}, ensure_ascii=False), now()))

    def case_scoring(self, case_id: str) -> dict:
        row = self.conn.execute("SELECT scoring_json FROM case_scoring WHERE case_id=?", (case_id,)).fetchone()
        return json.loads(row["scoring_json"]) if row else {}

    def scoring_plan(self, case_id: str, standard_fp: str | None = None) -> tuple[dict, str, bool]:
        """计分方案与口径指纹 → (plan, scoring_fp, 是否临时权重口径)。

        - md 口径：case_scoring 的 scoring 块按当前标准条目解析（items.resolve_plan），
          scoring_fp = canonical({"uses": {条目: 用途}, "weights": {条目: 权重}})；
        - 临时权重口径：temp_weights.in_use=1 时权重换成临时值（只覆盖权重、不覆盖
          用途；已不存在的条目忽略，新增计分条目 0），scoring_fp = canonical({uses, temp})。
        用途必须进指纹（doc/17 §5.2）：改用途后旧结论不会被误复用；两种口径同形，
        结论可并存、直接比较。未分配（计分权重合计 0）在 plan["unassigned"] 上。"""
        seed = self._case_seed(case_id)
        spec = None
        if seed:
            q = self.question(seed) or {}
            fp = standard_fp or q.get("current_standard_fp")
            spec = self.spec_of(seed, fp) if fp else None
        plan = resolve_plan(spec or {}, self.case_scoring(case_id))
        temp = self.conn.execute("SELECT weights_json, in_use FROM temp_weights WHERE case_id=?",
                                 (case_id,)).fetchone()
        if temp and temp["in_use"]:
            tw = json.loads(temp["weights_json"] or "{}")
            weights = {i: round(float(tw.get(i) or 0), 4)
                       for i, u in plan["uses"].items() if u == "score"}
            plan = {"uses": plan["uses"], "weights": weights,
                    "unassigned": sum(weights.values()) <= 0, "temp": True}
            return plan, canonical_sha({"uses": plan["uses"], "temp": weights}), True
        return plan, canonical_sha({"uses": plan["uses"], "weights": plan["weights"]}), False

    def _refresh_scoring_fp(self, case_id: str) -> str:
        """刷新用例全部变体的 questions.current_scoring_fp（S4 缓存列，doc/17 §7）。
        刷新时机：导入、临时权重保存 / 切换 / 转正 / 清除、标准切换。"""
        _, sfp, _ = self.scoring_plan(case_id)
        with self.lock, self.conn:
            self.conn.execute("UPDATE questions SET current_scoring_fp=? WHERE case_id=?",
                              (sfp, case_id))
        return sfp

    def temp_weights(self, case_id: str) -> dict | None:
        """临时权重（至多一份，doc/17 §5.2），含与当前条目集的一致性提示字段。"""
        row = self.conn.execute("SELECT * FROM temp_weights WHERE case_id=?", (case_id,)).fetchone()
        if not row:
            return None
        weights = json.loads(row["weights_json"] or "{}")
        plan, _, _ = self.scoring_plan(case_id)
        scored = {i for i, u in plan["uses"].items() if u == "score"}
        return {"weights": weights, "in_use": bool(row["in_use"]), "set_by": row["set_by"],
                "set_at": row["set_at"], "unassigned": sum(weights.values()) <= 0,
                "unknown": sorted(set(weights) - scored),          # 标准变化后已不存在的条目
                "missing": sorted(scored - set(weights))}          # 新增的计分条目（按 0 计）

    def set_temp_weights(self, case_id: str, weights: dict[str, float], by: str = "web",
                         use: bool = True) -> dict:
        """保存临时权重（doc/17 §5.2）：只收当前标准里用途为计分的条目（含拒答），
        0–100 名义百分比，按 §3.1 同式归一后存库（每用例至多一份、不留历史）。
        use=True 时同步切为临时权重口径；随后调用方只重算本用例（rescore_cases）。"""
        seed = self._case_seed(case_id)
        if seed is None:
            raise StoreError(f"用例不存在：{case_id}")
        plan, _, _ = self.scoring_plan(case_id)
        scored = {i for i, u in plan["uses"].items() if u == "score"}
        unknown = [k for k in weights if k not in scored]
        if unknown:
            raise StoreError(f"不可调权重的条目：{unknown}（只有用途为计分的条目能设临时权重）")
        for k, v in weights.items():
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not 0 <= float(v) <= 100:
                raise StoreError(f"权重应为 0–100 的百分比：{k} = {v!r}")
        total = sum(float(v) for v in weights.values())
        norm = ({k: round(float(v) / total * 100, 4) for k, v in weights.items()} if total > 0
                else {k: 0.0 for k in weights})
        with self.lock, self.conn:
            row = self.conn.execute("SELECT in_use FROM temp_weights WHERE case_id=?", (case_id,)).fetchone()
            in_use = 1 if (use or (row and row["in_use"])) else 0
            self.conn.execute("INSERT OR REPLACE INTO temp_weights VALUES(?,?,?,?,?)",
                              (case_id, json.dumps(norm, ensure_ascii=False), in_use, by, now()))
        sfp = self._refresh_scoring_fp(case_id)
        return {"case_id": case_id, "weights": norm, "in_use": bool(in_use),
                "unassigned": total <= 0, "scoring_fp": sfp}

    def temp_weights_use(self, case_id: str, on: bool) -> dict:
        """切换评测口径：md 权重 / 临时权重（doc/17 §5.2 的开关）。"""
        with self.lock, self.conn:
            row = self.conn.execute("SELECT 1 FROM temp_weights WHERE case_id=?", (case_id,)).fetchone()
            if row is None:
                raise StoreError(f"没有临时权重可切换：{case_id}")
            self.conn.execute("UPDATE temp_weights SET in_use=? WHERE case_id=?",
                              (1 if on else 0, case_id))
        sfp = self._refresh_scoring_fp(case_id)
        return {"case_id": case_id, "in_use": on, "scoring_fp": sfp}

    def clear_temp_weights(self, case_id: str) -> dict:
        """删除临时权重（转正后 / 放弃实验）：口径回 md。"""
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM temp_weights WHERE case_id=?", (case_id,))
        sfp = self._refresh_scoring_fp(case_id)
        return {"case_id": case_id, "in_use": False, "scoring_fp": sfp}

    def weights_touched_after(self, answer_id: str) -> bool:
        """人工总分之后计分配置或计分规则改过（C4 提示）。"""
        a = self.answer(answer_id)
        if a is None:
            return False
        at = self.conn.execute(
            "SELECT MAX(at) AS at FROM reviews WHERE answer_id=? AND item_id=?",
            (answer_id, TOTAL_ITEM)).fetchone()["at"]
        if at is None:
            return False
        q_row = self.question(a["qkey"]) or {}
        cid = q_row.get("case_id") or ""
        w = self.conn.execute(
            "SELECT MAX(set_at) AS s FROM temp_weights WHERE case_id=?", (cid,)).fetchone()["s"]
        s = self.conn.execute(
            "SELECT MAX(imported_at) AS s FROM case_scoring WHERE case_id=?", (cid,)).fetchone()["s"]
        rules_at = self.conn.execute("SELECT MAX(created_at) AS s FROM rules").fetchone()["s"]
        return any(x and x > at for x in (w, s, rules_at))

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
                           scoring_by_case: dict[str, dict] | None = None) -> str:
        """登记/刷新快照（snapshot_sha 单一算法，S2：doc/15 §1 + doc/17 §8.2 的
        scoring 分量）。scoring_by_case：导入原始行的计分配置（specs 表已剥除）。"""
        method_js, method_sha = self._load_method(method)
        rows_v, smap = [], dict(scoring_by_case or {})
        for qkey in qkeys:
            q = self.question(qkey) or {}
            sv = self.conn.execute(
                "SELECT standard_fp, spec_json FROM specs WHERE qkey=? AND evalset_version=?",
                (qkey, version)).fetchone()
            if sv is None:
                continue
            rows_v.append([q.get("uid") or "?", q.get("input_fp") or "", sv["standard_fp"]])
            if not smap and (q.get("variant_index") or 0) == 0:
                sc = self.case_scoring(q.get("case_id") or "?")
                if sc:
                    smap[q.get("case_id") or "?"] = sc
        snap_sha = canonical_sha({"rows": sorted(rows_v), "scoring": smap, "method_sha": method_sha})
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO snapshots VALUES(?,?,?,?,?,?,?)"
                " ON CONFLICT(evalset_version) DO UPDATE SET snapshot_sha=excluded.snapshot_sha,"
                " method_json=excluded.method_json, method_sha=excluded.method_sha,"
                " include=excluded.include",
                (version, snap_sha, method_js, method_sha, include, None, now()))
        return snap_sha

    def import_evalset(self, rows: list[dict], version: str, method: dict | None = None) -> dict:
        """specs import：登记全部标准并把该版本设为当前快照；未出现的题保留旧标准、
        标记非活跃。计分配置（scoring 块）按用例覆盖写 case_scoring、刷新缓存列；
        报告未分配（计分权重合计 0，评测将跳过，doc/17 §4）的用例。"""
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
            scoring_by_case = {}
            for cid, seed in seeds.items():
                self._register_case_scoring(cid, seed.get("scoring"))
                if seed.get("scoring"):
                    scoring_by_case[cid] = seed["scoring"]
            for cid in seeds:
                self._refresh_scoring_fp(cid)
            self._register_snapshot(version, list(qkeys), method, scoring_by_case=scoring_by_case)
            self.conn.execute("INSERT OR REPLACE INTO meta VALUES('current_snapshot', ?)", (version,))
        unassigned = [cid for cid, seed in seeds.items()
                      if resolve_plan(seed, seed.get("scoring"))["unassigned"]]
        return {"questions": len(qkeys), "version": version, "cases": len(seeds),
                "unassigned": unassigned}

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
            self._refresh_scoring_fp(self.question(qkey)["case_id"])
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
            if snap is None:
                self._register_snapshot(version, [r["qkey"] for r in rows])
            self.conn.execute("UPDATE questions SET active=0, off_snapshot=0")
            for r in rows:
                self.conn.execute("UPDATE questions SET active=1, current_standard_fp=? WHERE qkey=?",
                                  (r["standard_fp"], r["qkey"]))
            for cid in {self.question(r["qkey"])["case_id"] for r in rows}:
                if cid:
                    self._refresh_scoring_fp(cid)
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
        并登记执行时所用的标准与计分配置（含空库路径，R5）。
        题目尚无当前标准时以执行时标准设为当前并置活跃（R5）。
        注入 found>1 与变体泄露嫌疑在入库时预计算存列（R7）；
        被测指纹与拆字段列从 run 的 subject 回填（对比页免 join，doc/15 §2.3）。"""
        case = record.get("case") or {}
        qkey = qkey_of(case)
        fp = standard_fp_of(case)
        cid = case.get("case_id") or case.get("id") or "?"
        self.upsert_question(case, current_if_absent=fp)
        self.register_spec(qkey, fp, case, evalset_version_of(manifest))
        if (case.get("variant_index") or 0) == 0 and case.get("scoring") is not None:
            self._register_case_scoring(cid, case.get("scoring"))
            self._refresh_scoring_fp(cid)
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
        """按 run 开始时间排序（R8：导入顺序会把旧 run 提前，绊线方向会判反）。"""
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
        直接写结论，不算待重判；未分配用例的会话跳过评测、不调算子（doc/17 §4）。
        一条 SQL 找出缺判定的答案，只对它们解析 record。"""
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
        unassigned: dict[str, bool] = {}
        for r in self.conn.execute(sql, tuple(args)):
            q_row = self.question(r["qkey"]) or {}
            cid = q_row.get("case_id") or ""
            if cid not in unassigned:
                plan, _, _ = self.scoring_plan(cid, r["fp"])
                unassigned[cid] = bool(plan.get("unassigned"))
            if unassigned[cid]:
                continue                              # 权重未分配：跳过该用例的会话
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
                " reviewer, at, score, legacy_key) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (answer_id, item_id, item_sha, answer_sha, value, reason, reviewer, now(), None, legacy_key))
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
            if score is not None and vrow["kind"] not in ("required", "expected"):
                raise StoreError(f"显式分只有必要/期望事实采信（doc/17 §6.1 按条目类型强制），"
                                 f"不能带分数：{item_id}")
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
            v, sfp = self._conclude(answer_id, standard_fp, chain_sha, rules)
            self._write_conclusion(self.conn, answer_id, standard_fp, chain_sha, rules_sha, sfp, v)
        return self.conclusion(answer_id, standard_fp, chain_sha, rules_sha, sfp) or {}

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
        指纹、分数合法且有理由的记录（标准或回答变了即作废；计分配置不再使其作废，C4）。"""
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
                        page: int = 0, size: int = 20, veto_mode: str = "zero") -> tuple[list[dict], int]:
        """未决队列（§6.3 按有效结论取数）：机器结论为空或异常、且没有效人工改判
        （answer_sha + item_sha 匹配、取值在 scale 内）的参与条目。成员规则 =
        rules.live_items 单一实现（doc/17 §2.1 末列 / §6.4）：权重 > 0 的计分条目或
        所在节点有默认规则；参考、权重 0 的期望事实不进；短路时只有前提条目；
        未分配用例整例跳过。"""
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
        by_answer: dict[str, list[dict]] = {}
        for c in cands:
            by_answer.setdefault(c["answer_id"], []).append(c)
        out: list[dict] = []
        if cands:
            aids = list(by_answer)
            qmarks_a = ",".join("?" * len(aids))
            vr_by_answer: dict[str, list[dict]] = {}
            for v in self.conn.execute(
                    f"SELECT answer_id, standard_fp, item_id, kind, value, item_sha, answer_sha FROM verdicts"
                    f" WHERE answer_id IN ({qmarks_a}) AND chain_sha = ?", (*aids, chain_sha)):
                vr_by_answer.setdefault(v["answer_id"], []).append(dict(v))
            reviews_by_answer: dict[str, list[dict]] = {}
            for rv in self.conn.execute(
                    f"SELECT answer_id, item_id, item_sha, answer_sha, value FROM reviews"
                    f" WHERE answer_id IN ({qmarks_a})", tuple(aids)):
                reviews_by_answer.setdefault(rv["answer_id"], []).append(dict(rv))
            qkeys = {c["qkey"] for c in cands}
            qmarks_q = ",".join("?" * len(qkeys))
            specs = {(r["qkey"], r["standard_fp"]): json.loads(r["spec_json"])
                     for r in self.conn.execute(
                         f"SELECT qkey, standard_fp, spec_json FROM specs WHERE qkey IN ({qmarks_q})",
                         tuple(qkeys))}
            plan_by_case: dict[str, tuple[dict, str, bool]] = {}
            for cid in {c.get("case_id") for c in cands if c.get("case_id")}:
                plan_by_case[cid] = self.scoring_plan(cid)
            for a_id, items_ in by_answer.items():
                first = items_[0]
                forced = first["mode"] == "forced"
                fp = first["current_standard_fp"]
                spec = specs.get((first["qkey"], fp)) or {}
                plan = plan_by_case.get(first["case_id"], ({}, "", False))[0]
                if plan.get("unassigned"):
                    continue                       # 未分配用例：整例跳过（doc/17 §4）
                rows_ = [v for v in vr_by_answer.get(a_id, []) if v["standard_fp"] == fp]
                # 有效结论（人工改判覆盖机器值；取值须在 scale 内）
                decided: dict[str, str] = {}
                for v in rows_:
                    val = v["value"]
                    for rv in reviews_by_answer.get(a_id, []):
                        if rv["item_id"] == v["item_id"] and rv["answer_sha"] == v["answer_sha"] \
                                and (rv["item_sha"] or "") == (v["item_sha"] or "") \
                                and rv["value"] in SCALES.get(v["kind"], ()):
                            val = rv["value"]      # 最后一条有效改判
                    if val is not None:
                        decided[v["item_id"]] = val
                items_objs = [Item(kind, iid, None, None, {}) for iid, kind in spec_items(spec)]
                live = live_items(items_objs, decided, plan, forced=forced, veto_mode=veto_mode)
                for c in items_:
                    if c["item_id"] in live:
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
        """计算一个 (答案, 标准, 链配置, 计分规则, 计分口径) 的会话结论与会话分（不写库、
        不调用算子）。计分方案按用例解析（doc/17 §5.2 双口径），返回 (结论, scoring_fp)；
        未分配用例直接记 skipped（doc/17 §4）。"""
        a = self.answer(answer_id)
        if a is None:
            raise StoreError(f"答案不存在：{answer_id}")
        spec_row = self.conn.execute("SELECT spec_json FROM specs WHERE qkey=? AND standard_fp=?",
                                     (a["qkey"], standard_fp)).fetchone()
        if spec_row is None:
            raise StoreError(f"标准不存在：qkey={a['qkey']} standard_fp={standard_fp}")
        record = json.loads(a["record_json"])
        spec = json.loads(spec_row["spec_json"])
        q_row = self.question(a["qkey"]) or {}
        plan, sfp, _ = self.scoring_plan(q_row.get("case_id") or "", standard_fp)
        head = {"uid": record.get("uid", "?"), "mode": record.get("mode", "?"),
                "trial": record.get("trial", 0), "dim": spec.get("dim", "?")}
        if plan.get("unassigned"):
            return EpisodeVerdict(**head, status="skipped", invalid_reason="权重未分配"), sfp
        ctx = build_context(record, spec)
        manifest = json.loads((self.run(a["run_id"]) or {}).get("snapshot_json") or "{}")
        routing = ((manifest.get("profile") or {}).get("content") or {}).get("routing")
        err = episode_error(record, ctx)
        if err is not None:
            return EpisodeVerdict(**head, status="error", error=err), sfp
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
        return session_conclusion(record, ctx, items, eff, rules, routing, statuses=statuses,
                                  override=override, scoring=plan), sfp

    @staticmethod
    def _write_conclusion(conn, answer_id: str, standard_fp: str, chain_sha: str,
                          rules_sha: str, scoring_fp: str, v: EpisodeVerdict) -> None:
        """物化一条结论行（不开事务，供 submit_reviews 在同一事务内复用）。
        键 = (答案, 标准, 链配置, 计分规则, 计分口径) 五段（doc/15 §2.2 + doc/17 §5.2）。"""
        conn.execute("INSERT OR REPLACE INTO conclusions(answer_id, standard_fp, chain_sha, rules_sha,"
                     " scoring_fp, status, reason, detail_json,"
                     " score, score_min, score_max, auto_score, score_source)"
                     " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (answer_id, standard_fp, chain_sha, rules_sha, scoring_fp, v.status,
                      v.invalid_reason or v.error or "", json.dumps(v.as_dict(), ensure_ascii=False),
                      v.score, v.score_min, v.score_max, v.auto_score, v.score_source))

    def recompute_conclusion(self, answer_id: str, standard_fp: str, chain_sha: str,
                             rules: Rules, rules_sha: str) -> str:
        """重算并物化一个 (答案, 标准, 链配置, 规则, 计分口径) 的会话结论（不调用任何算子）。"""
        v, sfp = self._conclude(answer_id, standard_fp, chain_sha, rules)
        with self.lock, self.conn:
            self._write_conclusion(self.conn, answer_id, standard_fp, chain_sha, rules_sha, sfp, v)
        return v.status

    def rescore(self, rules: Rules, rules_sha: str, current_chain_sha: str | None = None,
                case_ids: list[str] | None = None) -> int:
        """只重算会话结论表（rescore）：对已有判定的全部 (答案, 标准, 链) 组合与当前
        标准下的无条目会话（运行错误）重算，不调用任何算子。case_ids 给定时只重算
        这些用例（rescore --case / 页面临时权重保存后，S5：所有有判定的链一起算）。
        未分配用例的答案没有判定行，也在当前链下物化 skipped 结论（doc/17 §4）。"""
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
            for a in self.answers():                      # 无判定行会话：运行错误或未分配跳过
                cur = self.current_spec(a["qkey"])
                if cur is None:
                    continue
                fp = cur[0]
                if self.has_verdicts(a["answer_id"], fp, current_chain_sha):
                    continue
                record = json.loads(a["record_json"])
                spec = cur[1]
                q_row = self.question(a["qkey"]) or {}
                plan, _, _ = self.scoring_plan(q_row.get("case_id") or "", fp)
                if not plan.get("unassigned") \
                        and episode_error(record, build_context(record, spec)) is None:
                    continue                              # 有条目但未判定：待重判，不产出结论
                self.recompute_conclusion(a["answer_id"], fp, current_chain_sha, rules, rules_sha)
                n += 1
        return n

    def rescore_cases(self, case_ids: list[str], rules: Rules, rules_sha: str) -> int:
        """按用例重算（doc/15 §3.5 S5）：该用例的会话在所有有判定的链下、按当前计分
        规则重算。页面临时权重保存 / 切换与 CLI rescore --case 走这里。"""
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
                   scoring_fp: str | None = None) -> dict | None:
        if scoring_fp is None:
            a = self.answer(answer_id)
            q = self.question(a["qkey"]) if a else None
            scoring_fp = (q or {}).get("current_scoring_fp") or ""
        row = self.conn.execute(
            "SELECT * FROM conclusions WHERE answer_id=? AND standard_fp=? AND chain_sha=?"
            " AND rules_sha=? AND scoring_fp=?",
            (answer_id, standard_fp, chain_sha, rules_sha, scoring_fp)).fetchone()
        if not row:
            return None
        out = dict(row)
        out["detail"] = json.loads(row["detail_json"] or "{}")
        return out

    def answer_view_rows(self, chain_sha: str, rules_sha: str, with_records: bool = False,
                         snapshot: str | None = None) -> list[dict]:
        """answers × questions × (标准, 链, 规则, 计分口径) 下的结论（统计与比较的取数口；
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
               q.dim, q.topic, q.current_standard_fp, q.current_scoring_fp, q.off_snapshot, q.active,
               c.standard_fp AS c_standard, c.scoring_fp AS c_scoring,
               c.status AS c_status, c.reason AS c_reason, c.detail_json,
               c.score AS c_score, c.score_min AS c_score_min, c.score_max AS c_score_max,
               c.score_source AS c_score_source
        FROM answers a JOIN questions q ON a.qkey = q.qkey
        LEFT JOIN runs r0 ON r0.run_id = a.run_id
        LEFT JOIN conclusions c ON c.answer_id = a.answer_id AND c.chain_sha = ?
             AND c.rules_sha = ?
             AND c.standard_fp = q.current_standard_fp
             AND c.scoring_fp = q.current_scoring_fp
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
                r["c_standard"], r["c_scoring"] = c.get("standard_fp"), c.get("scoring_fp")
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
        pend_items, _ = self.undecided_items(chain_sha, run_id=run_id, page=0, size=10 ** 9,
                                             veto_mode=rules.score.veto_mode)
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
        人工栏、有效结论、人工会话分、作废记录、轮次观测。条目用途与权重显示用例
        计分方案（md / 临时权重口径，恒百分制；参考条目不显示数值）。"""
        a = self.answer(answer_id)
        if a is None:
            raise StoreError(f"答案不存在：{answer_id}")
        cur = self.current_spec(a["qkey"])
        fp = standard_fp or (cur[0] if cur else None)
        if fp is None:
            fp = a["judge_fp_at_run"]
        spec = self.spec_of(a["qkey"], fp) or {}
        q_row = self.question(a["qkey"]) or {}
        plan, sfp, is_temp = self.scoring_plan(q_row.get("case_id") or "", fp)
        record = json.loads(a["record_json"])
        ctx = build_context(record, spec)
        items = extract_items(ctx)
        reviews = self.reviews_for(answer_id)
        vrows = {r["item_id"]: r for r in self.verdicts(answer_id, fp, chain_sha)}
        sections = []
        # 拒答导读固定文案（doc/17 §2.2/§8.1：说明依据资料无法回答，而非编造或泛泛拒答）
        REFUSAL_GUIDE = "应说明依据资料无法回答，而非编造或泛泛拒答"
        for kind in ("precond", "path", "forbidden", "required", "expected", "refusal"):
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
                use = plan["uses"].get(it.id) or NODES[kind]["use"]
                rows_.append({
                    "id": it.id, "kind": kind, "turn": it.turn, "text": it.text or "",
                    "keywords": it.attrs.get("keywords") or [],
                    "use": use,
                    "weight": plan["weights"].get(it.id) if use == "score" else None,
                    "weight_is_pct": use == "score",
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
                hint = REFUSAL_GUIDE if kind == "refusal" else node_hint(kind)
                sections.append({"kind": kind, "label": NODES[kind]["label"], "hint": hint, "rows": rows_})
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
            "scoring_fp": sfp, "scoring_is_temp": is_temp,
            "weights_touched_after": self.weights_touched_after(answer_id),
            "sections": sections, "turns": turns,
            "injections_applied": record.get("injections_applied") or [],
            "injection_observed": json.loads(a.get("injection_observed_json") or "null"),
            "conclusion": concl, "voided": voided, "override": override,
            "spec": spec, "pending_rejudge": len(self.pending_rejudge(chain_sha, qkey=a["qkey"])),
        }
