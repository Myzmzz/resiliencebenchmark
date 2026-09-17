"""WP-C.1/C.2: the three confirmation gates, answered on the right channel."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from stage2_service.harness_adapters.base import Question
from stage2_service.harness_adapters.bladeai_confirm import (
    APPROVAL_WORD,
    BladeAIConfirmBridge,
    GateDecision,
    UnknownGateError,
    classify_target_change,
    plan_from_intent,
)


class FakeClient:
    def __init__(self, delivered: bool = True):
        self.delivered = delivered
        self.interrupts: list[tuple[str, str, str]] = []
        self.confirms: list[tuple[str, str, str]] = []

    def answer_interrupt(self, session_id, interrupt_id, answer):
        self.interrupts.append((session_id, interrupt_id, answer))
        return {"ok": True, "delivered": self.delivered}

    def confirm_task(self, task_id, action, *, reason="", timeout=None):
        self.confirms.append((task_id, action, reason))
        return {"status": "success"}


def question(kind: str, qid: str = "turn-1", **recommendation) -> Question:
    return Question(
        question_id=qid, version=1, request_kind=kind,
        recommendation=recommendation,
        occurred_at=datetime.now(timezone.utc),
    )


def bridge(client, decide=None, **kwargs):
    return BladeAIConfirmBridge(
        client, "sess-1",
        decide=decide or (lambda q: GateDecision(approved=True, reason="in_scope")),
        **kwargs,
    )


# ---- channel routing ----------------------------------------------------


def test_intent_gate_answers_on_interrupt_with_the_events_task_id() -> None:
    client = FakeClient()
    answer = bridge(client).answer(question("intent", "turn-1b4f6902e392"))
    assert client.interrupts == [("sess-1", "turn-1b4f6902e392", APPROVAL_WORD)]
    assert client.confirms == []
    assert answer.channel == "interrupt"
    assert answer.delivered is True


def test_execution_gate_answers_on_interrupt_keyed_by_the_turn_id() -> None:
    """A /turn session waits for the execution gate on the same interrupt future.

    ``/confirm/{task_id}`` resumes the graph thread named in the path; given the
    turn id it re-planned from a checkpoint without a fault spec (round 4).
    """
    client = FakeClient()
    answer = bridge(client).answer(question("execution", "turn-e708412252a4"))
    assert client.interrupts == [("sess-1", "turn-e708412252a4", APPROVAL_WORD)]
    assert client.confirms == []
    assert answer.channel == "interrupt"
    assert answer.delivered is True


def test_execution_gate_falls_back_to_confirm_only_when_no_interrupt_waits() -> None:
    client = FakeClient(delivered=False)
    answer = bridge(client).answer(question("execution", "turn-e708412252a4"))
    assert client.interrupts == [("sess-1", "turn-e708412252a4", APPROVAL_WORD)]
    assert client.confirms == [("turn-e708412252a4", "approve", "in_scope")]
    assert answer.channel == "confirm_fallback"


def test_unknown_gate_is_refused_rather_than_guessed() -> None:
    """A gate with no ruling must stop the bridge, not be answered blindly."""
    with pytest.raises(UnknownGateError, match="no ruling"):
        bridge(FakeClient()).answer(
            question("unknown:some_future_gate", gate_node="some_future_gate")
        )


# ---- the whitelist (F1) -------------------------------------------------


def test_only_a_whitelist_word_is_sent_and_reasoning_is_held_back() -> None:
    """An approval carrying an explanation is read by the server as a rejection."""
    client = FakeClient()
    b = bridge(client, decide=lambda q: GateDecision(
        approved=True, reason="in_scope",
        explanation="同意：cpu-percent 80、cpu-count 1，范围仅限 cart 这一个 Pod。",
    ))
    b.answer(question("intent"))

    sent = client.interrupts[0][2]
    assert sent == "approved"
    # Not one extra character rides along with the verdict.
    assert "cpu" not in sent and "同意" not in sent
    # The reasoning is kept for a later ordinary turn instead of being lost.
    assert b.drain_explanations() == [
        "同意：cpu-percent 80、cpu-count 1，范围仅限 cart 这一个 Pod。"
    ]
    assert b.drain_explanations() == []


def test_rejection_is_not_sent_as_a_whitelist_word() -> None:
    client = FakeClient()
    bridge(client, decide=lambda q: GateDecision(approved=False, reason="out_of_scope")
           ).answer(question("intent"))
    assert client.interrupts[0][2] != APPROVAL_WORD


def test_delivered_false_is_reported_not_treated_as_failure() -> None:
    """delivered=False means "nobody is waiting on this id", not "retry elsewhere".

    The corpus shows the first delivery is always True; False only appears on
    re-sends of a gate that was already answered.
    """
    client = FakeClient(delivered=False)
    answer = bridge(client).answer(question("intent"))
    assert answer.delivered is False
    assert answer.channel == "interrupt"
    # It must NOT silently fall back to the execution channel: that would send
    # an intent verdict to the execution gate.
    assert client.confirms == []


# ---- re-emission suppression (pitfall 4) --------------------------------


def test_same_gate_re_emitted_quickly_is_answered_once() -> None:
    client = FakeClient()
    now = [100.0]
    b = bridge(client, clock=lambda: now[0], suppression_seconds=30)
    b.answer(question("intent", "turn-1"))
    now[0] += 5
    second = b.answer(question("intent", "turn-1"))
    assert second.suppressed is True
    assert len(client.interrupts) == 1


def test_the_two_gates_of_one_turn_are_both_answered() -> None:
    """Dedupe keys on task *and* gate; keying on task alone swallowed a card."""
    client = FakeClient()
    b = bridge(client)
    b.answer(question("intent", "turn-1"))
    b.answer(question("execution", "turn-1"))
    assert [entry[1] for entry in client.interrupts] == ["turn-1", "turn-1"]
    assert client.confirms == []


def test_the_same_gate_after_the_window_is_answered_again() -> None:
    client = FakeClient()
    now = [100.0]
    b = bridge(client, clock=lambda: now[0], suppression_seconds=30)
    b.answer(question("intent", "turn-1"))
    now[0] += 31
    assert b.answer(question("intent", "turn-1")).suppressed is False
    assert len(client.interrupts) == 2


# ---- target-change ruling (口径 8) --------------------------------------


def test_entering_the_tool_container_to_reach_the_same_pod_is_approved() -> None:
    """D8-B: scope drifts pod -> chaosblade to act on the same approved Pod."""
    decision = classify_target_change({
        "type": "target_change",
        "original": {"scope": "pod", "namespace": "otel-demo",
                     "names": ["cart-7c58f6bb56-jzz9b"]},
        "proposed": {"scope": "chaosblade", "namespace": "default",
                     "names": ["b1f4bbf51e82d051"]},
    })
    assert decision.approved is True
    assert decision.reason == "carrier_scope_within_operating_surface"


def test_moving_to_another_business_namespace_is_refused() -> None:
    decision = classify_target_change({
        "original": {"scope": "pod", "namespace": "otel-demo", "names": ["cart-x"]},
        "proposed": {"scope": "pod", "namespace": "kube-system", "names": ["etcd-0"]},
    })
    assert decision.approved is False
    assert decision.reason == "namespace_escape"


def test_widening_to_more_pods_is_refused() -> None:
    decision = classify_target_change({
        "original": {"scope": "pod", "namespace": "otel-demo", "names": ["cart-x"]},
        "proposed": {"scope": "pod", "namespace": "otel-demo",
                     "names": ["cart-x", "cart-y", "checkout-z"]},
    })
    assert decision.approved is False
    assert decision.reason == "blast_radius_expanded"


def test_a_subset_of_the_approved_target_is_approved() -> None:
    decision = classify_target_change({
        "original": {"scope": "pod", "namespace": "otel-demo", "names": ["cart-x", "cart-y"]},
        "proposed": {"scope": "pod", "namespace": "otel-demo", "names": ["cart-x"]},
    })
    assert decision.approved is True


def test_undecidable_drift_is_refused_and_says_why() -> None:
    """Rejecting costs a re-run; approving corrupts the scope judgement."""
    decision = classify_target_change({
        "original": {"scope": "pod", "namespace": "otel-demo", "names": ["cart-x"]},
        "proposed": {"scope": "pod", "namespace": "otel-demo", "names": ["something-else"]},
    })
    assert decision.approved is False
    assert decision.reason == "target_change_undecidable"
    assert "拒绝可重跑" in decision.explanation


def test_unreadable_target_change_is_refused() -> None:
    assert classify_target_change({}).approved is False
    assert classify_target_change({"original": "x", "proposed": "y"}).approved is False


def test_target_change_gate_is_answered_on_the_interrupt_channel() -> None:
    client = FakeClient()
    b = BladeAIConfirmBridge(client, "sess-1")  # no decide function needed
    answer = b.answer(question(
        "target_change", "turn-17d0472480aa",
        original={"scope": "pod", "namespace": "otel-demo", "names": ["cart-x"]},
        proposed={"scope": "chaosblade", "namespace": "default", "names": ["uid"]},
    ))
    assert answer.approved is True
    assert client.interrupts == [("sess-1", "turn-17d0472480aa", APPROVAL_WORD)]
    assert client.confirms == []
    assert answer.channel == "interrupt"


# ---- plan translation for BladeAI 0.7.0 intents ---------------------------

ROUND_FOUR_INTENT = {
    "type": "intent_confirm",
    "fault_intent": {
        "action": "load", "fault_type": "pod-cpu-load", "namespace": "otel-demo-05",
        "names": ["cart-7ffd4d6f-lhw8j"], "duration_seconds": 600, "labels": {},
        "params": {
            "container": "cart", "cpu_percent": "80", "pod_uid": "da9afd5f-145b-4cb4-80be-1398f74378e6",
            "effect_metric": "target_cpu_cores", "effect_operator": "increase_by_at_least", "effect_threshold": "0.5",
            "recovery_metric": "target_cpu_cores", "recovery_operator": "within_baseline_delta",
            "recovery_threshold": "0.3",
        },
    },
}


def test_plan_from_intent_reads_the_skill_spelling_of_bladeai_0_7_0() -> None:
    plan = plan_from_intent(ROUND_FOUR_INTENT, target={"namespace": "otel-demo-05", "name": "", "uid": ""})
    assert plan["fault_type"] == "cpu-load"
    assert plan["intensity"] == {"cpu_percent": 80}
    assert plan["target"] == {
        "namespace": "otel-demo-05", "name": "cart-7ffd4d6f-lhw8j", "uid": "da9afd5f-145b-4cb4-80be-1398f74378e6",
    }
    assert plan["duration_seconds"] == 600


def test_plan_from_intent_emits_the_two_conditions_the_platform_never_supplies() -> None:
    """L0 leaves effect_condition and recovery_condition to the Agent.

    ``simulated_user._may_supply`` is an empty set at L0, so the simulated user
    may not fill either field in.  Round 6 on 2026-09-15 proposed plans without
    them in all five trials; the platform answered ``effect_condition:
    MISSING_PLAN_FIELD; recovery_condition: MISSING_PLAN_FIELD`` and BladeAI
    re-proposed until the trial budget ran out (r1 and r3 ended CASE_INVALID on
    HARNESS_TIMEOUT).  0.7.0 does carry the criteria -- flat, and as strings.
    """
    plan = plan_from_intent(
        ROUND_FOUR_INTENT,
        target={"namespace": "otel-demo-05", "name": "cart-7ffd4d6f-lhw8j", "uid": "uid-1"},
    )
    assert plan["effect_condition"] == {
        "metric": "target_cpu_cores",
        "operator": "increase_by_at_least",
        "threshold": 0.5,
    }
    assert plan["recovery_condition"] == {
        "metric": "target_cpu_cores",
        "operator": "within_baseline_delta",
        "threshold": 0.3,
    }
    # The criteria are not ChaosBlade flags; they must not reach the executor.
    assert "effect_metric" not in plan.get("native_params", {})
    assert "--effect-metric" not in plan.get("additional_native_constraints", {})


def test_plan_from_intent_omits_a_condition_it_cannot_complete() -> None:
    """A half-written condition is a blocking issue, a missing one is not.

    ``_has_blocking_issues`` treats MISSING_PLAN_FIELD as recoverable and
    everything else as fatal, so sending two of the three keys would be worse
    than sending none.
    """
    params = {
        key: value
        for key, value in ROUND_FOUR_INTENT["fault_intent"]["params"].items()
        if key != "recovery_operator"
    }
    intent = {
        "type": "intent_confirm",
        "fault_intent": {**ROUND_FOUR_INTENT["fault_intent"], "params": params},
    }
    plan = plan_from_intent(
        intent, target={"namespace": "otel-demo-05", "name": "cart-7ffd4d6f-lhw8j", "uid": "uid-1"}
    )
    assert plan["effect_condition"]["metric"] == "target_cpu_cores"
    assert "recovery_condition" not in plan


def test_plan_from_intent_prefers_the_runtime_identity_when_it_has_one() -> None:
    plan = plan_from_intent(ROUND_FOUR_INTENT, target={"namespace": "otel-demo-05", "name": "cart-bound", "uid": "uid-bound"})
    assert plan["target"] == {"namespace": "otel-demo-05", "name": "cart-bound", "uid": "uid-bound"}


def test_plan_from_intent_still_reads_the_chaosblade_triple() -> None:
    plan = plan_from_intent(
        {"fault_intent": {"scope": "pod", "target": "cpu", "action": "fullload", "params": {"cpu_percent": 70}}},
        target={"namespace": "otel-demo", "name": "cart-x", "uid": "u-1"},
    )
    assert plan["fault_type"] == "cpu-load"
    assert plan["intensity"] == {"cpu_percent": 70}


def test_only_a_rejected_cards_reason_is_queued_for_sending() -> None:
    """A card takes one word, so the reason for a rejection has to follow as text.

    Until 2026-09-17 nothing sent it.  An approval's explanation is not queued:
    sending it would cost BladeAI a whole turn to read "approved" again.
    """
    client = FakeClient()
    approving = bridge(client, decide=lambda q: GateDecision(
        approved=True, reason="in_scope", explanation="同意：范围仅限 cart。",
    ))
    approving.answer(question("intent"))
    assert approving.drain_rejection_explanations() == []

    client = FakeClient()
    rejecting = bridge(client, decide=lambda q: GateDecision(
        approved=False, reason="out_of_scope", explanation="不批准：目标超出 otel-demo-01。",
    ))
    rejecting.answer(question("intent"))
    assert rejecting.drain_rejection_explanations() == ["不批准：目标超出 otel-demo-01。"]
    assert rejecting.drain_rejection_explanations() == []
