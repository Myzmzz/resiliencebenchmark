"""Campaign-level evaluation summary derived from sealed Trial results."""

from __future__ import annotations

from collections import Counter, defaultdict
import json
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
    node_scores: list[float] = []
    experiment_gates: Counter = Counter()
    agent_outcomes: Counter = Counter()
    for item in result.trials:
        by_harness[item.harness.value][item.agent_verdict.value] += 1
        by_case[item.kind.value][item.agent_verdict.value] += 1
        node_scores.append(float(item.score_summary.get("percentage") or 0))
        experiment_gates[str(item.experiment_gate.get("status") or "NOT_EVALUATED")] += 1
        agent_outcomes[item.agent_outcome.value] += 1
        cleanup_fallbacks += int(
            item.recovery.main_fault_ever_active
            and item.recovery.recovery_attribution.get("cleanup_executor") == "CONTROLLER_FALLBACK"
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
        "experiment_gate_counts": dict(experiment_gates),
        "experiment_completed_count": sum(item.experiment_completed is True for item in result.trials),
        "experiment_incomplete_count": sum(item.experiment_completed is False for item in result.trials),
        "average_node_score": (
            round(sum(node_scores) / len(node_scores), 2) if node_scores else None
        ),
        "agent_outcome_counts": dict(agent_outcomes),
        "formally_scored": result.qualification.get("scored") is True,
        "error": result.error,
    }


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def build_trial_report(request, trial_id, report, recovery, decision) -> str:
    prompt = request.case_bundle.base_prompt if request.case_bundle is not None else "未提供"
    summary = {
        "experiment_completed": decision.get("experiment_completed"),
        "agent_verdict": decision.get("agent_verdict"),
        "effect_observation": decision.get("effect_observation"),
        "effect_claim": decision.get("effect_claim"),
        "output_repaired": report.final_output.get("output_repaired", False),
        "output_repair_count": report.final_output.get("output_repair_count", 0),
        "retry_history": report.final_output.get("retry_history", []),
        "recovery_attribution": recovery.recovery_attribution,
        "node_results": decision.get("node_results"),
        "interaction_ledger": decision.get("interaction_ledger"),
    }
    return (
        f"# {trial_id}\n\n等级标签：{request.prompt_level_label}\n\n"
        f"decision_policy：{request.decision_policy.value}\n\nprompt_mode：{request.prompt_mode.value}\n\n"
        f"## Prompt 原文\n\n{prompt}\n\n## 完成、行为及交互记录\n\n```json\n"
        + json.dumps(summary, ensure_ascii=False, indent=2) + "\n```\n"
    )
