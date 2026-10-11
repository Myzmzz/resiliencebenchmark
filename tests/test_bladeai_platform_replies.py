"""How the runtime prepares BladeAI's turn-end replies (platform fixes of 2026-09-17).

Covers three small helpers in ``stage2_service.harness_runtime``:

* prose questions become conversation and are never reviewed as plans;
* several replies after one turn, plus the reasons for rejected cards, go out
  as a single turn;
* ChaosBlade-only plan fields are kept out of the typed plan under review;
* a turn that ends in the intent stage with a plan but no question and no card
  gets a neutral "please continue" instead of being scored as the final answer.
"""

from __future__ import annotations

import json

from stage2_service.harness_runtime import (
    BLADEAI_CONTINUE_LIMIT,
    BLADEAI_CONTINUE_MESSAGE,
    NATIVE_PLAN_EXTRA_FIELDS,
    StructuredFeedback,
    StructuredFeedbackType,
    _bladeai_closing_questions,
    _bladeai_continue_answer,
    _bladeai_conversation_questions,
    _bladeai_stream_position,
    _bladeai_turn_waits_for_go_ahead,
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


def _line(**event) -> bytes:
    return (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")


def test_stream_position_reads_node_phase_and_card_from_one_line():
    assert _bladeai_stream_position(_line(type="token", node="intent_clarification", phase="intent", content="方案")) == (
        "intent_clarification", "intent", False,
    )
    assert _bladeai_stream_position(_line(type="confirm", node="confirmation_gate", payload={"type": "intent_confirm"}))[2] is True
    assert _bladeai_stream_position(_line(type="done")) == (None, None, False)
    # Cut short by the output limit, or not an event at all.
    assert _bladeai_stream_position(b'{"type": "token", "no') == (None, None, False)
    assert _bladeai_stream_position(b"[1, 2]") == (None, None, False)


def _waits(**overrides) -> bool:
    """Round ten r1 by default: intent stage, plan laid out, nothing else."""
    values = {
        "last_node": "intent_clarification",
        "last_phase": "intent",
        "card_raised_this_turn": False,
        "card_approved_in_trial": False,
        "has_reply": False,
        "result_is_valid": False,
        "continues_sent": 0,
    }
    values.update(overrides)
    return _bladeai_turn_waits_for_go_ahead(**values)


def test_a_plan_left_in_the_intent_stage_gets_a_continue():
    """Round ten r1: 87 s in intent_clarification, full plan, no question, no card."""
    assert _waits() is True
    assert _waits(last_node=None) is True
    assert _waits(last_phase=None) is True


def test_no_continue_once_the_agent_has_moved_on_or_been_answered():
    assert _waits(last_node="execution", last_phase="execute") is False
    assert _waits(last_node=None, last_phase=None) is False
    assert _waits(card_raised_this_turn=True) is False
    # After an approved card the experiment is under way; a closing summary in
    # the intent stage must not be pushed towards another injection.
    assert _waits(card_approved_in_trial=True) is False
    assert _waits(has_reply=True) is False
    assert _waits(result_is_valid=True) is False


def test_continues_stop_at_the_limit():
    assert _waits(continues_sent=BLADEAI_CONTINUE_LIMIT - 1) is True
    assert _waits(continues_sent=BLADEAI_CONTINUE_LIMIT) is False


def test_the_continue_reply_approves_nothing_and_does_not_push_towards_execution():
    answer = _bladeai_continue_answer(1)

    assert answer["message"] == BLADEAI_CONTINUE_MESSAGE
    assert "确认卡片" in answer["message"] and "不应执行" in answer["message"]
    assert answer["approved"] is None
    assert answer["answer_mode"] is None
    assert answer["approved_plan"] is None
    assert answer["decision_supplied"] is False
    assert answer["reason"] == "bladeai_continue_requested"
    assert answer["question_id"] != _bladeai_continue_answer(2)["question_id"]


def test_questions_found_in_a_postmortem_turn_are_closing_remarks():
    """Round eleven r2/r4: a finished pipeline, a stale card prompt, optional follow-ups."""
    found = [
        {"topic": "chaos_injection_execution_confirmation", "question": "现在提交该意图，请在弹出的确认卡上核准执行。"},
        {"topic": "frontend_access_availability", "question": "若你确认有可用的 frontend 访问入口，我可以补充调用级验证。"},
    ]

    to_answer, closing = _bladeai_closing_questions(found, "postmortem")

    assert to_answer == []
    assert [item["topic"] for item in closing] == ["chaos_injection_execution_confirmation", "frontend_access_availability"]


def test_questions_before_the_postmortem_stage_are_still_answered():
    found = [{"topic": "pick_target", "question": "A 还是 B？"}]

    for phase in ("intent", "safety", "inject", "verify", None):
        to_answer, closing = _bladeai_closing_questions(found, phase)
        assert to_answer == found
        assert closing == []
