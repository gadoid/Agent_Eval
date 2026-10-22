"""有效结论合成（方案 §6、§12 步骤 2 验收）：覆盖、last-wins、作废清单。"""
from scoring.effective import Review, applied_count, effective_final, voided_reviews


def row(kind="required", item_id="F1", value="未提及", answer_sha="abc", episode_id="e1"):
    return {"schema": 1, "run_id": "r", "episode_id": episode_id, "uid": "u",
            "item": {"kind": kind, "id": item_id, "turn": None},
            "machine": {"value": value, "source": "judge", "operator": "f@1", "reason": ""},
            "trace": [], "answer_sha": answer_sha, "status": "ok"}


def review(value="对", sha="abc", item_id="F1", episode_id="e1", at="t1"):
    return Review(run_id="r", episode_id=episode_id, item_id=item_id, value=value,
                  answer_sha=sha, reason="为什么", reviewer="codfish", at=at)


def test_no_review_returns_machine():
    v = effective_final(row(), [], "abc")
    assert v.value == "未提及" and v.source == "judge" and v.operator == "f@1"


def test_review_overrides_and_last_wins():
    v = effective_final(row(), [review(value="部分", at="t1"), review(value="对", at="t2")], "abc")
    assert v.value == "对" and v.source == "human" and v.operator == "review" and v.reason == "为什么"


def test_sha_mismatch_is_void():
    v = effective_final(row(), [review(sha="其他")], "abc")
    assert v.value == "未提及" and v.source == "judge"


def test_illegal_value_is_void():
    v = effective_final(row(kind="path", item_id="path#1", value="满足"),
                        [review(value="对", item_id="path#1")], "abc")   # path 的 scale 不含"对"
    assert v.value == "满足" and v.source == "judge"


def test_voided_reviews_list():
    rows = [row()]
    voided = voided_reviews(rows, [review(sha="旧", at="t1"), review(value="对", at="t2")])
    assert len(voided) == 1 and "answer_sha" in voided[0]["reason"]
    assert voided_reviews(rows, [review(value="对")]) == []


def test_applied_count():
    rows = [row(item_id="F1"), row(item_id="X1", kind="forbidden", value="未采信")]
    reviews = [review(item_id="F1"), review(value="采信", item_id="X1")]
    assert applied_count(rows, reviews) == 2
    assert applied_count(rows, [review(item_id="F1", sha="mismatch")]) == 0


def test_review_only_applies_to_its_item():
    """同会话内同类型、同回答哈希的条目不得吃掉彼此的改判（真实多事实数据暴露过的回归）。"""
    rows = [row(item_id="F1"), row(item_id="F2"), row(item_id="F3")]
    reviews = [review(item_id="F1")]
    assert effective_final(rows[0], reviews, "abc").source == "human"
    v2 = effective_final(rows[1], reviews, "abc")
    assert v2.source == "judge" and v2.value == "未提及"
    assert effective_final(rows[2], reviews, "abc").value == "未提及"
    assert applied_count(rows, reviews) == 1
