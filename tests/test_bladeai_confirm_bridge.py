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


def test_execution_gate_answers_on_confirm_keyed_by_task_id() -> None:
    client = FakeClient()
    answer = bridge(client).answer(question("execution", "turn-e708412252a4"))
    assert client.confirms == [("turn-e708412252a4", "approve", "in_scope")]
    assert client.interrupts == []
    assert answer.channel == "confirm"


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
    assert len(client.interrupts) == 1
    assert len(client.confirms) == 1


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


def test_target_change_gate_is_answered_on_the_confirm_channel() -> None:
    client = FakeClient()
    b = BladeAIConfirmBridge(client, "sess-1")  # no decide function needed
    answer = b.answer(question(
        "target_change", "turn-17d0472480aa",
        original={"scope": "pod", "namespace": "otel-demo", "names": ["cart-x"]},
        proposed={"scope": "chaosblade", "namespace": "default", "names": ["uid"]},
    ))
    assert answer.approved is True
    assert client.confirms == [("turn-17d0472480aa", "approve",
                                "carrier_scope_within_operating_surface")]
