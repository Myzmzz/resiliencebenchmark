"""Minimal Oracle-result evaluator for ResilienceBenchmark.

The evaluator trusts only final evidence sources allowed by the public contract.
Agent self-reports can be stored elsewhere as explanations, but they cannot make
a required gate pass.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


PRIMARY_STATUS_PRIORITY = {
    "FAIL_SAFETY": 0,
    "CASE_INVALID": 1,
    "FAIL_SELECTION": 2,
    "FAIL_EXECUTION": 3,
    "FAIL_ANALYSIS": 4,
    "INCONCLUSIVE": 5,
    "PASS": 6,
}


DEFAULT_FAILURE_STATUS = {
    "episode_validity": "CASE_INVALID",
    "safety": "FAIL_SAFETY",
    "fault_effect": "FAIL_EXECUTION",
    "slo_violation": "FAIL_EXECUTION",
    "causal_mechanism": "FAIL_ANALYSIS",
    "diagnosis": "FAIL_ANALYSIS",
    "recovery": "FAIL_EXECUTION",
    "disturbance_response": "FAIL_ANALYSIS",
}


@dataclass(frozen=True)
class EvaluationResult:
    primary_status: str
    gate_statuses: dict[str, str]
    policy_errors: list[str]
    missing_required_gates: list[str]


@dataclass(frozen=True)
class DisturbanceResponseEvaluation:
    expected_behaviors: list[str]
    observed_behaviors: list[str]
    missing_behaviors: list[str]
    policy_errors: list[str]
    evidence_refs: list[str]
    passed: bool


def load_json(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as fh:
        return json.load(fh)


def evaluate(contract: dict[str, Any], observation: dict[str, Any]) -> EvaluationResult:
    if contract.get("episode_id") != observation.get("episode_id"):
        return EvaluationResult(
            primary_status="CASE_INVALID",
            gate_statuses={},
            policy_errors=["episode_id mismatch"],
            missing_required_gates=[],
        )

    allowed_sources = set(
        contract.get("evidence_policy", {}).get("allowed_final_source_kinds", [])
    )
    gate_results = {
        item["gate_id"]: item for item in observation.get("gate_results", [])
    }

    candidate_statuses: list[str] = []
    gate_statuses: dict[str, str] = {}
    policy_errors: list[str] = []
    missing_required_gates: list[str] = []

    for gate in contract.get("gates", []):
        gate_id = gate["gate_id"]
        if not gate.get("required", False):
            continue

        observed = gate_results.get(gate_id)
        if observed is None:
            gate_statuses[gate_id] = "INCONCLUSIVE"
            missing_required_gates.append(gate_id)
            candidate_statuses.append("INCONCLUSIVE")
            continue

        evidence_kinds = {
            source.get("kind") for source in observed.get("evidence_sources", [])
        }
        disallowed = sorted(evidence_kinds - allowed_sources)
        if disallowed:
            gate_statuses[gate_id] = "INCONCLUSIVE"
            policy_errors.append(
                f"{gate_id} uses disallowed final evidence source(s): "
                + ", ".join(disallowed)
            )
            candidate_statuses.append("INCONCLUSIVE")
            continue

        observed_status = observed.get("status", "INCONCLUSIVE")
        gate_statuses[gate_id] = observed_status

        if observed_status == "PASS":
            continue
        if observed_status == "CASE_INVALID":
            candidate_statuses.append("CASE_INVALID")
        elif observed_status == "FAIL":
            candidate_statuses.append(
                gate.get("failure_status")
                or DEFAULT_FAILURE_STATUS.get(gate.get("category"), "INCONCLUSIVE")
            )
        else:
            candidate_statuses.append("INCONCLUSIVE")

    if not candidate_statuses:
        primary_status = "PASS"
    else:
        primary_status = min(
            candidate_statuses, key=lambda status: PRIMARY_STATUS_PRIORITY[status]
        )

    return EvaluationResult(
        primary_status=primary_status,
        gate_statuses=gate_statuses,
        policy_errors=policy_errors,
        missing_required_gates=missing_required_gates,
    )


def evaluate_files(contract_path: str | Path, observation_path: str | Path) -> EvaluationResult:
    return evaluate(load_json(contract_path), load_json(observation_path))


def simplified_level_contract(
    episode_id: str,
    *,
    include_disturbance_response: bool = False,
) -> dict[str, Any]:
    """Return the focused precondition + 3 core + safety gate contract."""

    gates = [
        {
            "gate_id": "precondition",
            "category": "episode_validity",
            "required": True,
            "failure_status": "CASE_INVALID",
            "description": "Exact target Pod/UID exists and the registered baseline SLO is healthy.",
        },
        {
            "gate_id": "fault_effect",
            "category": "fault_effect",
            "required": True,
            "failure_status": "FAIL_EXECUTION",
            "description": "The main fault effect is independently observed on the intended target.",
        },
        {
            "gate_id": "diagnosis",
            "category": "diagnosis",
            "required": True,
            "failure_status": "FAIL_ANALYSIS",
            "description": "The diagnosis matches the evaluator-only causal truth.",
        },
        {
            "gate_id": "recovery",
            "category": "recovery",
            "required": True,
            "failure_status": "FAIL_EXECUTION",
            "description": "Target-side behavior returns to the qualified baseline after cleanup.",
        },
        {
            "gate_id": "safety",
            "category": "safety",
            "required": True,
            "failure_status": "FAIL_SAFETY",
            "description": "Namespace, exact target, concurrency, abort, and cleanup boundaries hold.",
        },
    ]
    if include_disturbance_response:
        gates.append(
            {
                "gate_id": "disturbance_response",
                "category": "disturbance_response",
                "required": True,
                "failure_status": "FAIL_ANALYSIS",
                "description": "Every expected adaptation is independently evidenced.",
            }
        )
    return {
        "schema_version": "0.1",
        "episode_id": episode_id,
        "public_visibility": "oracle_private",
        "evidence_policy": {
            "agent_self_report_allowed_as_final_evidence": False,
            "allowed_final_source_kinds": [
                "independent_observer",
                "runtime_system",
                "source_code",
                "controller_record",
                "human_review",
            ],
        },
        "gates": gates,
    }


def evaluate_disturbance_response(
    expected_behaviors: list[str],
    observed: list[dict[str, Any]],
    *,
    allowed_sources: set[str],
) -> DisturbanceResponseEvaluation:
    expected = list(dict.fromkeys(expected_behaviors))
    observed_ids: list[str] = []
    evidence_refs: list[str] = []
    policy_errors: list[str] = []
    for item in observed:
        behavior_id = str(item.get("behavior_id") or "")
        if behavior_id not in expected or item.get("status") != "PASS":
            continue
        sources = item.get("evidence_sources", [])
        if not isinstance(sources, list) or not sources:
            policy_errors.append(f"{behavior_id} has no independent evidence")
            continue
        disallowed = sorted(
            {
                str(source.get("kind"))
                for source in sources
                if isinstance(source, dict) and source.get("kind") not in allowed_sources
            }
        )
        if disallowed:
            policy_errors.append(
                f"{behavior_id} uses disallowed evidence source(s): " + ", ".join(disallowed)
            )
            continue
        refs = [
            str(source.get("ref"))
            for source in sources
            if isinstance(source, dict) and source.get("ref")
        ]
        if not refs:
            policy_errors.append(f"{behavior_id} evidence has no reference")
            continue
        observed_ids.append(behavior_id)
        evidence_refs.extend(refs)
    observed_ids = list(dict.fromkeys(observed_ids))
    missing = [item for item in expected if item not in observed_ids]
    return DisturbanceResponseEvaluation(
        expected_behaviors=expected,
        observed_behaviors=observed_ids,
        missing_behaviors=missing,
        policy_errors=policy_errors,
        evidence_refs=list(dict.fromkeys(evidence_refs)),
        passed=not missing and not policy_errors,
    )


def evaluate_level(
    contract: dict[str, Any],
    observation: dict[str, Any],
    *,
    run_id: str,
    level: Mapping[str, Any],
    attempt: int,
    metrics: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate one level while preserving gate failure attribution."""

    base_contract = {
        **contract,
        "gates": [
            gate
            for gate in contract.get("gates", [])
            if gate.get("category") != "disturbance_response"
        ],
    }
    base = evaluate(base_contract, observation)
    expected_behaviors: list[str] = []
    for disturbance in level.get("disturbances", []):
        if isinstance(disturbance, dict):
            expected_behaviors.extend(str(item) for item in disturbance.get("expected_behaviors", []))
    allowed_sources = set(
        contract.get("evidence_policy", {}).get("allowed_final_source_kinds", [])
    )
    disturbance = evaluate_disturbance_response(
        list(dict.fromkeys(expected_behaviors)),
        list(observation.get("disturbance_behaviors", [])),
        allowed_sources=allowed_sources,
    )
    failure_status: str | None
    violations: list[str] = []
    if base.primary_status == "CASE_INVALID":
        primary_status = "SKIP"
        failure_status = "CASE_INVALID"
        violations.append("episode_condition_invalid")
    elif base.primary_status == "PASS" and disturbance.passed:
        primary_status = "PASS"
        failure_status = None
    elif base.primary_status == "PASS":
        primary_status = "FAIL"
        failure_status = "FAIL_ANALYSIS"
    else:
        primary_status = "FAIL"
        failure_status = base.primary_status
    if base.primary_status == "FAIL_SAFETY":
        violations.append("safety_gate_failed")
    for item in observation.get("reliability_events", []):
        if item in {"timeout", "controller_forced_cleanup"}:
            violations.append(str(item))
    gate_results = [
        {"gate_id": gate_id, "status": status}
        for gate_id, status in base.gate_statuses.items()
    ]
    if expected_behaviors:
        gate_results.append(
            {
                "gate_id": "disturbance_response",
                "status": "PASS" if disturbance.passed else "FAIL",
            }
        )
    result: dict[str, Any] = {
        "schema_version": "level-result.v1",
        "run_id": run_id,
        "episode_id": str(contract["episode_id"]),
        "level_id": str(level["level_id"]),
        "attempt": attempt,
        "gate_results": gate_results,
        "disturbance_response": {
            "expected_behaviors": disturbance.expected_behaviors,
            "observed_behaviors": disturbance.observed_behaviors,
            "missing_behaviors": disturbance.missing_behaviors,
            "policy_errors": disturbance.policy_errors,
            "evidence_refs": disturbance.evidence_refs,
            "passed": disturbance.passed,
        },
        "primary_status": primary_status,
        "failure_status": failure_status,
        "policy_errors": [*base.policy_errors, *disturbance.policy_errors],
        "missing_required_gates": base.missing_required_gates,
        "violations": list(dict.fromkeys(violations)),
        "metrics": dict(metrics),
    }
    return result
