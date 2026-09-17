"""How the runtime prepares BladeAI's turn-end replies (platform fixes of 2026-09-17).

Covers three small helpers in ``stage2_service.harness_runtime``:

* prose questions become conversation and are never reviewed as plans;
* several replies after one turn, plus the reasons for rejected cards, go out
  as a single turn;
* ChaosBlade-only plan fields are kept out of the typed plan under review.
"""

from __future__ import annotations

from stage2_service.harness_runtime import (
    NATIVE_PLAN_EXTRA_FIELDS,
    StructuredFeedback,
    StructuredFeedbackType,
    _bladeai_conversation_questions,
    _merge_bladeai_replies,
    _split_native_plan_extras,
)
from stage2_service.simulated_user import CONVERSATION_REQUEST_KIND


def _reply(question_id: str, message: str) -> StructuredFeedback:
    return StructuredFeedback(
        category=StructuredFeedbackType.USER_DECISION,
        message=message,
        payload={"event_type": "USER_DECISION", "question_id": question_id},
    )


def test_prose_questions_become_conversation_and_carry_no_injected_plan():
    """Round nine wrote one recovered plan into every question; that recovery is gone."""
    interpreted = [
        {
            "topic": "final_injection_decision",
            "question": "现在提交该意图，请在弹出的确认卡片中做最终决策。",
            "request_kind": "confirmation",
            "recommendation": None,
        },
        {
            "topic": "recover_old_experiment",
            "question": "是否先回收旧实验？",
            "request_kind": "confirmation",
            "recommendation": None,
        },
        {"topic": "empty", "question": ""},
        "not a question",
    ]

    marked = _bladeai_conversation_questions(interpreted)

    assert [item["topic"] for item in marked] == ["final_injection_decision", "recover_old_experiment"]
    assert all(item["request_kind"] == CONVERSATION_REQUEST_KIND for item in marked)
    assert all(item["recommendation"] is None for item in marked)


def test_a_single_reply_without_card_reasons_is_sent_unchanged():
    only = _reply("q-1", "好的，请提交确认卡片。")

    assert _merge_bladeai_replies([("请在卡片中做决策。", only)], []) == [only]


def test_several_replies_and_card_reasons_go_out_as_one_turn():
    """In round nine two answers went out one turn apart and the second never left."""
    merged = _merge_bladeai_replies(
        [
            ("是否先回收旧实验？", _reply("q-1", "可以，先回收。")),
            ("修正后的计划是否批准？", _reply("q-2", "请提交确认卡片。")),
        ],
        ["不批准：持续时长超过 300 秒。"],
    )

    assert len(merged) == 1
    text = merged[0].message
    assert text.index("被拒绝的确认卡片") < text.index("是否先回收旧实验") < text.index("修正后的计划")
    assert "可以，先回收。" in text
    assert "请提交确认卡片。" in text
    assert "持续时长超过 300 秒" in text
    assert merged[0].category is StructuredFeedbackType.USER_DECISION
    assert merged[0].payload["merged_reply_count"] == 2
    assert merged[0].payload["rejection_explanation_count"] == 1
    assert merged[0].payload["merged_question_ids"] == ["q-1", "q-2"]


def test_a_card_reason_alone_is_still_sent():
    merged = _merge_bladeai_replies([], ["不批准：目标不是 cart。"])

    assert len(merged) == 1
    assert "目标不是 cart" in merged[0].message


def test_blank_card_reasons_do_not_create_a_turn():
    assert _merge_bladeai_replies([], ["  ", ""]) == []


def test_chaosblade_only_fields_are_moved_out_of_the_reviewed_plan():
    """AgentPlan forbids unknown fields, so these failed a card before it was read."""
    plan = {
        "fault_type": "cpu-load",
        "intensity": {"cpu_percent": 80},
        "additional_native_constraints": {"--cpu-count": "1"},
        "native_params": {"cpu-percent": "80", "cpu-count": "1"},
    }

    reviewed, extras = _split_native_plan_extras(plan)

    assert reviewed == {"fault_type": "cpu-load", "intensity": {"cpu_percent": 80}}
    assert set(extras) == set(NATIVE_PLAN_EXTRA_FIELDS)
    # The caller's dict is left untouched.
    assert "additional_native_constraints" in plan


def test_an_empty_or_missing_plan_splits_into_two_empty_dicts():
    assert _split_native_plan_extras(None) == ({}, {})
    assert _split_native_plan_extras({}) == ({}, {})
