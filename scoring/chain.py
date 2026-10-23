"""链执行器、跨 run 缓存与 verdicts.jsonl 读写（方案 §5.2、§5.3、§8.1）。"""
from __future__ import annotations

import hashlib
import importlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .items import EpisodeContext, Item, SCALES, build_context, episode_error, extract_items, item_sha_of
from .operators import Operator, Verdict


class ConfigError(Exception):
    """算子/链/口径配置不合法：拒绝运行。"""


# ---------------------------------------------------------------- 配置装载与校验

def _resolve_impl(impl: str, params: dict) -> Operator:
    from .operators import REGISTRY  # 延迟导入避免注册顺序问题

    if impl in REGISTRY:
        cls = REGISTRY[impl]
    elif ":" in impl:
        module, cls_name = impl.rsplit(":", 1)
        try:
            cls = getattr(importlib.import_module(module), cls_name)
        except (ImportError, AttributeError) as e:
            raise ConfigError(f"外部算子导入失败：{impl}（{e}）") from e
    else:
        raise ConfigError(f"未知算子实现：{impl}")
    try:
        return cls(**params)
    except TypeError as e:
        raise ConfigError(f"算子实例化失败：{impl} {params}（{e}）") from e


_ATTRS_PREFIX = "item.attrs."
_KNOWN_INPUTS = ("item.text", "item.kind", "item.turn", "item.attrs", "answer")


def _validate_inputs(op: Operator, instance_id: str) -> None:
    for name in getattr(op, "inputs", ()) or ():
        if name in _KNOWN_INPUTS or (name.startswith(_ATTRS_PREFIX) and len(name) > len(_ATTRS_PREFIX)):
            continue
        raise ConfigError(f"算子实例 {instance_id} 声明了未知输入：{name}")


@dataclass
class OperatorConfig:
    instances: dict[str, Operator]
    chains: dict[str, list[Operator]]
    config_sha: str = ""                                # operators.yaml 原文哈希（审计用，不参与取数）
    chain_iids: dict[str, list[str]] = None             # 链 → 实例 id 顺序（规范化哈希用）
    instance_specs: dict[str, dict] = None              # 实例 id → {impl, **params}
    chain_sha: str = ""                                 # 规范化链配置哈希（verdicts/conclusions 取数键）

    def __post_init__(self):
        if self.chain_iids is None:
            self.chain_iids = {}
        if self.instance_specs is None:
            self.instance_specs = {}

    def canonical(self) -> dict:
        """规范化链配置（整改清单 R4）：只含链中实际引用的实例；
        注释、空白、未被引用的实例不影响。"""
        used = sorted({iid for iids in self.chain_iids.values() for iid in iids})
        return {
            "chains": {kind: [f"{op.id}@{op.version}:{iid}"
                              for iid, op in zip(self.chain_iids.get(kind, []), ops)]
                       for kind, ops in self.chains.items()},
            "instances": {iid: self.instance_specs[iid] for iid in used if iid in self.instance_specs},
        }

    def operators_of_config(self) -> list[str]:
        """链配置的算子清单（判定配置摘要，取自链配置而非命中行）。"""
        return sorted({f"{op.id}@{op.version}" for ops in self.chains.values() for op in ops})


def _chain_sha_of(canonical: dict) -> str:
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False)
                          .encode("utf-8")).hexdigest()


def config_from_dict(d: dict, config_sha: str = "") -> OperatorConfig:
    if d.get("schema") != 1:
        raise ConfigError("operators 配置 schema 必须为 1")
    instances: dict[str, Operator] = {}
    instance_specs: dict[str, dict] = {}
    for iid, spec in (d.get("operators") or {}).items():
        spec = dict(spec or {})
        impl = spec.pop("impl", None)
        if impl is None:
            raise ConfigError(f"算子实例 {iid} 缺少 impl")
        instances[iid] = _resolve_impl(impl, spec)
        instance_specs[iid] = {"impl": impl, **spec}
        _validate_inputs(instances[iid], iid)
    chains: dict[str, list[Operator]] = {}
    chain_iids: dict[str, list[str]] = {}
    for kind, chain in (d.get("chains") or {}).items():
        if kind not in SCALES:
            raise ConfigError(f"链配置了未知条目类型：{kind}")
        ops = []
        iids = []
        for iid in chain or []:
            if iid not in instances:
                raise ConfigError(f"链 {kind} 引用了不存在的算子实例：{iid}")
            op = instances[iid]
            if kind not in getattr(op, "scales", {}):
                raise ConfigError(f"算子 {iid}（{op.id}@{op.version}）的 scale 不覆盖条目类型 {kind}")
            ops.append(op)
            iids.append(iid)
        chains[kind] = ops
        chain_iids[kind] = iids
    cfg = OperatorConfig(instances=instances, chains=chains, config_sha=config_sha,
                         chain_iids=chain_iids, instance_specs=instance_specs)
    cfg.chain_sha = _chain_sha_of(cfg.canonical())
    return cfg


def load_operator_config(path: str | Path) -> OperatorConfig:
    p = Path(path)
    raw = p.read_text(encoding="utf-8")
    try:
        import yaml
    except ImportError as e:  # pragma: no cover
        raise ConfigError("读取 operators.yaml 需要 PyYAML（pip install pyyaml）") from e
    return config_from_dict(yaml.safe_load(raw), config_sha=hashlib.sha256(raw.encode("utf-8")).hexdigest())


def default_operators_path() -> Path:
    return Path(__file__).resolve().parent / "operators.yaml"


# ---------------------------------------------------------------- 跨 run 缓存（§5.3）

def _resolve_input(name: str, item: Item, ctx: EpisodeContext) -> str:
    if name == "item.text":
        return item.text or ""
    if name == "item.kind":
        return item.kind
    if name == "item.turn":
        return str(item.turn)
    if name == "answer":
        return ctx.answer_for(item)
    if name == "item.attrs":
        return json.dumps(item.attrs, sort_keys=True, ensure_ascii=False)
    if name.startswith(_ATTRS_PREFIX):
        return json.dumps(item.attrs.get(name[len(_ATTRS_PREFIX):]), sort_keys=True, ensure_ascii=False)
    raise ConfigError(f"未知的算子输入声明：{name}")


class VerdictCache:
    """模型判定缓存：全局追加式，键 = 算子 id@版本 + 声明输入的哈希。
    条目侧与回答侧对称：任一变化都不会误命中。规则算子不走缓存。"""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self._map: dict[str, Verdict] = {}

    @classmethod
    def load(cls, path: str | Path | None) -> "VerdictCache":
        c = cls(path)
        if c.path and c.path.exists():
            for line in c.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                d = json.loads(line)
                c._map[d["key"]] = Verdict(d.get("value"), d.get("source", "judge"), d.get("operator", ""),
                                           d.get("reason", ""), score=d.get("score"))
        return c

    def key(self, op: Operator, item: Item, ctx: EpisodeContext) -> str:
        parts = [f"{op.id}@{op.version}"]
        for name in getattr(op, "inputs", ()) or ():
            parts.append(f"{name}={_resolve_input(name, item, ctx)}")
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()

    def get(self, key: str) -> Verdict | None:
        return self._map.get(key)

    def put(self, key: str, v: Verdict) -> None:
        self._map[key] = v
        if self.path:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"key": key, **v.as_dict()}, ensure_ascii=False) + "\n")


def default_cache_path() -> Path:
    return Path(__file__).resolve().parent / "verdict-cache.jsonl"


# ---------------------------------------------------------------- 链执行

def judge_item(item: Item, ctx: EpisodeContext, chain: list[Operator], cache: VerdictCache | None = None
               ) -> tuple[Verdict, list[str], str]:
    """执行算子链。返回 (机器结论, trace, status)；status ∈ ok / undecided / error。
    前一个算子弃权才往后走；异常或取值出 scale 即 error，不中断其他条目（§4.1）。"""
    trace: list[str] = []
    for op in chain:
        opid = f"{op.id}@{op.version}"
        use_cache = cache is not None and op.source == "judge"
        key = cache.key(op, item, ctx) if use_cache else None
        if use_cache and cache.get(key) is not None:
            trace.append(f"{op.id}:cache")
            return cache.get(key), trace, "ok"
        try:
            v = op.judge(item, ctx)
        except Exception as e:  # noqa: BLE001 —— 异常即 error，不影响同会话其他条目
            trace.append(f"{op.id}:error")
            return Verdict(None, op.source, opid, reason=f"算子异常：{e}"), trace, "error"
        scale = op.scales.get(item.kind, ())
        if v.abstain:
            if v.value is not None:
                trace.append(f"{op.id}:error")
                return Verdict(None, op.source, opid, reason=f"弃权时不得给出取值：{v.value!r}"), trace, "error"
            trace.append(f"{op.id}:abstain")
            continue
        if v.value is None or v.value not in scale:
            trace.append(f"{op.id}:error")
            return Verdict(None, op.source, opid, reason=f"输出不在 scale {scale} 内：{v.value!r}"), trace, "error"
        if v.score is not None and not (isinstance(v.score, (int, float)) and 0 <= v.score <= 100):
            trace.append(f"{op.id}:error")
            return Verdict(None, op.source, opid, reason=f"分数不在 0–100 内：{v.score!r}"), trace, "error"
        trace.append(f"{op.id}:hit")
        if use_cache:
            cache.put(key, v)
        return v, trace, "ok"
    return Verdict(None, "", "", abstain=True), trace, "undecided"


# ---------------------------------------------------------------- verdicts.jsonl（§8.1）

def episode_id_of(record: dict) -> str:
    return record.get("episode_id") or f"{record.get('uid', '?')}-{record.get('mode', '?')}-t{record.get('trial', 0)}"


def answer_sha(answer: str) -> str:
    return hashlib.sha256(answer.encode("utf-8")).hexdigest()[:16]


@dataclass
class EpisodeJudged:
    record: dict
    ctx: EpisodeContext | None
    rows: list[dict]
    error: str | None


def judge_episode(record: dict, cfg: OperatorConfig, run_id: str, cache: VerdictCache | None = None,
                  case: dict | None = None) -> EpisodeJudged:
    """对一段会话的全部条目执行链，产出 verdicts.jsonl 行。会话前置不满足时无行。
    case 缺省取 record 内嵌编译行；重判时传 specs 表中的期望编译行。"""
    ctx = build_context(record, case)
    err = episode_error(record, ctx)
    if err:
        return EpisodeJudged(record, ctx, [], err)
    rows = []
    for item in extract_items(ctx):
        machine, trace, status = judge_item(item, ctx, cfg.chains.get(item.kind) or [], cache)
        rows.append({
            "schema": 1,
            "run_id": run_id,
            "episode_id": episode_id_of(record),
            "uid": record.get("uid", "?"),
            "item": {"kind": item.kind, "id": item.id, "turn": item.turn},
            "machine": machine.as_dict(),
            "trace": trace,
            "answer_sha": answer_sha(ctx.answer_for(item)),
            "item_sha": item_sha_of(item),
            "status": status,
        })
    return EpisodeJudged(record, ctx, rows, None)


def write_verdicts(run_dir: str | Path, rows: list[dict]) -> Path:
    p = Path(run_dir) / "verdicts.jsonl"
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return p


def load_verdicts(run_dir: str | Path) -> list[dict]:
    p = Path(run_dir) / "verdicts.jsonl"
    if not p.exists():
        raise FileNotFoundError(f"{p} 不存在：先运行 score 产出逐条判定")
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
