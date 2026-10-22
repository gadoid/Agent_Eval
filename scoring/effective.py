"""有效结论合成（方案 §6）：人工改判覆盖机器结论的唯一出口。

rescore、聚合、导出、复核服务全部只经 effective_final 取数；失效检查
（answer_sha 不匹配、取值不合法）在本模块完成，不在服务层另做。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .items import SCALES
from .operators import Verdict


@dataclass
class Review:
    run_id: str
    episode_id: str
    item_id: str
    value: str
    answer_sha: str
    reason: str = ""
    reviewer: str = ""
    at: str = ""
    schema: int = 1

    def as_dict(self) -> dict:
        d = asdict(self)
        if not d["at"]:
            d["at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Review":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def load_reviews(run_dir: str | Path) -> list[Review]:
    p = Path(run_dir) / "review.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(Review.from_dict(json.loads(line)))
    return out


def append_review(run_dir: str | Path, review: Review) -> Path:
    """复核服务写回：只追加不改写，同一条目以最后一条有效记录为准（§8.2）。"""
    p = Path(run_dir) / "review.jsonl"
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(review.as_dict(), ensure_ascii=False) + "\n")
    return p


def _valid(r: Review, item_id: str, item_kind: str, answer_sha: str) -> bool:
    """改判对条目生效的条件：同一 run/会话内条目 id 一致、answer_sha 匹配、取值在该条目
    类型的 scale 内。传入的 reviews 是会话级列表，item_id 过滤必须在这里完成。"""
    return (r.item_id == item_id and r.answer_sha == answer_sha
            and r.value in SCALES.get(item_kind, ()))


def effective_final(machine_row: dict, reviews: list[Review], answer_sha: str) -> Verdict:
    """有效结论 = 最后一条有效人工改判 ?? 机器结论（§6）。"""
    kind = machine_row["item"]["kind"]
    item_id = machine_row["item"]["id"]
    valid = [r for r in reviews if _valid(r, item_id, kind, answer_sha)]
    if valid:
        r = valid[-1]
        return Verdict(r.value, "human", "review", reason=r.reason)
    m = machine_row["machine"]
    return Verdict(m.get("value"), m.get("source", ""), m.get("operator", ""), reason=m.get("reason", ""))


def applied_count(rows: list[dict], reviews: list[Review]) -> int:
    """生效的人工改判条数（判定配置摘要 reviews_applied）。"""
    by_ep: dict[str, list[Review]] = {}
    for r in reviews:
        by_ep.setdefault(r.episode_id, []).append(r)
    n = 0
    for row in rows:
        if effective_final(row, by_ep.get(row["episode_id"], []), row["answer_sha"]).source == "human":
            n += 1
    return n


def voided_reviews(rows: list[dict], reviews: list[Review]) -> list[dict]:
    """作废清单（§6）：条目不存在、answer_sha 不匹配或取值不合法的改判。"""
    by_key = {(row["episode_id"], row["item"]["id"]): row for row in rows}
    out = []
    for r in reviews:
        row = by_key.get((r.episode_id, r.item_id))
        if row is None:
            why = "条目不存在"
        elif r.answer_sha != row["answer_sha"]:
            why = "answer_sha 不匹配（回答已变化）"
        elif r.value not in SCALES.get(row["item"]["kind"], ()):
            why = f"取值不合法：{r.value!r}"
        else:
            continue
        out.append({"review": r.as_dict(), "reason": why})
    return out
