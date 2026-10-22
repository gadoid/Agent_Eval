"""视图（方案 §10 出口迁移）：能力视图的未决与主口径、用例质量视图。"""
from scoring.effective import Review
from scoring.policy import EpisodeVerdict, policy_from_dict
from scoring.views import ability_view, quality_view


def mrow(ep, item_id, kind="required", value="对", source="rule", sha="s1", uid="u1"):
    return {"schema": 1, "run_id": "r", "episode_id": ep, "uid": uid,
            "item": {"kind": kind, "id": item_id, "turn": None},
            "machine": {"value": value, "source": source, "operator": "keyword@1", "reason": ""},
            "trace": ["keyword:hit"], "answer_sha": sha, "status": "ok"}


def test_ability_view_counts_undecided_and_headline():
    policy = policy_from_dict({})
    mk = lambda st: EpisodeVerdict(uid="u", mode="natural", trial=1, dim="知识召回", status=st)
    cell = ability_view([mk("pass"), mk("fail"), mk("undecided"), mk("invalid"), mk("error")], policy)["知识召回|natural"]
    assert cell["n"] == 2 and cell["pass"] == 1
    assert cell["undecided"] == 1 and cell["invalid"] == 1 and cell["error"] == 1
    assert cell["precond_rate"] == 0.6667                    # 分母 = pass+fail+invalid，invalid 记不成立
    assert cell["headline"] is True                          # 知识召回主口径 = natural
    forced_cell = ability_view([EpisodeVerdict(uid="u", mode="forced", trial=1, dim="知识召回", status="pass")],
                               policy)["知识召回|forced"]
    assert forced_cell["headline"] is False


def test_quality_view_mismatch_and_rule_agreement():
    rows = [mrow("e1", "F1"), mrow("e2", "F2")]
    reviews = [Review(run_id="r", episode_id="e1", item_id="F1", value="对", answer_sha="s1"),
               Review(run_id="r", episode_id="e2", item_id="F2", value="错", answer_sha="s1")]
    records = [{"uid": "kr-2#0", "injections_applied": [{"point": "p", "found": 2}]}]
    q = quality_view([], records, rows, reviews)
    assert q["rule_agreement"] == {"agree": 1, "total": 2, "rate": 0.5}
    assert len(q["human_machine_mismatch"]) == 1
    m = q["human_machine_mismatch"][0]
    assert (m["episode_id"], m["item"], m["machine"], m["human"]) == ("e2", "F2", "对", "错")
    assert q["injection_found_gt1"] == ["kr-2#0"]


def test_quality_view_forced_ec_precond_low():
    verdicts = [EpisodeVerdict(uid="ec-9#0", mode="forced", trial=t, dim="错误知识纠正", status="invalid")
                for t in (1, 2)]
    q = quality_view(verdicts, [], [], [])
    assert q["forced_ec_precond_low"] == ["ec-9#0"]
    ok = [EpisodeVerdict(uid="ec-8#0", mode="forced", trial=t, dim="错误知识纠正", status="pass") for t in (1, 2)]
    assert quality_view(ok, [], [], [])["forced_ec_precond_low"] == []
