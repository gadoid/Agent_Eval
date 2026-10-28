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
