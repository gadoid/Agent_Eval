"""视图（方案 §10 出口迁移 + 改造方案 §4.3 题目级统计）：能力视图、变体泄露、
难度/区分度/稳定性/题组一致性/历史。"""
from scoring import views
from scoring.policy import EpisodeVerdict, policy_from_dict
from scoring.views import ability_view, question_stats, variant_leak_suspects


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


def _leak_case(turns):
    return {"uid": "x", "case": {
        "conditions": [
            {"slot": "收款状态", "value": "尾款未收", "turn": 1, "keywords": ["尾款"]},
            {"slot": "问点", "value": "电放", "turn": 2, "keywords": ["电放"]}],
        "turns": turns}}


def test_variant_leak_suspects():
    """C5：被替换轮缺少本轮条件关键词 / 含更早轮次条件关键词 → 嫌疑；种子不查。"""
    variant = _leak_case([{"role": "user", "content": "可以放货吗"},          # 缺"尾款"
                          {"role": "user", "content": "那没收钱也能放？"}])   # 缺"电放"、无早期泄漏
    seed = _leak_case([{"role": "user", "content": "完全无关的种子轮"}])      # 种子：同样缺词也不列
    variant["uid"], seed["uid"] = "kr-1#1", "kr-1#0"
    suspects = variant_leak_suspects([seed, variant])
    assert suspects == [
        {"uid": "kr-1#1", "turn": 1, "reason": "第 1 轮缺少条件「收款状态」关键词：尾款"},
        {"uid": "kr-1#1", "turn": 2, "reason": "第 2 轮缺少条件「问点」关键词：电放"}]
    # 更早轮次关键词泄漏
    leak = _leak_case([{"role": "user", "content": "尾款没收，可以电放吗"},
                       {"role": "user", "content": "那尾款没收时也能电放？"}])  # 第 2 轮含第 1 轮的"尾款"
    leak["uid"] = "kr-2#1"
    suspects2 = variant_leak_suspects([leak])
    assert suspects2 == [{"uid": "kr-2#1", "turn": 2, "reason": "第 2 轮含更早轮次条件「收款状态」关键词：尾款"}]


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
