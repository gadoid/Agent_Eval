"""被测配置指纹（doc/15 §3.1 + doc/18 §2.1）：③ 被测配置的身份 = subject_fp。

字段表与缺省值和 runner 解析 profile 的缺省一致（B6：缺失字段按缺省补齐，
不写 unknown——同一套配置的旧记录不会因缺字段分裂成两组；确实缺关键项的
标 subject_incomplete）。agent.name 即 adapter 名（orchestrator.ts：agent 段）。
Python 侧用于 ingest 与回算；runner 侧（snapshot.ts）有一份 TS 实现，
同一 manifest fixture 两端必须得出相同指纹（tests/test_subject.py +
runner/tests 对拍，doc/15 §7.4）。doc/18 起：eline_profile_sha 移出 subject
（仍在 snapshot，RESUME_KEYS 用），新增 persona_sha / plugins_sha，
model/effort/provider 取有效值（覆盖 > cordis agent-default-model，
bundle 提供时拿不到 → null，观测兜底）；cordis 解析只在 TS 一份，
Python 读 manifest.snapshot.cordis 段。
"""
from __future__ import annotations

import json

from .rules import canonical_sha

# 缺省值与 runner profile 解析一致：dsh.profile ?? 'headless'、skill_tool ?? 'skill'
_DEFAULTS = {"skill_tool": "skill", "routing": {}, "dsh_profile": "headless",
             "agent": {"name": "dsh-cli", "version": None}}


def parse_profile_overrides(profile_id: str) -> tuple[str | None, str | None]:
    """profile id 后缀 @模型-努力程度（runner CLI --model/--reasoning-effort 覆盖时追加）。
    模型名可含连字符（glm-5.3、deepseek-v4-flash）→ 只从右侧切一次。"""
    if "@" not in profile_id:
        return None, None
    suffix = profile_id.rsplit("@", 1)[1]
    if "-" not in suffix:
        return suffix, None
    model, effort = suffix.rsplit("-", 1)
    return (model or None), (effort or None)


def subject_of_manifest(manifest: dict) -> dict:
    """run.json（manifest）→ subject 字段表（doc/15 §3.1 + doc/18 §2.1，键序即规范化序）。"""
    prof = (manifest or {}).get("profile") or {}
    content = prof.get("content") if isinstance(prof.get("content"), dict) else {}
    snap = (manifest or {}).get("snapshot") or {}
    cord = snap.get("cordis") if isinstance(snap.get("cordis"), dict) else {}
    pid = str(prof.get("id") or content.get("id") or "")
    ov_model, ov_effort = parse_profile_overrides(pid)
    dsh = content.get("dsh") or {}
    agent = snap.get("agent") or {}
    return {
        "agent": {"name": agent.get("name") or _DEFAULTS["agent"]["name"],
                  "version": agent.get("version")},
        "dsh_profile": dsh.get("profile") or _DEFAULTS["dsh_profile"],
        "model": ov_model if ov_model is not None else (content.get("model") or cord.get("model")),
        "model_provider": content.get("model_provider") or cord.get("model_provider"),
        "persona_sha": cord.get("persona_sha"),
        "plugins_sha": cord.get("plugins_sha"),
        "reasoning_effort": ov_effort if ov_effort is not None
        else (content.get("reasoning_effort") or cord.get("reasoning_effort")),
        "routing": content.get("routing") or {},
        "skill_tool": content.get("skill_tool") or _DEFAULTS["skill_tool"],
        "skills_base": snap.get("skills_base"),
        "skills_mode": snap.get("skills_mode") or content.get("skills_mode"),
    }


def subject_fp_of_manifest(manifest: dict) -> tuple[str, str, int]:
    """→ (subject_fp, subject_json, subject_incomplete)。incomplete：skills_mode 或
    skills_base 缺失（缺省补不出来的关键项）；这两个键保留 None 进指纹（与 TS 端一致），
    但分组时单列「身份不完整」。"""
    subject = subject_of_manifest(manifest)
    incomplete = 1 if (subject["skills_mode"] is None or subject["skills_base"] is None) else 0
    js = json.dumps(subject, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return canonical_sha(subject), js, incomplete
