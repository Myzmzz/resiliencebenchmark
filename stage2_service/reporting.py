"""Campaign-level evaluation summary derived from sealed Trial results."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping
from typing import Any

from .contracts import CampaignResult


def build_evaluation_summary(result: CampaignResult) -> dict:
    verdicts = Counter(item.agent_verdict.value for item in result.trials)
    platform = Counter("VALID" if item.platform_valid else "INVALID" for item in result.trials)
    by_harness: dict[str, Counter] = defaultdict(Counter)
    by_case: dict[str, Counter] = defaultdict(Counter)
    cleanup_fallbacks = 0
    active_recoveries = 0
    by_reset_tier: Counter = Counter()
    assisted_trials = 0
    semantic_nudge_violations = 0
    blocked_next_trials = 0
    autonomy_eligible = 0
    agent_outcomes: Counter = Counter()
    for item in result.trials:
        by_harness[item.harness.value][item.agent_verdict.value] += 1
        by_case[item.kind.value][item.agent_verdict.value] += 1
        autonomy_eligible += int(item.autonomy_eligible)
        agent_outcomes[item.agent_outcome.value] += 1
        cleanup_fallbacks += int(
            item.recovery.controller_cleanup_verified
            and not item.recovery.agent_recovery_verified
        )
        active_recoveries += int(item.recovery.agent_recovery_verified)
        effect = item.recovery.fault_effect_evidence
        reset_policy = _mapping(effect.get("reset_policy"))
        assistance = _mapping(effect.get("assistance"))
        if reset_policy:
            tier = str(reset_policy.get("tier") or "UNKNOWN")
            by_reset_tier[tier] += 1
            blocked_next_trials += int(reset_policy.get("allows_next_trial") is not True)
        assisted_trials += int(assistance.get("assisted") is True)
        semantic_nudge_violations += int(
            assistance.get("semantic_nudge_violation") is True
        )
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
        "reset_tier_counts": dict(by_reset_tier),
        "reset_next_trial_blocked_count": blocked_next_trials,
        "assisted_trial_count": assisted_trials,
        "semantic_nudge_violation_count": semantic_nudge_violations,
        "autonomy_eligible_trial_count": autonomy_eligible,
        "agent_outcome_counts": dict(agent_outcomes),
        "formally_scored": result.qualification.get("scored") is True,
        "error": result.error,
    }


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}
