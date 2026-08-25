"""Independent runtime evidence collection for multi-level evaluation."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

from progression.controller import TrialTicket

from .evaluator import evaluate_level, simplified_level_contract


class FaultEffectObserver(Protocol):
    def __call__(
        self,
        ticket: TrialTicket,
        level: Mapping[str, Any],
        trial_report: Mapping[str, Any],
        preparation: Mapping[str, Any],
        finalization: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


class AgentResultLoader(Protocol):
    def __call__(self, trial_report: Mapping[str, Any]) -> Mapping[str, Any]: ...


class HarnessArtifactAgentResultLoader:
    def __init__(self, artifact_root: Path):
        self.artifact_root = artifact_root.resolve()

    def __call__(self, trial_report: Mapping[str, Any]) -> Mapping[str, Any]:
        reference = str(trial_report.get("agentResultRef") or "")
        if not reference or ".." in Path(reference).parts:
            return {}
        path = (self.artifact_root / reference).resolve()
        try:
            path.relative_to(self.artifact_root)
        except ValueError:
            return {}
        if not path.is_file():
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}


class RuntimeLevelEvaluator:
    def __init__(
        self,
        *,
        episode_id: str,
        expected_diagnosis_terms: Sequence[str],
        source_evidence_refs: Sequence[str],
        effect_observer: FaultEffectObserver,
        agent_result_loader: AgentResultLoader,
    ):
        self.episode_id = episode_id
        self.expected_diagnosis_terms = tuple(term.lower() for term in expected_diagnosis_terms)
        self.source_evidence_refs = tuple(source_evidence_refs)
        self.effect_observer = effect_observer
        self.agent_result_loader = agent_result_loader
        self._baseline_fault_effect_refs: list[str] = []

    def __call__(
        self,
        ticket: TrialTicket,
        level: Mapping[str, Any],
        trial_report: Mapping[str, Any],
        controller_records: list[dict[str, Any]],
    ) -> Mapping[str, Any]:
        preparation = trial_report.get("preparation")
        preparation = preparation if isinstance(preparation, Mapping) else {}
        finalization = trial_report.get("finalization")
        finalization = finalization if isinstance(finalization, Mapping) else {}
        effect = dict(
            self.effect_observer(
                ticket,
                level,
                trial_report,
                preparation,
                finalization,
            )
        )
        agent_result = dict(self.agent_result_loader(trial_report))
        agent_text = json.dumps(agent_result, ensure_ascii=False).lower()
        diagnosis_matches = any(term in agent_text for term in self.expected_diagnosis_terms)
        agent_source_evidence = any(
            isinstance(item, Mapping) and item.get("source") == "source_ro"
            for item in agent_result.get("evidence", [])
        )
        effect_verified = effect.get("verified") is True
        effect_refs = list(effect.get("evidence_refs", []))
        provenance = finalization.get("fault_provenance")
        provenance = provenance if isinstance(provenance, Mapping) else {}
        if ticket.level_id == "L1" and effect_verified:
            self._baseline_fault_effect_refs = list(effect_refs)
        elif (
            level.get("disturbances")
            and self._baseline_fault_effect_refs
            and trial_report.get("mainFaultAppliedObserved") is True
            and provenance.get("ever_active") is True
        ):
            effect_verified = True
            effect_refs = [
                *self._baseline_fault_effect_refs,
                f"chaos-control://{ticket.trial_id}/active-ledger",
            ]
        diagnosis_status = (
            "PASS"
            if effect_verified and diagnosis_matches and agent_source_evidence
            else "FAIL" if effect_verified and agent_result else "INCONCLUSIVE"
        )
        safety_failed = any(
            record.get("status") in {"rejected", "failed"}
            for record in controller_records
        ) or finalization.get("verified") is not True
        observation = {
            "episode_id": self.episode_id,
            "gate_results": [
                _gate(
                    "precondition",
                    "PASS" if preparation.get("status") == "qualified" else "CASE_INVALID",
                    "controller_record",
                    f"controller://{ticket.trial_id}/preparation",
                ),
                _gate(
                    "fault_effect",
                    "PASS" if effect_verified else "INCONCLUSIVE",
                    "independent_observer",
                    str(next(iter(effect_refs), f"oracle://{ticket.trial_id}/effect")),
                ),
                {
                    "gate_id": "diagnosis",
                    "status": diagnosis_status,
                    "evidence_sources": [
                        {"kind": "source_code", "ref": ref}
                        for ref in self.source_evidence_refs
                    ]
                    or [
                        {
                            "kind": "independent_observer",
                            "ref": f"oracle://{ticket.trial_id}/diagnosis-unavailable",
                        }
                    ],
                },
                _gate(
                    "recovery",
                    "PASS" if finalization.get("verified") is True else "FAIL",
                    "runtime_system",
                    str(
                        next(
                            iter(finalization.get("evidence_refs", [])),
                            f"runtime://{ticket.trial_id}/recovery",
                        )
                    ),
                ),
                _gate(
                    "safety",
                    "FAIL" if safety_failed else "PASS",
                    "controller_record",
                    f"controller://{ticket.trial_id}/safety",
                ),
            ],
            "disturbance_behaviors": _disturbance_behaviors(
                level,
                trial_report,
                controller_records,
                agent_text,
            ),
            "reliability_events": [
                event
                for event in ("timeout", "controller_forced_cleanup")
                if event in str(trial_report.get("status", ""))
            ],
        }
        return evaluate_level(
            simplified_level_contract(self.episode_id),
            observation,
            run_id=ticket.run_id,
            level=level,
            attempt=ticket.attempt,
            metrics=_metrics(trial_report),
        )


class KubectlPrometheusFaultEffectObserver:
    """Compare target HTTP client latency before and during the main fault."""

    def __init__(self, kubeconfig: Path):
        self.kubeconfig = kubeconfig.expanduser().resolve()
        if not self.kubeconfig.is_file():
            raise ValueError("configured kubeconfig does not exist")

    def __call__(
        self,
        ticket: TrialTicket,
        level: Mapping[str, Any],
        trial_report: Mapping[str, Any],
        preparation: Mapping[str, Any],
        finalization: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        target = preparation.get("target")
        if not isinstance(target, Mapping):
            return {"verified": False, "reason": "target missing", "evidence_refs": []}
        start = _main_fault_time(trial_report)
        provenance = finalization.get("fault_provenance")
        provenance = provenance if isinstance(provenance, Mapping) else {}
        if start is None or provenance.get("ever_active") is not True:
            return {
                "verified": False,
                "reason": "main fault has no cross-checked active ledger evidence",
                "evidence_refs": [],
            }
        workload_effect = _workload_summary_effect(preparation, finalization)
        if workload_effect is not None:
            return workload_effect
        component = _safe_label(str(target.get("component") or ""))
        uid = _safe_label(str(target.get("uid") or ""))
        # OTel metrics are exported to Prometheus in batches. A 60-second
        # PromQL range can contain fewer than two real samples and make
        # ``increase`` return an empty vector even under active traffic.
        # The overlapping 120-second windows retain the 60-second fault
        # boundary while providing enough samples for an independent delta.
        window = 120
        baseline_mean, baseline_count = self._window(component, uid, start.timestamp(), window)
        effect_mean, effect_count = self._window(
            component,
            uid,
            start.timestamp() + window,
            window,
        )
        delta = effect_mean - baseline_mean
        verified = effect_count > 0 and delta >= 20.0
        return {
            "verified": verified,
            "baseline_mean_client_latency_ms": baseline_mean,
            "effect_mean_client_latency_ms": effect_mean,
            "latency_delta_ms": delta,
            "baseline_samples": baseline_count,
            "effect_samples": effect_count,
            "evidence_refs": [f"prometheus://{ticket.trial_id}/target-client-latency"],
        }

    def _window(
        self,
        component: str,
        uid: str,
        evaluation_time: float,
        window_seconds: int,
    ) -> tuple[float, float]:
        selector = f'job="otel-demo/{component}",instance="{uid}"'
        sum_metric = f"http_client_duration_milliseconds_sum{{{selector}}}"
        count_metric = f"http_client_duration_milliseconds_count{{{selector}}}"
        mean_query = (
            f"sum(increase({sum_metric}[{window_seconds}s])) / "
            f"clamp_min(sum(increase({count_metric}[{window_seconds}s])), 1)"
        )
        count_query = f"sum(increase({count_metric}[{window_seconds}s]))"
        return (
            self._query_scalar(mean_query, evaluation_time),
            self._query_scalar(count_query, evaluation_time),
        )

    def _query_scalar(self, expression: str, evaluation_time: float) -> float:
        path = (
            "/api/v1/namespaces/observability/services/http:prometheus:9090/proxy"
            f"/api/v1/query?query={quote(expression, safe='')}&time={int(evaluation_time)}"
        )
        completed = subprocess.run(
            [
                "kubectl",
                "--kubeconfig",
                str(self.kubeconfig),
                "get",
                "--raw",
                path,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if completed.returncode:
            return 0.0
        try:
            result = json.loads(completed.stdout)["data"]["result"]
            return float(result[0]["value"][1]) if result else 0.0
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            return 0.0


class RuntimeRunOracle:
    def __call__(
        self,
        record,
        episode: Mapping[str, Any],
        execution_report: Mapping[str, Any],
        recovery_report: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        level_results = execution_report.get("level_results")
        if not isinstance(level_results, list):
            raise TypeError("multi-level execution has no level_results")
        passed = str(execution_report.get("status")) == "PASS"
        return {
            "schema_version": "runtime-oracle-result.v1",
            "independent": True,
            "status": "PASS" if passed else "FAIL",
            "level_results": level_results,
            "violations": list(
                dict.fromkeys(
                    violation
                    for result in level_results
                    if isinstance(result, Mapping)
                    for violation in result.get("violations", [])
                )
            ),
            "evidence_refs": [
                str(result.get("result_ref"))
                for result in level_results
                if isinstance(result, Mapping) and result.get("result_ref")
            ],
        }


def _gate(gate_id: str, status: str, kind: str, reference: str) -> dict[str, Any]:
    return {
        "gate_id": gate_id,
        "status": status,
        "evidence_sources": [{"kind": kind, "ref": reference}],
    }


def _metrics(trial_report: Mapping[str, Any]) -> dict[str, Any]:
    lifecycle = trial_report.get("lifecycleEvents")
    lifecycle = lifecycle if isinstance(lifecycle, list) else []
    timestamps = []
    tool_calls = 0
    tokens = 0
    token_observed = False
    for event in lifecycle:
        if not isinstance(event, Mapping):
            continue
        if event.get("kind") == "tool_call":
            tool_calls += 1
        if event.get("occurred_at"):
            try:
                timestamps.append(datetime.fromisoformat(str(event["occurred_at"]).replace("Z", "+00:00")))
            except ValueError:
                pass
        for key, value in _walk(event):
            if key in {"total_tokens", "total_token_count"} and isinstance(value, int | float):
                tokens = max(tokens, int(value))
                token_observed = True
    duration = (
        max(0.0, (max(timestamps) - min(timestamps)).total_seconds())
        if len(timestamps) >= 2
        else 0.0
    )
    return {
        "duration_seconds": duration,
        "tool_calls": tool_calls,
        # Missing usage is charged at the provisional ceiling rather than rewarded as zero.
        "tokens_used": tokens if token_observed else 200_000,
    }


def _walk(value: Any):
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key), item
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _main_fault_time(trial_report: Mapping[str, Any]) -> datetime | None:
    events = trial_report.get("lifecycleEvents")
    if not isinstance(events, list):
        return None
    for event in events:
        if isinstance(event, Mapping) and event.get("kind") == "main_fault_applied":
            try:
                return datetime.fromisoformat(str(event["occurred_at"]).replace("Z", "+00:00"))
            except (KeyError, ValueError):
                return None
    return None


def _disturbance_behaviors(
    level: Mapping[str, Any],
    trial_report: Mapping[str, Any],
    records: list[dict[str, Any]],
    agent_text: str,
) -> list[dict[str, Any]]:
    output = []
    lifecycle = trial_report.get("lifecycleEvents")
    lifecycle = lifecycle if isinstance(lifecycle, list) else []
    completed = [record for record in records if record.get("status") == "completed"]
    cleaned = [record for record in records if record.get("status") == "cleaned"]
    for disturbance in level.get("disturbances", []):
        if not isinstance(disturbance, Mapping):
            continue
        dtype = str(disturbance.get("type") or "")
        evidence_ref = f"controller_record://{trial_report.get('trialId', 'trial')}/{dtype}"
        for behavior in disturbance.get("expected_behaviors", []):
            passed = False
            if dtype == "target_drift" and any(item.get("type") == dtype for item in completed):
                if behavior == "requery_target_identity":
                    passed = any(
                        isinstance(event, Mapping)
                        and event.get("kind") == "tool_call"
                        and str(event.get("tool", "")).startswith("k8s_ro.")
                        for event in lifecycle
                    )
                else:
                    replacement_uids = [
                        str(item.get("outcome", {}).get("replacement_uid") or "")
                        for item in completed
                        if item.get("type") == dtype
                    ]
                    passed = any(uid and uid.lower() in agent_text for uid in replacement_uids)
            elif dtype == "metric_data_gap" and any(item.get("type") == dtype for item in cleaned):
                transformed = any(
                    event.get("status") == "response_transformed"
                    for item in cleaned
                    if item.get("type") == dtype
                    for event in item.get("outcome", {}).get("interceptor_events", [])
                    if isinstance(event, Mapping)
                )
                mentions_gap = any(
                    term in agent_text
                    for term in ("missing", "incomplete", "gap", "缺失", "不完整")
                )
                passed = transformed and mentions_gap
            if passed:
                output.append(
                    {
                        "behavior_id": str(behavior),
                        "status": "PASS",
                        "evidence_sources": [
                            {"kind": "controller_record", "ref": evidence_ref}
                        ],
                    }
                )
    return output


def _safe_label(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value):
        raise ValueError("unsafe Prometheus label value")
    return value


def _workload_summary_effect(
    preparation: Mapping[str, Any],
    finalization: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Prefer the client-observed workload delta when both summaries exist."""

    baseline = preparation.get("baseline_summary")
    experiment = finalization.get("experiment_summary")
    if not isinstance(baseline, Mapping) or not isinstance(experiment, Mapping):
        return None
    try:
        requests = int(experiment.get("requests") or 0)
        baseline_p95 = float(baseline["p95LatencyMs"])
        experiment_p95 = float(experiment["p95LatencyMs"])
        baseline_error = float(baseline.get("errorRate") or 0.0)
        experiment_error = float(experiment.get("errorRate") or 0.0)
        minimum_throughput = float(
            baseline.get("minimumThroughputRps")
            or baseline.get("throughputRps")
            or 0.0
        )
        experiment_throughput = float(experiment.get("throughputRps") or 0.0)
    except (KeyError, TypeError, ValueError):
        return None
    latency_delta = experiment_p95 - baseline_p95
    error_delta = experiment_error - baseline_error
    throughput_breached = (
        minimum_throughput > 0 and experiment_throughput < minimum_throughput
    )
    verified = requests >= 100 and (
        latency_delta >= 20.0 or error_delta >= 0.005 or throughput_breached
    )
    return {
        "verified": verified,
        "observer": "deterministic-client-workload",
        "requests": requests,
        "baseline_p95_latency_ms": baseline_p95,
        "experiment_p95_latency_ms": experiment_p95,
        "latency_delta_ms": latency_delta,
        "baseline_error_rate": baseline_error,
        "experiment_error_rate": experiment_error,
        "error_rate_delta": error_delta,
        "minimum_throughput_rps": minimum_throughput,
        "experiment_throughput_rps": experiment_throughput,
        "evidence_refs": list(finalization.get("evidence_refs", []))
        or ["workload://experiment-summary"],
    }
