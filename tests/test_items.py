"""题目指纹与变体泄露嫌疑（items.py；R6/R11 验收）。"""
from scoring.items import input_fp_of, standard_fp_of, qkey_of, variant_leak_suspects
from tests.helpers import mk_record


def case_of(**over):
    base = dict(mk_record()["case"])
    base.update(over)
    return base


# ---------------------------------------------------------------- 指纹（步骤 2 + R11）

def test_fingerprint_reference_note_and_tag_changes_do_not_matter():
    c1 = case_of(reference_answer="旧参考", note="旧备注", tags=["a"], status="structured")
    c2 = case_of(reference_answer="新参考", note="新备注", tags=["b"], status="review")
    assert qkey_of(c1) == qkey_of(c2)
    assert standard_fp_of(c1) == standard_fp_of(c2)


def test_fingerprint_topic_changes_qkey():
    """R11：forced 会话的 /skill 前缀由 topic 经 routing 决定，topic 属于实际输入。"""
    c1 = case_of(topic="进单资料审核")
    c2 = case_of(topic="收汇风险")
    assert qkey_of(c1) != qkey_of(c2)
    assert input_fp_of(c1) != input_fp_of(c2)
    assert standard_fp_of(c1) == standard_fp_of(c2)


def test_fingerprint_fact_text_changes_judge_fp_only():
    c1 = case_of()
    c2 = case_of(facts=[dict(c1["facts"][0], text="改写后的事实文本"),
                        dict(c1["facts"][1], text="未收尾款也可以先电放")])
    assert standard_fp_of(c1) != standard_fp_of(c2)
    assert qkey_of(c1) == qkey_of(c2)                # 输入没变 → 同一题


def test_fingerprint_user_message_changes_qkey():
    c1 = case_of(turns=[{"role": "user", "content": "问题一"}])
    c2 = case_of(turns=[{"role": "user", "content": "问题一改写"}])
    assert qkey_of(c1) != qkey_of(c2)
    assert input_fp_of(c1) != input_fp_of(c2)
    assert standard_fp_of(c1) == standard_fp_of(c2)


# ---------------------------------------------------------------- 变体泄露嫌疑（C5，R6 修订）

def variant_record(uid, turns, conditions, variant=None):
    return {"uid": uid, "case": {"turns": turns, "conditions": conditions, "variant": variant}}


COND_TURNLESS = [{"slot": "地区", "value": "江苏", "turn": None, "keywords": ["江苏"]}]
COND_TURNS = [
    {"slot": "收款状态", "value": "尾款未收", "turn": 1, "keywords": ["尾款"]},
    {"slot": "问点", "value": "电放", "turn": 2, "keywords": ["电放"]},
]


def test_no_false_positive_on_turnless_condition():
    """R6 误报一：条件没写轮次 → 视为最后一轮，不属于"更早轮次"，单轮变体不报。"""
    rec = variant_record("kr-1#1", [{"role": "user", "content": "江苏那边的单子能做舱单吗？"}],
                         COND_TURNLESS, variant={"scope": "turn", "rewrite": "口语"})
    assert variant_leak_suspects([rec]) == []        # 关键词"江苏"就在本轮，不缺也不泄


def test_no_false_positive_on_unreplaced_turn():
    """R6 误报二：scope=turn 只查被替换的轮次，未替换的第 3 轮含第 1 轮关键词不报。
    被替换的第 2 轮缺本轮条件关键词属于真实嫌疑，仍要报。"""
    rec = variant_record(
        "kr-2#1",
        [{"role": "user", "content": "尾款没收的电放单怎么处理"},   # 第 1 轮（未替换）
         {"role": "user", "content": "改成口语的那一问"},           # 第 2 轮（被替换）
         {"role": "user", "content": "那 10 月那单呢"}],            # 第 3 轮（未替换）
        [{"slot": "出运", "value": "10 月", "turn": 1, "keywords": ["10 月"]},
         {"slot": "问点", "value": "电放", "turn": 2, "keywords": ["电放"]}],
        variant={"scope": "turn", "turn": 2, "rewrite": "口语"})
    assert variant_leak_suspects([rec]) == [
        {"uid": "kr-2#1", "turn": 2, "reason": "第 2 轮缺少条件「问点」关键词：电放"}]


def test_replaced_turn_missing_and_earlier_leak_reported():
    """正向：被替换轮缺本轮条件关键词 / 含更早轮次条件关键词 → 报出（按条件顺序）。"""
    rec = variant_record(
        "kr-3#1",
        [{"role": "user", "content": "尾款没收，可以电放吗"},        # 第 1 轮（未替换）
         {"role": "user", "content": "那尾款没收时也能放吗"}],       # 第 2 轮（被替换）
        COND_TURNS,
        variant={"scope": "turn", "turn": 2, "rewrite": "口语"})
    suspects = variant_leak_suspects([rec])
    assert suspects == [
        {"uid": "kr-3#1", "turn": 2, "reason": "第 2 轮含更早轮次条件「收款状态」关键词：尾款"},
        {"uid": "kr-3#1", "turn": 2, "reason": "第 2 轮缺少条件「问点」关键词：电放"},
    ]


def test_scope_whole_checks_all_turns_and_turnless_condition_is_last_turn():
    rec = variant_record(
        "kr-4#1",
        [{"role": "user", "content": "换成别的问法"},                 # 第 1 轮：缺"尾款"
         {"role": "user", "content": "尾款没收，可以电放吗"}],        # 第 2 轮：两词都在
        COND_TURNS,
        variant={"scope": "whole", "rewrite": "正式"})
    suspects = variant_leak_suspects([rec])
    assert suspects == [
        {"uid": "kr-4#1", "turn": 1, "reason": "第 1 轮缺少条件「收款状态」关键词：尾款"},
        {"uid": "kr-4#1", "turn": 2, "reason": "第 2 轮含更早轮次条件「收款状态」关键词：尾款"},
    ]


def test_seed_not_checked():
    rec = variant_record("kr-5#0", [{"role": "user", "content": "完全无关"}], COND_TURNS,
                         variant={"scope": "whole"})
    assert variant_leak_suspects([rec]) == []


def test_weight_changes_judge_fp_not_item_sha():
    from scoring.items import Item, item_sha_of, standard_fp_of, weight_of
    from tests.helpers import fact, mk_record
    case = mk_record()["case"]
    weighted = dict(case, facts=[dict(f, weight=2) if f["id"] == "F1" else f for f in case["facts"]])
    assert standard_fp_of(case) == standard_fp_of(weighted)   # v8：权重移出标准（doc/15 §3.2）
    f1 = next(f for f in case["facts"] if f["id"] == "F1")
    a = Item("required", "F1", f1["text"], f1["turn"], f1)
    b = Item("required", "F1", f1["text"], f1["turn"], dict(f1, weight=2))
    assert item_sha_of(a) == item_sha_of(b)
    assert weight_of(a, 1) == 1 and weight_of(b, 1) == 2 and weight_of(Item("expected", "E1", "", None, fact("E1", weight=0)), 1) == 0


def test_version_label_shortens_paths():
    """评测集版本短名（比较轴/配置分组不再出现文件全路径）。"""
    from scoring.items import version_label
    assert version_label(r"D:\Agent_Eval\evalset\dist\evalset-v20261022-1.jsonl") == "v20261022-1"
    assert version_label("D:/x/out/adhoc/dist/evalset-adhoc-202610220540-58.jsonl") == "adhoc-202610220540-58"
    assert version_label("v20261022-1") == "v20261022-1"
    assert version_label(None) == "?"


def test_evalset_version_of_prefers_version_shortens_file():
    from scoring.store import evalset_version_of
    assert evalset_version_of({"snapshot": {"evalset": {"version": "v1", "file": "D:/x/evalset-v1.jsonl"}}}) == "v1"
    assert evalset_version_of({"snapshot": {"evalset": {"file": "D:/x/evalset-adhoc-1.jsonl"}}}) == "adhoc-1"
    assert evalset_version_of({}) == "?"


def test_attr_value_normalizes_evalset_version():
    from scoring.views import attr_value
    row = {"profile_id": "p", "evalset_version": "D:/a/b/evalset-v20261022-1.jsonl"}
    assert attr_value(row, "evalset_version") == "v20261022-1"
    assert attr_value(row, "profile") == "p"
