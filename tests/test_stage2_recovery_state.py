"""O04: a failed recovery stops the run; it does not become a full reinstall.

On 2026-09-11 a permission-restore error was classified as "unknown or failed
rollback", which inferred ``T3_FULL_REINSTALL``. The platform uninstalled the
system under test and the reinstall then failed. The four paths below are the
ones that must never reach a reinstall on their own.
"""

from __future__ import annotations

from typing import Any

import pytest

from stage2_service.recovery_state import RecoveryState, classify_recovery
from stage2_service.reset import OtelDemoResetter
from stage2_service.reset_policy import ResetTier, classify_reset_policy


# Each carries the rollback-failure context that used to infer T3, so these are
# exactly the situations that reached ``helm uninstall`` before this change.
FAILED_ROLLBACK = {
    "rollback_attempted": True,
    "rolled_back": False,
    "main_fault_ever_active": True,
}
FOUR_PATHS = [
    pytest.param(
        {**FAILED_ROLLBACK, "recovery_exception": True},
        "RECOVERY_RAISED",
        id="recovery-raised",
    ),
    pytest.param(
        {**FAILED_ROLLBACK, "restoration_state_missing": True},
        "CONTROLLER_RESTARTED",
        id="controller-restart",
    ),
    pytest.param(
        {**FAILED_ROLLBACK, "cleanup_handle_missing": True},
        "CLEANUP_HANDLE_MISSING",
        id="cleanup-handle-missing",
    ),
    pytest.param(
        {**FAILED_ROLLBACK, "capability_loss_rollback_failed": True},
        "CAPABILITY_LOSS_ROLLBACK_FAILED",
        id="d7-d8-rollback-failed",
    ),
]


@pytest.mark.parametrize(("evidence", "code"), FOUR_PATHS)
def test_each_unverified_path_withholds_the_reinstall(evidence: dict[str, Any], code: str):
    assessment = classify_recovery(evidence)

    assert assessment.state is RecoveryState.UNVERIFIED
    assert code in assessment.reason_codes
    assert assessment.reinstall_authorized is False
    assert code in assessment.block_reason


@pytest.mark.parametrize(("evidence", "code"), FOUR_PATHS)
def test_the_reset_policy_carries_the_block_through(evidence: dict[str, Any], code: str):
    decision = classify_reset_policy(evidence)

    # The tier is the one that used to uninstall the system under test.
    assert decision.tier is ResetTier.T3_FULL_REINSTALL
    assert decision.recovery_state is RecoveryState.UNVERIFIED
    assert decision.reinstall_authorized is False
    assert decision.verified is False
    assert decision.allows_next_trial is False
    assert "RECOVERY_UNVERIFIED" in decision.reason_codes
    assert code in decision.reinstall_block_reason


def test_a_failed_rollback_still_names_the_tier_but_does_not_authorize_it():
    """The tier is still T3 — what changed is that the platform may not apply it."""
    decision = classify_reset_policy(
        {"rollback_attempted": True, "rolled_back": False, "main_fault_ever_active": True}
    )

    assert decision.tier is ResetTier.T3_FULL_REINSTALL
    assert "UNKNOWN_OR_FAILED_ROLLBACK" in decision.reason_codes
    assert decision.reinstall_authorized is False


def test_a_verified_recovery_authorizes_the_remedy_the_tier_asks_for():
    decision = classify_reset_policy(
        {
            "rollback_attempted": True,
            "rolled_back": True,
            "rollback_verified": True,
            "foreign_active_faults": True,
        }
    )

    assert decision.tier is ResetTier.T3_FULL_REINSTALL
    assert decision.recovery_state is RecoveryState.VERIFIED
    assert decision.reinstall_authorized is True


def test_an_operator_named_tier_is_still_honoured():
    """An explicit T3 is a decision that was already taken, not an inference."""
    decision = classify_reset_policy(
        {"reset_tier": "T3_FULL_REINSTALL", "recovery_exception": True}
    )

    assert decision.tier is ResetTier.T3_FULL_REINSTALL
    assert decision.reinstall_authorized is True


def test_clean_evidence_needs_no_recovery_state():
    decision = classify_reset_policy({"baseline_verified": True})

    assert decision.recovery_state is RecoveryState.NOT_REQUIRED
    assert decision.reinstall_authorized is True


# --- the reset itself -----------------------------------------------------


class _Runner:
    """Any helm/deploy call at all is a failure for these paths."""

    def __init__(self) -> None:
        self.argv: list[list[str]] = []

    def run(self, argv, *, env=None, timeout=None):
        self.argv.append(list(argv))
        raise AssertionError(f"the environment must not be touched: {argv}")


def _reset(tmp_path) -> tuple[OtelDemoResetter, _Runner]:
    runner = _Runner()
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")
    runtime_env = tmp_path / "otel-demo.env"
    runtime_env.write_text("NAMESPACE=otel-demo\n", encoding="utf-8")
    chart = tmp_path / "chart.tgz"
    chart.write_bytes(b"chart")
    reset = OtelDemoResetter(
        repo_root=tmp_path,
        kubeconfig=kubeconfig,
        runtime_env_file=runtime_env,
        chart_file=chart,
        environment_gate=type("Gate", (), {"qualify": staticmethod(lambda episode: {"qualified": True})})(),
        traffic_evidence=type(
            "Traffic",
            (),
            {
                "current": staticmethod(lambda: {"business_healthy": True}),
                "wait_until_healthy": staticmethod(lambda **_: {"business_healthy": True}),
                "reset_and_wait_healthy": staticmethod(lambda **_: {"business_healthy": True}),
            },
        )(),
        runner=runner,
    )
    return reset, runner


@pytest.mark.parametrize(("evidence", "code"), FOUR_PATHS)
def test_the_system_under_test_is_never_uninstalled_on_an_unverified_recovery(
    tmp_path, evidence: dict[str, Any], code: str
):
    reset, runner = _reset(tmp_path)

    result = reset.reset_with_policy(
        "campaign-1234567890abcdef-codex-t1", object(), evidence
    )

    assert runner.argv == []
    assert result["uninstalled"] is False
    assert result["reinstalled"] is False
    assert result["reinstall_withheld"] is True
    assert result["verified"] is False
    assert result["reset_policy"]["recovery_state"] == "RECOVERY_UNVERIFIED"
    assert code in result["reason"]
