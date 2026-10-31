"""模型判定算子（doc/08 §5.2 第二批、doc/16 §4 判据契约）。

判定模型经 dsh CLI 单发会话调用（同一账号态，真实计费）。结论不由模型自由给出：
模型只产出判据结果（C1/C2/C3 × 满足/不满足/无法判断，客观 C1 附 contra 子标记），
结论由 items.verdict_from_criteria 按推导表得出；任一判据无法判断即弃权送人工。

成本控制：逐条同步调用（每次一个 dsh 进程）很慢，正式流程先用
`python -m scoring judge <run 目录>` 按会话聚合条目批量预填 verdict_cache
（键与链执行完全一致），score 时逐条命中缓存；缓存未命中且实例未开 sync 时
弃权（进人工复核），不静默降级。
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from ..items import (CRITERIA, CRITERIA_VER, FACT_SCALE, SCALES, EpisodeContext, Item,
                     verdict_from_criteria)
from ..operators import Verdict

_NATURE = {"主观": "subjective", "客观": "objective",
           "subjective": "subjective", "objective": "objective"}
_KIND_CN = {"required": "必要事实", "expected": "期望事实", "forbidden": "禁止事实"}


def _nature_of(item: Item) -> str:
    raw = str(item.attrs.get("nature") or item.attrs.get("性质") or "").strip()
    return _NATURE.get(raw, "objective")


def dialog_text(ctx: EpisodeContext) -> str:
    """对话全文（用户+助手逐轮），供判定模型看上下文；也是缓存键 dialog 输入。"""
    return "\n".join(
        f"[第{t.get('index') or i}轮·用户] {t.get('input') or ''}\n"
        f"[第{t.get('index') or i}轮·助手] {t.get('output') or ''}"
        for i, t in enumerate(ctx.record.get("turns") or []))


# ---------------------------------------------------------------- dsh 单发通道

def _overlay_yaml(root: str) -> str:
    """会话持久化重定向到隔离目录（不污染 ~/.dsh/sessions），格式对齐 runner。"""
    return ("- id: session-persistence-jsonl\n"
            "  name: '@deepseek-ai/dsh-session-persistence-jsonl'\n"
            "  config:\n"
            f"    root: {json.dumps(root)}\n")


class DshTransport:
    """spawn dsh CLI 跑一次判定会话，stdout 即最终回答。"""

    def __init__(self, bin_args: list[str], profile: str, home: str, timeout: int = 600,
                 judge_dir: str | None = None, env: dict | None = None):
        self.bin_args, self.profile, self.home = list(bin_args), profile, home
        self.timeout, self.judge_dir, self.env = int(timeout), judge_dir, dict(env or {})

    def ask(self, prompt: str) -> str:
        root = Path(self.judge_dir or Path(self.home) / "judge-sessions")
        root.mkdir(parents=True, exist_ok=True)
        overlay = root / f"judge-{int(time.time() * 1000)}-{os.getpid()}.yml"
        overlay.write_text(_overlay_yaml(str(root).replace("\\", "/")), encoding="utf-8")
        env = {**os.environ, "DSH_HOME": self.home, **self.env}
        try:
            proc = subprocess.run([*self.bin_args, "--profile", self.profile,
                                   "--patch", str(overlay), prompt],
                                  capture_output=True, text=True, encoding="utf-8",
                                  timeout=self.timeout, env=env, cwd=str(root))
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"判定会话超时（{self.timeout}s）") from e
        if proc.returncode != 0:
            raise RuntimeError(f"dsh 退出码 {proc.returncode}：{(proc.stderr or '')[-400:]}")
        return (proc.stdout or "").strip()


# ---------------------------------------------------------------- 输出解析

def _balanced_chunk(text: str, start: int, open_c: str, close_c: str) -> str | None:
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == open_c:
            depth += 1
        elif c == close_c:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def extract_json(text: str):
    """容忍模型输出带围栏/日志前缀/前后缀：直接 parse 失败则按出现位置依次截取
    平衡的 [..] / {..} 试 parse（dsh stdout 常带 [llm-…] 日志行，必须跳过）。"""
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    candidates = sorted((text.find(c), c, e) for c, e in (("[", "]"), ("{", "}")) if text.find(c) >= 0)
    for _pos, open_c, close_c in candidates:
        start = _pos
        while start >= 0:
            chunk = _balanced_chunk(text, start, open_c, close_c)
            if chunk is not None:
                try:
                    return json.loads(chunk)
                except ValueError:
                    pass
            start = text.find(open_c, start + 1)
    raise ValueError(f"判定模型输出不是合法 JSON：{text[:200]}")


# ---------------------------------------------------------------- 算子

class _LlmCriteria:
    """判据契约模型算子基类：模型给判据结果，结论按推导表得出。"""

    id = ""
    version = f"1+{CRITERIA_VER}"
    source = "judge"
    inputs = ("item.text", "item.kind", "item.turn", "item.attrs", "dialog")

    def __init__(self, model: str = "", dsh_bin: list | None = None, dsh_profile: str = "evalheadless",
                 dsh_home: str = "~/.dsh", dsh_env: dict | None = None, timeout: int = 600,
                 judge_dir: str | None = None, sync: bool = False):
        if not dsh_bin:
            raise ValueError("模型算子需要 dsh_bin（dsh CLI 命令，列表形式）")
        self.model = model
        self.dsh_bin, self.dsh_profile = list(dsh_bin), dsh_profile
        self.dsh_home = str(Path(dsh_home).expanduser())
        self.dsh_env, self.timeout, self.judge_dir = dsh_env or {}, timeout, judge_dir
        self.sync = sync

    @property
    def opid(self) -> str:
        return f"{self.id}@{self.version}"

    def transport(self) -> DshTransport:
        return DshTransport(self.dsh_bin, self.dsh_profile, self.dsh_home,
                            timeout=self.timeout, judge_dir=self.judge_dir, env=self.dsh_env)

    # -- prompt ------------------------------------------------------------
    def _criteria_lines(self, item: Item) -> tuple[str, str]:
        nature = _nature_of(item)
        crits = CRITERIA[(item.kind, nature)]
        lines = "\n".join(f"- {c['id']} {c['name']}：{c['rule']}" for c in crits)
        contra = ("- 客观条目 C1 判「不满足」时必须给出 contra 子标记："
                  "true = 回答给出了与条目矛盾的值；false = 回答未涉及该内容。"
                  if nature == "objective" else "")
        return lines, contra

    def item_block(self, item: Item, ctx: EpisodeContext) -> str:
        lines, contra = self._criteria_lines(item)
        turn = f"第 {item.turn} 轮" if item.turn else "最后一轮"
        return (f"### 条目 {item.id}\n类型：{_KIND_CN.get(item.kind, item.kind)}"
                f"（{item.kind}，{_nature_of(item)}）；只依据【助手在第 {turn or item.turn} 的回答】判定\n"
                f"内容：{item.text}\n判据：\n{lines}\n{contra}")

    def build_prompt(self, item: Item, ctx: EpisodeContext) -> str:
        return self._wrap([self.item_block(item, ctx)], dialog_text(ctx))

    def _wrap(self, blocks: list[str], dialog: str) -> str:
        n = len(blocks)
        shape = ('{"id": "条目id", "criteria": {"C1": {"result": "满足|不满足|无法判断", "contra": true|false}, '
                 '"C2": {...}, "C3": {...}}, "reason": "一句话依据，引用回答关键片段"}')
        arr = shape if n == 1 else f"[{shape}, ...]"
        return ("你是评测判定模型，依据对话记录判定条目在助手回答中的表现。只依据对话本身，"
                "不引入外部知识，不猜测未出现的内容。\n\n"
                f"【对话记录】\n{dialog}\n\n【条目】\n"
                f"{chr(10).join(blocks)}\n\n【输出】只输出 JSON"
                f"{'对象' if n == 1 else '数组（每条目一项）'}，不要输出任何其他文字：\n{arr}")

    # -- 判定 ---------------------------------------------------------------
    def _verdict_of(self, data: dict, item: Item) -> Verdict:
        crit = data.get("criteria") or {}
        reason = str(data.get("reason") or "")[:300]
        value = verdict_from_criteria(item.kind, _nature_of(item), crit)
        if value == "undecided":
            return Verdict(None, self.source, self.opid, abstain=True,
                           reason=f"判据含无法判断，送人工复核：{reason}")
        return Verdict(value, self.source, self.opid, reason=reason)

    def judge(self, item: Item, ctx: EpisodeContext) -> Verdict:
        if not self.sync:
            return Verdict(None, self.source, self.opid, abstain=True,
                           reason="模型判定未预填缓存（先运行 python -m scoring judge <run 目录>）")
        raw = self.transport().ask(self.build_prompt(item, ctx))
        return self._verdict_of(extract_json(raw), item)


class LlmFact(_LlmCriteria):
    id = "llm-fact"
    scales = {"required": FACT_SCALE, "expected": FACT_SCALE}


class LlmForbidden(_LlmCriteria):
    id = "llm-forbid"
    scales = {"forbidden": SCALES["forbidden"]}


# ---------------------------------------------------------------- 批量预填

def batch_judge(records: list[dict], cfg, cache, transport_of=None, log=print) -> dict:
    """按会话把待判条目聚成一次模型调用，预填 verdict_cache（键与链执行一致）。

    规则算子（链上位于 judge 之前）已能给出结论的条目跳过，不浪费模型调用。
    返回 {会话数, 条目数, 填充数, 跳过数}。transport_of(op) 注入传输层（测试用）。
    """
    from ..items import build_context, extract_items

    stats = {"episodes": 0, "items": 0, "filled": 0, "skipped": 0}
    for record in records:
        ctx = build_context(record)
        jobs = []
        for item in extract_items(ctx):
            chain = cfg.chains.get(item.kind) or []
            op = next((o for o in chain if o.source == "judge"), None)
            if op is None:
                continue
            ruled = any(not o.judge(item, ctx).abstain
                        for o in chain[:chain.index(op)])
            if ruled:
                stats["skipped"] += 1
                continue
            jobs.append((item, op))
        if not jobs:
            continue
        stats["episodes"] += 1
        stats["items"] += len(jobs)
        prompt = jobs[0][1]._wrap([op.item_block(item, ctx) for item, op in jobs],
                                  dialog_text(ctx))
        transport = (transport_of(jobs[0][1]) if transport_of else jobs[0][1].transport())
        by_id: dict[str, dict] = {}
        try:
            parsed = extract_json(transport.ask(prompt))
            arr = parsed if isinstance(parsed, list) else [parsed]
            by_id = {str(d["id"]): d for d in arr if isinstance(d, dict) and d.get("id")}
        except ValueError:
            log(f"  {record.get('uid')}：批量判定输出不可解析，逐条补判")
        for item, op in jobs:
            data = by_id.get(str(item.id))
            if data is None:
                # 批量输出常漏条目：单条重问一次，仍缺才送人工
                try:
                    single = extract_json(transport.ask(
                        op._wrap([op.item_block(item, ctx)], dialog_text(ctx))))
                    arr1 = single if isinstance(single, list) else [single]
                    data = next((d for d in arr1
                                 if isinstance(d, dict) and str(d.get("id")) == str(item.id)), None)
                except ValueError:
                    data = None
            if not data:
                log(f"  {record.get('uid')} {item.id}：模型未返回该条目，送人工")
                continue
            cache.put(cache.key(op, item, ctx), op._verdict_of(data, item))
            stats["filled"] += 1
    return stats
