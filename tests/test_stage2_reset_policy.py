from __future__ import annotations

from stage2_service.reset_policy import ResetTier, classify_reset_policy


def test_t0_no_write_allows_next_trial_after_baseline_verification():
    decision = classify_reset_policy(
        {
            "baseline_verified": True,
            "business_healthy": True,
        }
    )

    assert decision.tier is ResetTier.T0_NO_WRITE
    assert decision.verified is True
    assert decision.allows_next_trial is True
    assert decision.to_dict()["required_actions"] == ["verify_baseline"]


def test_t1_capability_change_blocks_until_restored_and_verified():
    decision = classify_reset_policy(
        {
            "permission_revoked": True,
            "baseline_verified": True,
        }
    )

    assert decision.tier is ResetTier.T1_CAPABILITY
    assert decision.verified is False
    assert decision.allows_next_trial is False


def test_t2_fault_or_target_change_requires_cleanup_and_business_recovery():
    decision = classify_reset_policy(
        {
            "main_fault_ever_active": True,
            "fault_absent": True,
            "business_recovery_verified": True,
        }
    )

    assert decision.tier is ResetTier.T2_FAULT_OR_TARGET
    assert decision.verified is True
    assert decision.allows_next_trial is True


def test_unreconciled_unknown_outcome_forces_full_reinstall_tier():
    decision = classify_reset_policy(
        {
            "operation_outcome_uncertain": True,
            "fault_absent": True,
            "business_recovery_verified": True,
        }
    )

    assert decision.tier is ResetTier.T3_FULL_REINSTALL
    assert decision.verified is False
    assert decision.allows_next_trial is False


def test_failed_rollback_forces_full_reinstall_tier():
    decision = classify_reset_policy(
        {
            "rollback_attempted": True,
            "rolled_back": False,
            "main_fault_ever_active": True,
        }
    )

    assert decision.tier is ResetTier.T3_FULL_REINSTALL
    assert "UNKNOWN_OR_FAILED_ROLLBACK" in decision.reason_codes
