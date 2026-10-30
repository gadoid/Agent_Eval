"""被测指纹双实现对拍（doc/15 §7.4）：与 runner/tests/subject.test.ts 共用
tests/fixtures/subject_manifest.json，两侧必须得出相同 subject_fp。"""
import json
from pathlib import Path

from scoring.subject import parse_profile_overrides, subject_fp_of_manifest, subject_of_manifest

FIXTURE = Path(__file__).parent / "fixtures" / "subject_manifest.json"


def _cases() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


def test_dual_impl_fingerprints():
    for c in _cases():
        fp, js, incomplete = subject_fp_of_manifest(c["manifest"])
        assert fp == c["expected_subject_fp"], c["name"]
        assert bool(incomplete) == c["expected_incomplete"], c["name"]


def test_profile_suffix_override_parsing():
    assert parse_profile_overrides("eline-glm53-defaultroots@glm-5.3-low") == ("glm-5.3", "low")
    assert parse_profile_overrides("p@deepseek-v4-flash-high") == ("deepseek-v4-flash", "high")
    assert parse_profile_overrides("p@nosuffix") == ("nosuffix", None)
    assert parse_profile_overrides("plain-id") == (None, None)


def test_defaults_and_incomplete():
    subject = subject_of_manifest({"profile": {"id": "x", "content": {}}, "snapshot": {}})
    assert subject["dsh_profile"] == "headless"
    assert subject["skill_tool"] == "skill"
    assert subject["agent"] == {"name": "dsh-cli", "version": None}
    assert subject["model"] is None
    fp, _, incomplete = subject_fp_of_manifest({"profile": {"id": "x"}, "snapshot": {}})
    assert incomplete == 1 and fp


# ---------------------------------------------------------------- doc/18 §2.1：cordis 段

CORD = {"persona_sha": "pp1", "plugins_sha": "gg1", "model": "glm-5.3",
        "reasoning_effort": "high", "model_provider": "deepseek-account"}


def test_subject_reads_cordis_section():
    """model/effort/provider 取有效值（覆盖 > cordis）；persona/plugins 读 snapshot.cordis。"""
    s = subject_of_manifest({"profile": {"id": "p", "content": {}}, "snapshot": {"cordis": CORD}})
    assert s["model"] == "glm-5.3" and s["reasoning_effort"] == "high"
    assert s["model_provider"] == "deepseek-account"
    assert s["persona_sha"] == "pp1" and s["plugins_sha"] == "gg1"
    # CLI 覆盖（profile id 后缀）优先于 cordis
    s2 = subject_of_manifest({"profile": {"id": "p@deepseek-v4-flash-medium", "content": {}},
                              "snapshot": {"cordis": CORD}})
    assert s2["model"] == "deepseek-v4-flash" and s2["reasoning_effort"] == "medium"
    # profile 显式配置同样优先于 cordis
    s3 = subject_of_manifest({"profile": {"id": "p", "content": {"model": "glm-5.2", "reasoning_effort": "low"}},
                              "snapshot": {"cordis": CORD}})
    assert s3["model"] == "glm-5.2" and s3["reasoning_effort"] == "low"
    # 旧 manifest（无 cordis 段）：各字段 null（bundle 盲区 / 旧环境），不写 unknown
    s4 = subject_of_manifest({"profile": {"id": "p", "content": {}}, "snapshot": {}})
    assert s4["model"] is None and s4["persona_sha"] is None and s4["plugins_sha"] is None


def test_same_effective_model_same_fp():
    """doc/18 §1 差距修正：同有效模型（CLI 覆盖 vs cordis 缺省同值）→ 同 subject_fp，不再假分裂。"""
    over = {"profile": {"id": "p@glm-5.3-high", "content": {}}, "snapshot": {"cordis": CORD}}
    plain = {"profile": {"id": "p", "content": {}}, "snapshot": {"cordis": CORD}}
    assert subject_fp_of_manifest(over)[0] == subject_fp_of_manifest(plain)[0]
