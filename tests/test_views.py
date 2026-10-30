"""视图（方案 §10 出口迁移 + 改造方案 §4.3 题目级统计）：能力视图、
难度/区分度/稳定性/题组一致性/历史。变体泄露嫌疑见 test_items。"""
from scoring import views
from scoring.rules import rules_from_dict
from scoring.rules import EpisodeVerdict
from scoring.views import ability_view, question_stats


def test_ability_view_counts_undecided_and_headline():
    policy = rules_from_dict({})
    mk = lambda st: EpisodeVerdict(uid="u", mode="natural", trial=1, dim="知识召回", status=st)
    cell = ability_view([mk("pass"), mk("fail"), mk("undecided"), mk("invalid"), mk("error")], policy)["知识召回|natural"]
    assert cell["n"] == 2 and cell["pass"] == 1
    assert cell["undecided"] == 1 and cell["invalid"] == 1 and cell["error"] == 1
    assert cell["precond_rate"] == 0.6667                    # 分母 = pass+fail+invalid，invalid 记不成立
    assert cell["headline"] is True                          # 知识召回主口径 = natural
    forced_cell = ability_view([EpisodeVerdict(uid="u", mode="forced", trial=1, dim="知识召回", status="pass")],
                               policy)["知识召回|forced"]
    assert forced_cell["headline"] is False


# ---------------------------------------------------------------- 题目级统计（§4.3）

def arow(qkey, model="glm-5.3", mode="natural", status="pass", vi=0, rewrite=None,
         skills=("default-roots", "s1"), run="a", case="c1"):
    return {"qkey": qkey, "models_json": f'["{model}"]', "mode": mode, "trial": 1,
            "c_status": status, "variant_index": vi, "rewrite": rewrite,
            "skills_mode": skills[0], "skills_base": skills[1], "run_id": run,
            "detail_json": "{}", "uid": qkey, "dim": "d", "topic": "t", "case_id": case}


def test_question_stats_difficulty_discrimination_stability():
    rows = [
        arow("q1", model="m1", status="pass"),
        arow("q1", model="m1", status="pass"),
        arow("q1", model="m1", status="fail"),                       # m1 稳定性：3 中 2 一致
        arow("q1", model="m2", status="fail"),
        arow("q1", model="m2", status="fail"),
    ]
    s = question_stats(rows)["q1"]
    assert s["judged"] == 5 and s["difficulty"] == round(1 - 2 / 5, 4)
    assert s["models_n"] == 2
    assert s["discrimination"] == round(2 / 3 - 0.0, 4)              # m1 2/3 − m2 0
    assert s["stability"] == round((2 + 2) / 5, 4)                   # 各组最大一致数 / 总数
    assert s["pending"] == 0


def test_question_stats_group_consistency_by_case():
    """种子与非诱导变体按 case_id + 模型 + 干预配对；诱导变体不计入（§4.3）。"""
    rows = [
        arow("kr-1#0", vi=0, status="pass"),
        arow("kr-1#1", vi=1, rewrite="口语", status="pass"),         # 一致
        arow("kr-1#2", vi=2, rewrite="诱导", status="fail"),         # 诱导不计入
        arow("kr-1#3", vi=3, rewrite="正式", status="fail"),         # 不一致
        arow("kr-9#0", vi=0, case="c9", status="pass"),              # 无变体 → 无配对
    ]
    stats = question_stats(rows)
    assert stats["kr-1#0"]["group_pairs"] == 2
    assert stats["kr-1#0"]["group_consistency"] == 0.5
    assert stats["kr-1#3"]["group_consistency"] == 0.5               # 组值同挂各成员
    assert stats["kr-9#0"]["group_consistency"] is None


def test_question_stats_history_by_env():
    rows = [
        arow("q1", skills=("default-roots", "s1"), run="a", status="pass"),
        arow("q1", skills=("default-roots", "s2"), run="b", status="fail"),
    ]
    h = question_stats(rows, run_order={"a": 0, "b": 1})["q1"]["history"]
    assert [e["env"] for e in h] == ["default-roots/s1", "default-roots/s2"]
    assert h[0]["pass_rate"] == 1.0 and h[1]["pass_rate"] == 0.0


def test_model_label_and_verdicts_of():
    r = arow("q1")
    assert views.model_label(r) == "glm-5.3"
    assert views.model_label({**r, "models_json": None}) == "?"
    assert views.verdicts_of([arow("q1", status=None)]) == []


# ---------------------------------------------------------------- 百分制聚合（doc/10 §6.3–6.4 样例复现）

def _srow(qkey, case, dim, score, mode="forced", trial=1, vi=0, rewrite=None,
          skills_base="s1", profile="p1", **over):
    status = "pass" if score is not None and score >= 60 else ("invalid" if score == -1 else "fail")
    d = {"qkey": qkey, "uid": qkey, "case_id": case, "dim": dim, "mode": mode, "trial": trial,
         "c_status": None if score is None else ("invalid" if score == -1 else ("pass" if score >= 60 else "fail")),
         "c_score": None if score in (None, -1) else score, "variant_index": vi, "rewrite": rewrite,
         "profile_id": profile, "models_json": '["m"]', "skills_mode": "default-roots",
         "skills_base": skills_base, "evalset_version": "v1", "run_id": "r1"}
    d.update(over)
    return d


def test_dimension_and_composite_scores_doc_sample():
    """doc/10 §6.3 样例：错误知识纠正 62.5，四维度等权 → 综合分 73.9。"""
    policy = rules_from_dict({})
    rows = (
        # ec-0201：trial 89、54、invalid → 题目分 71.5
        [_srow("q1|a", "ec-0201", "错误知识纠正", 89), _srow("q1|a", "ec-0201", "错误知识纠正", 54, trial=2),
         _srow("q1|a", "ec-0201", "错误知识纠正", -1, trial=3)]
        # ec-0202：种子 78（两次平均 80/76）；诱导 V1 不计入维度分（D-7；§6.3 样例 75 与此矛盾，以决策表为准）
        + [_srow("q2|a", "ec-0202", "错误知识纠正", 80), _srow("q2|a", "ec-0202", "错误知识纠正", 76, trial=2),
           _srow("q3|a", "ec-0202", "错误知识纠正", 70, vi=1, rewrite="诱导"),
           _srow("q3|a", "ec-0202", "错误知识纠正", 74, vi=1, rewrite="诱导", trial=2)]
        # ec-0203 → 41；其余三维度 78.2 / 70.0 / 85.0（每个用例一个题）
        + [_srow("q4|a", "ec-0203", "错误知识纠正", 41)]
        + [_srow(f"k{i}|a", f"kr-{i}", "知识召回", 78.2, mode="natural") for i in range(1)]
        + [_srow(f"m{i}|a", f"mt-{i}", "多轮上下文召回", 70.0, mode="natural") for i in range(1)]
        + [_srow(f"s{i}|a", f"sc-{i}", "同义请求一致性", 85.0, mode="natural") for i in range(1)]
    )
    dims = views.dimension_scores(rows, policy)
    assert dims["错误知识纠正"]["score"] == 63.5
    comp = views.composite(dims, policy, known_dims=set(dims))
    assert comp["score"] == 74.2 and comp["complete"] is True
    # 题目分与用例分的中间量
    qs = views.question_scores(rows, policy)
    assert qs["q1|a"]["score"] == 71.5 and qs["q1|a"]["n"] == 2      # invalid 排除
    cs = views.case_scores(rows, policy)
    assert cs["ec-0202"]["score"] == 78.0 and cs["ec-0202"]["n"] == 1  # 诱导变体不计维度分（D-7）


def test_compare_scores_single_component():
    """doc/10 §6.4：单组件综合分差 = 各维度差按维度权重平均（样例 78.2→81.3 等 → +2.8）。"""
    policy = rules_from_dict({})
    dims = ["知识召回", "多轮上下文召回", "同义请求一致性", "错误知识纠正"]
    base_v = [78.2, 70.0, 85.0, 62.5]
    tgt_v = [81.3, 68.8, 85.4, 71.5]
    rows = []
    for i, d in enumerate(dims):                              # 同一批题在两个 skills_base 下各跑一次
        rows.append(_srow(f"q{i}|a", f"c{i}", d, base_v[i], mode="natural", skills_base="v1"))
        rows.append(_srow(f"q{i}|a", f"c{i}", d, tgt_v[i], mode="natural", skills_base="v2"))
    rep = views.compare_scores(rows, axis="skills_base", base="v1", target="v2", rules=policy)
    assert rep["n_shared"] == 4
    comp = rep["contributions"][0]
    assert comp["delta"] == 2.8
    by_name = {c["name"]: c for c in rep["contributions"]}
    assert by_name["错误知识纠正"]["delta"] == 9.0
    assert by_name["多轮上下文召回"]["delta"] == -1.2


def test_compare_scores_cost_and_small_sample():
    """doc/18 §5/§8：成本差 = 同题配对差的中位数（区间与分数差同一批重采样）；
    小样本标注只影响展示；无成本数据（旧 run）→ cost 为 None。"""
    policy = rules_from_dict({})
    rows = []
    base_tok, tgt_tok = [100, 200, 400], [150, 100, 500]       # 相对差 +50/−50/+25% → 中位 +25%
    base_dur, tgt_dur = [1000, 2000, 4000], [2000, 1000, 4000]  # +100/−50/0% → 中位 0%
    for i in range(3):
        rows.append(_srow(f"q{i}|a", f"c{i}", "知识召回", 80, mode="natural", skills_base="v1",
                          tokens_total=base_tok[i], duration_ms=base_dur[i]))
        rows.append(_srow(f"q{i}|a", f"c{i}", "知识召回", 80, mode="natural", skills_base="v2",
                          tokens_total=tgt_tok[i], duration_ms=tgt_dur[i]))
    rep = views.compare_scores(rows, axis="skills_base", base="v1", target="v2", rules=policy)
    assert rep["small_sample"] is True                          # 3 题 < 30
    cost = rep["cost"]
    assert cost["tokens"]["delta_pct"] == 25.0
    assert cost["duration"]["delta_pct"] == 0.0
    assert cost["tokens"]["lo"] <= 25.0 <= cost["tokens"]["hi"]
    rows_nc = [{k: v for k, v in r.items() if k not in ("tokens_total", "duration_ms")} for r in rows]
    assert views.compare_scores(rows_nc, axis="skills_base", base="v1", target="v2",
                                rules=policy)["cost"] is None


def test_shapley_doc_sample():
    """doc/10 §6.4 样例：2×2 全因子（66/70/71/78）→ φ = +5.5 / +6.5，合计 +12.0。"""
    cells = {frozenset(): 66.0, frozenset({"model"}): 70.0,
             frozenset({"skills_base"}): 71.0, frozenset({"model", "skills_base"}): 78.0}
    res = views.shapley(cells, ["model", "skills_base"])
    assert res["phi"] == {"model": 5.5, "skills_base": 6.5}
    assert res["total"] == 12.0
    missing = views.shapley({frozenset(): 66.0}, ["model", "skills_base"])
    assert missing["missing"] and any("model" in m for m in missing["missing"])


# ---------------------------------------------------------------- 评审整改回归（2026-10）

def test_question_scores_provisional_on_unconverged():
    """P1：结论已定但区间未收敛（pass / score=None）与 error 的会话被排除出均分，
    题目分与维度分要标临时；invalid 是合规排除，不算。"""
    policy = rules_from_dict({})
    r90 = _srow("q1|a", "c1", "知识召回", 90, mode="natural")
    rows = [r90, {**r90, "trial": 2, "c_score": None, "c_status": "pass"}]
    qs = views.question_scores(rows, policy)
    assert qs["q1|a"]["score"] == 90 and qs["q1|a"]["n"] == 1
    assert qs["q1|a"]["provisional"] is True
    rows_err = [r90, {**r90, "trial": 2, "c_score": None, "c_status": "error"}]
    assert views.question_scores(rows_err, policy)["q1|a"]["provisional"] is True
    rows_inv = [r90, _srow("q1|a", "c1", "知识召回", -1, mode="natural", trial=3)]
    assert views.question_scores(rows_inv, policy)["q1|a"]["provisional"] is False
    dims = views.dimension_scores(rows, policy)
    assert dims["知识召回"]["provisional"] is True


def test_dim_delta_respects_resample_multiplicity():
    """P1：配对自助法重采样含重复用例时要按重数计——[c1, c2, c2] 的对照均值是
    (80+50+50)/3 = 60（不是集合意义上的 65）。per_case 值为 [Σ分, 条数]。"""
    per_case = {"v1": {"c1": {"知识召回": [20.0, 1]}, "c2": {"知识召回": [20.0, 1]}},
                "v2": {"c1": {"知识召回": [80.0, 1]}, "c2": {"知识召回": [50.0, 1]}}}
    assert views._dim_delta(["c1", "c2", "c2"], "知识召回", per_case, "v1", "v2") == 40.0
    assert views._dim_delta(["c1", "c2"], "知识召回", per_case, "v1", "v2") == 45.0


def test_compare_scores_selects_headline_mode_per_question():
    """P1：mode 放开时同一 qkey 的 natural 与 forced 不混算——两侧都有主口径
    （ec → forced）只取主口径；一侧没有则该题按两侧 mode 不一致排除。"""
    policy = rules_from_dict({})
    rows = [
        _srow("q1|a", "c1", "错误知识纠正", 90, mode="forced", skills_base="v1"),
        _srow("q1|a", "c1", "错误知识纠正", 50, mode="forced", skills_base="v2"),
        _srow("q1|a", "c1", "错误知识纠正", 70, mode="natural", skills_base="v1"),  # 非主口径混入
    ]
    rep = views.compare_scores(rows, axis="skills_base", base="v1", target="v2", rules=policy)
    assert rep["n_shared"] == 1
    assert rep["metrics"]["v1"]["dims"]["错误知识纠正"]["score"] == 90     # 不与 natural 70 平均
    assert rep["metrics"]["v2"]["dims"]["错误知识纠正"]["score"] == 50
    mismatched = [
        _srow("q2|a", "c2", "错误知识纠正", 80, mode="natural", skills_base="v1"),
        _srow("q2|a", "c2", "错误知识纠正", 90, mode="forced", skills_base="v2"),
    ]
    rep2 = views.compare_scores(mismatched, axis="skills_base", base="v1", target="v2", rules=policy)
    assert rep2["n_shared"] == 0
    assert any("两侧干预模式不一致" in e["reason"] for e in rep2["excluded_qkeys"])


def test_shapley_cells_aligned_on_shared_questions():
    """P1：各格只在共有题目上算分（单侧题不出现在任何格），mode 不作组件时逐题
    按主口径选 mode——各格综合分出自同一题集，Shapley 才可比。"""
    policy = rules_from_dict({})
    rows = (
        [_srow("q1|a", "c1", "知识召回", 80, mode="natural", skills_base="v1"),
         _srow("q1|a", "c1", "知识召回", 40, mode="natural", skills_base="v2")]
        + [_srow("q2|a", "c2", "多轮上下文召回", 60, mode="natural", skills_base="v1")]   # 单侧题
        # ec 主口径 forced：v1 侧混入 natural 70，不与 forced 90 平均
        + [_srow("q3|a", "c3", "错误知识纠正", 90, mode="forced", skills_base="v1"),
           _srow("q3|a", "c3", "错误知识纠正", 70, mode="natural", skills_base="v1"),
           _srow("q3|a", "c3", "错误知识纠正", 50, mode="forced", skills_base="v2")]
    )
    cells, shared = views.shapley_cells(rows, ["skills_base"], policy)
    assert shared == {"q1|a", "q3|a"}
    assert cells[frozenset()] == round((80 + 90) / 2, 1)      # v1：q1 80 + q3 主口径 90
    assert cells[frozenset({"skills_base"})] == round((40 + 50) / 2, 1)
    res = views.shapley(cells, ["skills_base"])
    assert res["total"] == round(45.0 - 85.0, 1)
