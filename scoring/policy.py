"""旧口径层（退役过渡，doc/15 §3.3）。

会话结论与计分已迁 rules.py；本模块只剩旧 policy.yaml 的读取能力，
供 v8 迁移（由 headline_mode/exclude_rewrites 生成快照测法、由 score 生成
rules）读取历史配置使用。P2 删除本模块；--policy CLI 参数按弃用别名转发
到 --rules。不要在新代码中 import 本模块。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path


class PolicyError(Exception):
    """policy.yaml 不合法：拒绝运行。"""


VETO_KEYS = ("forbidden_adopted", "path_violated", "required_wrong")
VETO_MODES = ("zero", "cap", "weight")
SCORED_VALUES = ("对", "部分", "未提及", "错", "满足", "违反", "未采信", "采信", "拒答", "未拒答")


def _default_map() -> dict[str, float]:
    return {"对": 100, "部分": 50, "未提及": 0, "错": 0, "满足": 100, "违反": 0,
            "未采信": 100, "采信": 0, "拒答": 100, "未拒答": 0}


@dataclass
class ScorePolicy:
    pass_mark: float = 60
    default_weight: float = 1
    map: dict = field(default_factory=_default_map)
    veto: dict = field(default_factory=lambda: {k: "zero" for k in VETO_KEYS})
    cap: float = 40
    dimension_unit: str = "case"           # case：用例等权 | question：题目等权
    dimensions: dict = field(default_factory=dict)   # 维度 → 综合分权重；未列出的维度权重 1
    exclude_rewrites: tuple = ("诱导",)    # 不计入维度分的变体改写方式
    compare_delta: float = 10


@dataclass
class Policy:
    undecided: str = "exclude"        # exclude 不计入分母 / fail 记失败
    headline_mode: dict = field(default_factory=lambda: {"default": "natural", "错误知识纠正": "forced"})
    score: ScorePolicy = field(default_factory=ScorePolicy)

    def headline(self, dim: str) -> str:
        return self.headline_mode.get(dim, self.headline_mode.get("default", "natural"))


def _num(v, name: str, lo: float = 0, hi: float | None = None) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < lo or (hi is not None and v > hi):
        rng = f"{lo}–{hi}" if hi is not None else f"≥ {lo}"
        raise PolicyError(f"{name} 应为数值（{rng}），实际 {v!r}")
    return float(v)


def score_policy_from_dict(d: dict) -> ScorePolicy:
    sp = ScorePolicy()
    if not isinstance(d, dict):
        raise PolicyError("score 必须是映射")
    unknown = set(d) - {"pass_mark", "default_weight", "map", "veto", "dimension_unit", "dimensions",
                        "exclude_rewrites", "compare_delta"}
    if unknown:
        raise PolicyError(f"score 含未知参数：{sorted(unknown)}")
    if "pass_mark" in d:
        sp.pass_mark = _num(d["pass_mark"], "score.pass_mark", 0, 100)
    if "default_weight" in d:
        sp.default_weight = _num(d["default_weight"], "score.default_weight")
    if "map" in d:
        m = dict(d["map"] or {})
        bad = set(m) - set(SCORED_VALUES)
        if bad:
            raise PolicyError(f"score.map 含未知结论词：{sorted(bad)}")
        missing = [v for v in SCORED_VALUES if v not in m]
        if missing:
            raise PolicyError(f"score.map 缺少结论词：{missing}")
        sp.map = {k: _num(v, f"score.map.{k}", 0, 100) for k, v in m.items()}
    if "veto" in d:
        v = dict(d["veto"] or {})
        cap = v.pop("cap", None)
        bad = set(v) - set(VETO_KEYS)
        if bad:
            raise PolicyError(f"score.veto 含未知否决项：{sorted(bad)}")
        for k, mode in v.items():
            if mode not in VETO_MODES:
                raise PolicyError(f"score.veto.{k} 只能是 {'/'.join(VETO_MODES)}")
        sp.veto = {**sp.veto, **v}
        if cap is not None:
            sp.cap = _num(cap, "score.veto.cap", 0, 100)
    if "dimension_unit" in d:
        if d["dimension_unit"] not in ("case", "question"):
            raise PolicyError("score.dimension_unit 只能是 case 或 question")
        sp.dimension_unit = d["dimension_unit"]
    if "dimensions" in d:
        sp.dimensions = {str(k): _num(v, f"score.dimensions.{k}") for k, v in (d["dimensions"] or {}).items()}
    if "exclude_rewrites" in d:
        sp.exclude_rewrites = tuple(str(x) for x in (d["exclude_rewrites"] or []))
    if "compare_delta" in d:
        sp.compare_delta = _num(d["compare_delta"], "score.compare_delta", 0, 100)
    return sp


def policy_from_dict(d: dict) -> Policy:
    p = Policy()
    if not isinstance(d, dict):
        raise PolicyError("policy 配置必须是映射")
    retired = {"partial_as", "veto_on_forbidden"} & set(d)
    if retired:
        raise PolicyError(f"{sorted(retired)} 已由 score 节取代（百分制口径，见 doc/10 §4.5）")
    unknown = set(d) - {"undecided", "headline_mode", "score", "schema"}
    if unknown:
        raise PolicyError(f"policy 含未知参数：{sorted(unknown)}")
    if d.get("schema", 2) != 2:
        raise PolicyError("policy schema 应为 2（百分制口径）")
    if "score" in d:
        p.score = score_policy_from_dict(d["score"])
    if "undecided" in d:
        if d["undecided"] not in ("exclude", "fail"):
            raise PolicyError("undecided 只能是 exclude 或 fail")
        p.undecided = d["undecided"]
    if "headline_mode" in d:
        hm = dict(d["headline_mode"] or {})
        if "default" not in hm:
            raise PolicyError("headline_mode 必须含 default")
        p.headline_mode = hm
    return p


def load_policy(path: str | Path) -> tuple[Policy, str]:
    raw = Path(path).read_text(encoding="utf-8")
    try:
        import yaml
    except ImportError as e:  # pragma: no cover
        raise PolicyError("读取 policy.yaml 需要 PyYAML（pip install pyyaml）") from e
    return policy_from_dict(yaml.safe_load(raw) or {}), hashlib.sha256(raw.encode("utf-8")).hexdigest()


def default_policy_path() -> Path:
    return Path(__file__).resolve().parent / "policy.yaml"


# ---------------------------------------------------------------- 迁移辅助：policy → rules/method

def rules_dict_of_policy(p: Policy) -> dict:
    """v8 迁移第 5 步：旧 Policy → rules 规范化字典（scored_rewrites_exclude 承接
    exclude_rewrites 的「不计分」语义；compare_delta 丢弃）。"""
    return {
        "undecided": p.undecided,
        "operator_score": "adopt",
        "score": {"pass_mark": p.score.pass_mark, "default_weight": p.score.default_weight,
                  "map": dict(p.score.map), "veto": {**p.score.veto, "cap": p.score.cap}},
        "aggregate": {"scored_rewrites_exclude": list(p.score.exclude_rewrites),
                      "dimension_unit": p.score.dimension_unit,
                      "dimensions": dict(p.score.dimensions)},
    }


def method_dict_of_policy(p: Policy, rewrites_vocab: list[str], anchors: list[str]) -> dict:
    """v8 迁移第 4 步：旧 Policy → 快照测法（modes 承接 headline_mode；
    run_rewrites = 改写词表 − exclude_rewrites）。"""
    excluded = set(p.score.exclude_rewrites)
    return {"modes": dict(p.headline_mode),
            "run_rewrites": [r for r in rewrites_vocab if r not in excluded],
            "anchors": list(anchors)}
