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
}


@dataclass(frozen=True)
class EvaluationResult:
    primary_status: str
    gate_statuses: dict[str, str]
    policy_errors: list[str]
    missing_required_gates: list[str]


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
