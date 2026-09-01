"""Campaign-level evaluation summary derived from sealed Trial results."""

from __future__ import annotations

from collections import Counter, defaultdict

from .contracts import CampaignResult


def build_evaluation_summary(result: CampaignResult) -> dict:
    verdicts = Counter(item.agent_verdict.value for item in result.trials)
    platform = Counter("VALID" if item.platform_valid else "INVALID" for item in result.trials)
    by_harness: dict[str, Counter] = defaultdict(Counter)
    by_case: dict[str, Counter] = defaultdict(Counter)
    cleanup_fallbacks = 0
    active_recoveries = 0
    for item in result.trials:
        by_harness[item.harness.value][item.agent_verdict.value] += 1
        by_case[item.kind.value][item.agent_verdict.value] += 1
        cleanup_fallbacks += int(
            item.recovery.controller_cleanup_verified
            and not item.recovery.agent_recovery_verified
        )
        active_recoveries += int(item.recovery.agent_recovery_verified)
    total = len(result.trials)
    valid = platform["VALID"]
    return {
        "schema_version": "stage2-campaign-evaluation.v1",
        "campaign_id": result.campaign_id,
        "platform_status": result.platform_status.value,
        "qualification": result.qualification,
        "trial_count": total,
        "valid_trial_count": valid,
        "valid_trial_rate": valid / total if total else 0.0,
        "verdict_counts": dict(verdicts),
        "platform_counts": dict(platform),
        "by_harness": {key: dict(value) for key, value in by_harness.items()},
        "by_case": {key: dict(value) for key, value in by_case.items()},
        "agent_recovery_verified_count": active_recoveries,
        "controller_fallback_count": cleanup_fallbacks,
        "formally_scored": result.qualification.get("scored") is True,
        "error": result.error,
    }
