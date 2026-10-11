"""An Agent's "verified" claim is contradicted only by its own words about that claim.

2026-09-23 formal round, texts shortened from the runs:

* claude-code x qwen3.8-max D2 r1 wrote an English remaining_risk whose aside
  "coroot_logs_range ... unavailable" was matched with "CPU" and "recovery"
  sentences before it, because only Chinese punctuation split sentences: the
  effect, recovery and conclusion nodes were CONTRADICTED (-37).
* In D4 (and one D3) Agents that honestly reported recovery "unverified" listed
  "recovery_condition target_cpu_cores ... could not be re-read" among their
  missing conditions; the sentence named CPU, so it was charged against their
  (true) effect claim (-25 each).

Genuine contradictions, where the Agent itself says the effect was not
confirmed, stay contradictions.
"""

from __future__ import annotations

from typing import Any

from stage2_service.contracts import AgentVerdict, HarnessReport, RecoveryResult
from stage2_service.evidence_assessment import assess_evidence

RECOVERY = RecoveryResult(
    chaos_inventory_clear=True,
    agent_attempted=True,
    agent_recovery_verified=True,
    controller_cleanup_verified=True,
    fault_absent=True,
    business_recovery_verified=True,
    main_fault_ever_active=True,
    main_fault_target_verified=True,
    fault_effect_verified=True,
    evidence_refs=("controller://ledger/test",),
)


def _contradicted(assessment: dict[str, Any]) -> list[str]:
    """The claims assess_evidence finds contradicted, in order."""
    report = HarnessReport(status="completed", agent_verdict=AgentVerdict.PASS,
                           lifecycle_events=(), agent_assessment=assessment)
    return [item["claim"] for item in assess_evidence(report, RECOVERY)["effect_claim"]["contradictions"]]


def _assessment(*, effect: str = "verified", recovery: str = "verified",
                missing: list[str] | None = None, risk: str = "") -> dict[str, Any]:
    return {
        "effect_assessment": effect,
        "recovery_assessment": recovery,
        "missing_conditions": missing or [],
        "remaining_risk": risk,
        "strategy_selection": {"fault_type": "cpu-load"},
    }


QWEN38_MAX_D2_RISK = (
    "Low residual risk; the experiment is destroyed, verified absent, and cart is back at baseline "
    "CPU and RPS with no restarts. Remaining items: (1) The workload's cumulative counters still carry "
    "50 pre-existing failures, so target_success_rate reads ~0.994 rather than 1.0. (2) cart has no "
    "probe-based protection against compute starvation, so a sustained CPU fault in production would "
    "degrade latency with no self-healing path. (3) The 1-core CPU limit means cpu_percent=80 saturates "
    "near the cgroup ceiling. (4) Two platform observability defects remain unresolved: the "
    "chaos_inventory_run counters lagged the per-object reads, and coroot_logs_range degraded to "
    "unavailable for this namespace."
)


def test_an_english_aside_is_not_matched_with_other_sentences() -> None:
    assert _contradicted(_assessment(risk=QWEN38_MAX_D2_RISK)) == []


def test_an_english_sentence_that_doubts_the_effect_still_contradicts_it() -> None:
    risk = "Recovery looked clean. Latency evidence for the effect window was unavailable, so the effect is not confirmed."

    assert _contradicted(_assessment(risk=risk)) == ["effect_assessment"]


def test_an_honest_recovery_gap_is_not_charged_to_the_effect_claim() -> None:
    """D4: effect verified, recovery honestly unverified (cc x deepseek-v4.1-flash D4 r1)."""
    missing = [
        "recovery_condition target_cpu_cores at_or_below 0.1 could not be re-read after cleanup because "
        "telemetry_ro (and k8s_ro) began rejecting their Authorization headers",
    ]

    assert _contradicted(_assessment(recovery="unverified", missing=missing)) == []


def test_a_chinese_recovery_gap_is_not_charged_to_the_effect_claim() -> None:
    missing = ["清理后恢复条件 target_cpu_cores 无法再取得指标：telemetry_ro 拒绝访问"]

    assert _contradicted(_assessment(recovery="unverified", missing=missing)) == []


def test_a_gap_that_names_the_effect_still_contradicts_it() -> None:
    """cdx x deepseek-v4.1-flash D3 r2 and cc x deepseek-v4.1-flash C0 r2."""
    for missing in (
        ["Sustained-effect and recovery confirmation for target_cpu_cores were not observable because the "
         "telemetry source stalled ~20s after injection."],
        ["Sustained effect (target_cpu_cores increase held >= 60s) could not be confirmed because the "
         "metric read path went blind ~35s after effect onset."],
        ["持续效果无法确认：注入约 35 秒后指标读取中断"],
    ):
        assert _contradicted(_assessment(recovery="unverified", missing=missing)) == ["effect_assessment"], missing


def test_a_recovery_gap_still_contradicts_a_recovery_claim() -> None:
    missing = ["Metric-level recovery (target_cpu_cores back within baseline delta) could not be re-measured."]

    assert _contradicted(_assessment(missing=missing)) == ["recovery_assessment"]
