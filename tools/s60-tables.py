"""Generate markdown tables for the s60 batch report.

Reads runner/out/compare-subject.json plus archive/eval.db, writes
runner/out/s60-tables.md.
"""

from __future__ import annotations

import json
import sqlite3
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "archive" / "eval.db"
CMP = ROOT / "runner" / "out" / "compare-subject.json"
OUT = ROOT / "runner" / "out" / "s60-tables.md"
VERSION = "v20261030-4"

ORDER = [
    ("deepseek-v4-flash", "low"),
    ("deepseek-v4-flash", "high"),
    ("deepseek-v4-pro", "low"),
    ("deepseek-v4-pro", "high"),
    ("glm-5.3", "low"),
    ("glm-5.3", "high"),
    ("glm-5.3-flash", "low"),
    ("glm-5.3-flash", "high"),
]
SHORT = {
    ("deepseek-v4-flash", "low"): "flash/low",
    ("deepseek-v4-flash", "high"): "flash/high",
    ("deepseek-v4-pro", "low"): "pro/low",
    ("deepseek-v4-pro", "high"): "pro/high",
    ("glm-5.3", "low"): "glm/low",
    ("glm-5.3", "high"): "glm/high",
    ("glm-5.3-flash", "low"): "glmfl/low",
    ("glm-5.3-flash", "high"): "glmfl/high",
}


def main() -> None:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    subj = {}
    for row in conn.execute(
        "SELECT subject_fp, subject_json FROM runs WHERE evalset_version = ?", (VERSION,)
    ):
        s = json.loads(row["subject_json"] or "{}")
        subj[row["subject_fp"]] = (s.get("model"), s.get("reasoning_effort"))

    fp_of = {v: k for k, v in subj.items() if v in SHORT}
    missing = [c for c in ORDER if c not in fp_of]
    if missing:
        raise SystemExit(f"missing subject fp for {missing}")

    cmp = json.loads(CMP.read_text(encoding="utf-8"))
    matrix = cmp["matrix"]
    dims = cmp["dims"]

    # per-config aggregates from conclusions / answers
    agg = {}
    for cfg in ORDER:
        fp = fp_of[cfg]
        r = conn.execute(
            """
            SELECT
              COUNT(*) AS n_all,
              SUM(CASE WHEN c.status = 'pass' THEN 1 ELSE 0 END) AS n_pass,
              SUM(CASE WHEN c.status = 'fail' THEN 1 ELSE 0 END) AS n_fail,
              SUM(CASE WHEN c.status = 'undecided' THEN 1 ELSE 0 END) AS n_und,
              AVG(CASE WHEN c.status IN ('pass','fail') THEN c.score END) AS mean_score
            FROM answers a JOIN conclusions c ON c.answer_id = a.answer_id
            WHERE a.evalset_version = ? AND a.subject_fp = ?
            """,
            (VERSION, fp),
        ).fetchone()
        cost = conn.execute(
            """
            SELECT SUM(tokens_total) AS tok, SUM(steps) AS steps,
                   SUM(duration_ms) AS dur, COUNT(*) AS n
            FROM answers WHERE evalset_version = ? AND subject_fp = ?
            """,
            (VERSION, fp),
        ).fetchone()
        agg[cfg] = dict(r)
        agg[cfg].update(dict(cost))

    # case rows in evalset order
    case_ids = []
    for key in matrix:
        cid = key.split("#", 1)[0]
        if cid not in case_ids:
            case_ids.append(cid)
    case_ids.sort()

    def cell(entry):
        if not entry or entry.get("n", 0) == 0:
            return "-"
        p, n, pend = entry.get("pass", 0), entry.get("n", 0), entry.get("pending", 0)
        if p == n:
            return "P"
        if p == 0 and pend == 0:
            return "F"
        if pend:
            return "?"
        return f"{p}/{n}"

    lines = []
    lines.append("## 配置汇总\n")
    lines.append("| 配置 | 通过/已判 | 通过率 | 均分 | 弃权 | tokens | 步数 | 用时 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for cfg in ORDER:
        a = agg[cfg]
        judged = (a["n_pass"] or 0) + (a["n_fail"] or 0)
        rate = (a["n_pass"] or 0) / judged if judged else 0
        mean = a["mean_score"] or 0
        tok = (a["tok"] or 0) / 1000
        dur = (a["dur"] or 0) / 60000
        lines.append(
            f"| {SHORT[cfg]} | {a['n_pass'] or 0}/{judged} | {rate*100:.1f}% | {mean:.1f} | "
            f"{a['n_und'] or 0} | {tok:.0f}k | {a['steps'] or 0} | {dur:.1f} min |"
        )

    lines.append("\n## 逐题矩阵\n")
    header = "| 用例 | " + " | ".join(SHORT[c] for c in ORDER) + " |"
    lines.append(header)
    lines.append("|" + "---|" * (len(ORDER) + 1))
    for cid in case_ids:
        cells = []
        for cfg in ORDER:
            fp = fp_of[cfg]
            merged = None
            for key, row in matrix.items():
                if key.split("#", 1)[0] != cid:
                    continue
                e = row.get(fp)
                if not e:
                    continue
                if merged is None:
                    merged = dict(e)
                else:
                    merged["pass"] = merged.get("pass", 0) + e.get("pass", 0)
                    merged["n"] = merged.get("n", 0) + e.get("n", 0)
                    merged["pending"] = merged.get("pending", 0) + e.get("pending", 0)
            cells.append(cell(merged))
        lines.append(f"| {cid} | " + " | ".join(cells) + " |")

    lines.append("\n## 分维度\n")
    lines.append("| 维度 | " + " | ".join(SHORT[c] for c in ORDER) + " |")
    lines.append("|" + "---|" * (len(ORDER) + 1))
    for dim, row in dims.items():
        cells = []
        for cfg in ORDER:
            e = row.get(fp_of[cfg]) or {}
            if not e.get("n"):
                cells.append("-")
            else:
                cells.append(f"{e['pass']}/{e['n']}")
        lines.append(f"| {dim} | " + " | ".join(cells) + " |")

    lines.append("\n## 失败与未决明细\n")
    rows = conn.execute(
        """
        SELECT a.subject_fp, a.qkey, c.status, c.reason, c.score
        FROM answers a JOIN conclusions c ON c.answer_id = a.answer_id
        WHERE a.evalset_version = ? AND c.status IN ('fail','undecided')
        ORDER BY a.qkey, a.subject_fp
        """,
        (VERSION,),
    ).fetchall()
    for r in rows:
        model, effort = subj.get(r["subject_fp"], ("?", "?"))
        reason = (r["reason"] or "").replace("\n", " ")
        lines.append(
            f"- {r['qkey']} · {model}/{effort} · {r['status']} · score={r['score']} · {reason[:220]}"
        )

    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(case_ids)} cases)")


if __name__ == "__main__":
    main()
