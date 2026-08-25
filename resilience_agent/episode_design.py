"""Design complete resilience Episodes from static defect candidates.

This module is an internal capability of our own analysis Agent.  It does not
produce instructions for BladeAI, Claude Code, Codex, or another Agent host.
The experiment sequence is one part of the Episode contract, alongside the
snapshot, workload, evidence, action budget, Oracle, recovery, and readiness
gates.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .common import load_document, stable_id, unique_strings


def _parameter_values(names: list[str], context: dict[str, Any]) -> list[dict[str, Any]]:
    provided = context.get("experiment_parameters", {})
    return [
        {
            "name": name,
            "value": provided.get(name),
            "status": "provided" if provided.get(name) is not None else "tbd",
        }
        for name in names
    ]


def _runtime_target(candidate: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    supplied = context.get("runtime_target", {})
    resolved = all(supplied.get(field) for field in ("kind", "name", "uid"))
    return {
        "component": candidate["target"]["component"],
        "kind": supplied.get("kind"),
        "name": supplied.get("name"),
        "uid": supplied.get("uid"),
        "status": "resolved" if resolved else "unresolved",
    }


def _validation_phases(candidate: dict[str, Any], template: dict[str, Any]) -> list[dict[str, Any]]:
    hints = candidate["planning_hints"]
    evidence = hints["observable_evidence"]
    dynamic = {
        "qualification": [
            "fixed application snapshot",
            "immutable runtime target identity",
            "healthy workload baseline",
            "qualified independent observers and cleanup path",
        ],
        "fault_effect": unique_strings([*evidence["kubernetes"], *evidence["metrics"]]),
        "business_impact": [hints["slo_impact"]],
        "causal_mechanism": unique_strings(
            [*evidence["source"], *evidence["traces"], hints["expected_degradation"]]
        ),
        "recovery": hints["recovery_verification"],
        "final_decision": ["per-gate result with independent evidence provenance"],
    }
    phases = []
    for phase in template["validation_phases"]:
        item = dict(phase)
        item["required_evidence"] = dynamic.get(
            phase["phase_id"], phase.get("required_evidence", [])
        )
        phases.append(item)
    return phases


def _readiness(
    context: dict[str, Any],
    runtime_target: dict[str, Any],
    parameters: list[dict[str, Any]],
) -> dict[str, Any]:
    workload = context.get("workload", {})
    blockers: list[str] = []
    if not context.get("application"):
        blockers.append("Application mapping is missing.")
    if not context.get("namespace"):
        blockers.append("Namespace or equivalent runtime boundary is missing.")
    if not context.get("snapshot_id"):
        blockers.append("A fixed, reproducible environment snapshot has not been assigned.")
    if runtime_target["status"] != "resolved":
        blockers.append("The static component has not been bound to an immutable runtime target.")
    if not context.get("selected_actuator"):
        blockers.append("No concrete bounded actuator has been selected.")
    for parameter in parameters:
        if parameter["status"] == "tbd":
            blockers.append(f"Experiment parameter '{parameter['name']}' is still TBD.")
    if not workload.get("profile"):
        blockers.append("The repeatable workload profile is missing.")
    if not workload.get("baseline_window"):
        blockers.append("The baseline observation window is missing.")
    if not workload.get("slo"):
        blockers.append("Calibrated business SLO or correctness thresholds are missing.")
    if context.get("independent_observers_qualified") is not True:
        blockers.append("Independent evidence channels have not been qualified.")
    if not context.get("cleanup_handle"):
        blockers.append("Controller-owned cleanup handle has not been issued.")

    promotion_requirements = [
        "Run no-fault and positive/negative controls to calibrate triggerability and false positives.",
        "Validate that the workload exercises the candidate call path without revealing the answer.",
        "Validate every independent Oracle gate against retained evidence artifacts.",
        "Prove cleanup and business recovery are repeatable from the fixed snapshot.",
        "Author evaluator-only causal truth only after the mechanism is independently confirmed.",
    ]
    return {
        "ready_for_execution": not blockers,
        "ready_for_lock": False,
        "execution_blockers": blockers,
        "promotion_requirements": promotion_requirements,
    }


def _make_episode(
    candidate: dict[str, Any], templates: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    hints = candidate["planning_hints"]
    family = candidate["family"]
    defaults = templates["defaults"]
    strategy = templates.get("family_strategies", {}).get(family, defaults["strategy"])
    runtime_target = _runtime_target(candidate, context)
    parameters = _parameter_values(hints["parameters"], context)
    readiness = _readiness(context, runtime_target, parameters)
    status = "draft_ready_for_controller_review" if readiness["ready_for_execution"] else "draft_unqualified"
    episode_id = stable_id(
        f"EPI-{candidate['defect_ref']}",
        [candidate["candidate_id"], templates["template_version"]],
        length=10,
    ).upper()
    workload = context.get("workload", {})
    budget = context.get("budget", {})
    snapshot_id = context.get("snapshot_id")
    selected_actuator = context.get("selected_actuator")
    baseline_experiment_id = f"{episode_id}-BASELINE"
    validation_experiment_id = f"{episode_id}-VALIDATE"

    return {
        "episode_id": episode_id,
        "candidate_id": candidate["candidate_id"],
        "defect_ref": candidate["defect_ref"],
        "title": f"Episode for {candidate['title']} in {candidate['target']['component']}",
        "status": status,
        "design_basis": {
            "hypothesis": (
                f"If a bounded {hints['trigger_class']} condition affects "
                f"{candidate['target']['component']}, the predicted mechanism will cause: "
                f"{hints['expected_degradation']}"
            ),
            "mechanism": candidate["reasoning"]["mechanism"],
            "candidate_confidence": candidate["confidence_score"],
            "evidence_basis": [
                {
                    "kind": item["kind"],
                    "path": item["path"],
                    "line": item["line_start"],
                    "signal_id": item["signal_id"],
                    "summary": item["summary"],
                }
                for item in candidate["evidence"]
            ],
            "alternative_explanations": candidate["reasoning"]["alternative_explanations"],
            "truth_status": "hypothesis_not_independently_confirmed",
        },
        "application_snapshot": {
            "application": str(context.get("application", "unknown")),
            "namespace": context.get("namespace"),
            "snapshot_id": snapshot_id,
            "release_ref": context.get("release_ref"),
            "candidate_services": [candidate["target"]["component"]],
            "source_artifacts": candidate["target"]["artifacts"],
            "runtime_target": runtime_target,
            "health_prerequisites": context.get(
                "health_prerequisites",
                [
                    "The application is healthy before the Episode starts.",
                    "The baseline workload satisfies the registered SLO window.",
                ],
            ),
            "reset_contract": unique_strings(
                [*hints["cleanup"], *hints["recovery_verification"]]
            ),
        },
        "workload": {
            "profile": workload.get("profile"),
            "baseline_window": workload.get("baseline_window"),
            "slo": workload.get("slo", [hints["slo_impact"]]),
            "fixture_ref": workload.get("fixture_ref"),
            "status": (
                "provided"
                if workload.get("profile") and workload.get("baseline_window") and workload.get("slo")
                else "partial"
            ),
        },
        "evidence_contract": {
            "metrics": hints["observable_evidence"]["metrics"],
            "traces": hints["observable_evidence"]["traces"],
            "logs": hints["observable_evidence"]["logs"],
            "kubernetes": hints["observable_evidence"]["kubernetes"],
            "source": hints["observable_evidence"]["source"],
            "independence_requirement": (
                "Final Episode validity, effect, impact, mechanism, and recovery results must come "
                "from Controller/Evaluator evidence rather than this design module's self-report."
            ),
        },
        "action_space": {
            "allowed_trigger_classes": [hints["trigger_class"]],
            "actuator_candidates": hints["actuator_candidates"],
            "selected_actuator": selected_actuator,
            "parameters": parameters,
            "target_scope": "single_resolved_target",
            "forbidden_actions": defaults["forbidden_actions"],
        },
        "budget": {
            "max_experiments": int(budget.get("max_experiments", 2)),
            "max_duration_minutes": budget.get("max_duration_minutes"),
            "max_concurrent_faults": 1,
        },
        "experiment_sequence": [
            {
                "experiment_id": baseline_experiment_id,
                "role": "baseline_control",
                "objective": "Establish a healthy, attributable no-fault reference for the same workload and evidence channels.",
                "trigger": None,
                "phases": ["qualification", "baseline"],
                "required_evidence": unique_strings(
                    [
                        *hints["observable_evidence"]["metrics"],
                        *hints["observable_evidence"]["traces"],
                        *hints["observable_evidence"]["logs"],
                    ]
                ),
                "completion_criteria": "Baseline is healthy, repeatable, and tied to the fixed snapshot.",
            },
            {
                "experiment_id": validation_experiment_id,
                "role": "hypothesis_validation",
                "objective": strategy,
                "trigger": {
                    "class": hints["trigger_class"],
                    "selected_actuator": selected_actuator,
                    "parameters": parameters,
                },
                "phases": _validation_phases(candidate, defaults),
                "required_evidence": unique_strings(
                    [
                        hints["slo_impact"],
                        hints["expected_degradation"],
                        *hints["recovery_verification"],
                    ]
                ),
                "completion_criteria": "Every required Oracle gate has an explicit result and evidence provenance.",
            },
        ],
        "safety": {
            "mode": "design_only_not_executed",
            "guardrails": unique_strings([*defaults["guardrails"], *hints["guardrails"]]),
            "abort_conditions": defaults["abort_conditions"],
            "required_authorization": [
                "Explicit approval of the concrete runtime target and actuator.",
                "Controller safety validation and cleanup handle before execution.",
            ],
        },
        "oracle": {
            "primary_outcome": "verified_resilience_defect_or_rejected_hypothesis",
            "gates": defaults["oracle_gates"],
        },
        "recovery": {
            "cleanup_actions": hints["cleanup"],
            "verification": hints["recovery_verification"],
            "control_plane_absence_required": True,
            "business_recovery_required": True,
        },
        "decision_rules": {
            "confirm": [
                "The trigger effect is independently observed on the intended target.",
                "The registered business SLO or correctness condition is violated.",
                "Runtime and static evidence support the predicted causal mechanism.",
                "Recovery is independently verified after cleanup.",
            ],
            "reject": [
                "An effective safeguard invalidates the static hypothesis on the exercised path.",
                "The predicted mechanism does not occur under a valid and effective trigger.",
            ],
            "inconclusive": unique_strings(
                [
                    "The workload does not exercise the candidate path.",
                    "The trigger effect, business impact, mechanism, or recovery cannot be independently observed.",
                    *candidate["validation_requirements"],
                ]
            ),
        },
        "leakage_controls": {
            "internal_only_fields": [
                "defect_ref",
                "design_basis.hypothesis",
                "design_basis.evidence_basis",
                "design_basis.alternative_explanations",
            ],
            "public_materialization_rules": defaults["public_materialization_rules"],
        },
        "model_reasoning": None,
        "readiness": readiness,
    }


def design_episodes(
    candidate_document: dict[str, Any],
    templates_path: Path,
    *,
    min_confidence: float = 0.65,
) -> dict[str, Any]:
    """Create internal Episode designs from schema-valid candidate defects."""
    templates = load_document(templates_path)
    context = candidate_document["project"].get("context", {})
    eligible = [
        item
        for item in candidate_document["candidates"]
        if item["confidence_score"] >= min_confidence
    ]
    episodes = [_make_episode(candidate, templates, context) for candidate in eligible]
    return {
        "schema_version": "episode-designs.v0.1",
        "design_set_id": stable_id(
            "EPSET", [candidate_document["analysis_id"], *(item["episode_id"] for item in episodes)]
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_analysis_id": candidate_document["analysis_id"],
        "generation_mode": "internal_episode_design",
        "model_provenance": None,
        "summary": {
            "candidate_count": len(candidate_document["candidates"]),
            "eligible_candidate_count": len(eligible),
            "episode_count": len(episodes),
            "min_confidence": min_confidence,
        },
        "episodes": episodes,
    }
