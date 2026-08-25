"""Per-attempt reset, target rebind, formal baseline, and capability issuance."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from mcp_servers.chaos_control.service import new_cleanup_handle
from progression.controller import TrialTicket
from scripts.reset_episode import (
    LOCUST_IMAGE_ENV,
    SubprocessCommandRunner,
    run_checked,
    wait_application_ready,
    wait_cleanup_workload,
    workload_command,
)

from .runtime_secrets import BaselineCapabilityIssuer
from .system_snapshot import (
    RuntimeInventoryAdapter,
    SnapshotStatus,
)


class TrialPreparationError(RuntimeError):
    pass


class ResetVerifier(Protocol):
    def __call__(self, ticket: TrialTicket, level: Mapping[str, Any]) -> Mapping[str, Any]: ...


class TargetResolver(Protocol):
    def __call__(self, ticket: TrialTicket, level: Mapping[str, Any]) -> Mapping[str, Any]: ...


class BaselineMeasurer(Protocol):
    def __call__(
        self,
        ticket: TrialTicket,
        level: Mapping[str, Any],
        target: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


class ExperimentWorkloadSession(Protocol):
    def start(
        self,
        ticket: TrialTicket,
        level: Mapping[str, Any],
        target: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...

    def finish(self, ticket: TrialTicket) -> Mapping[str, Any]: ...


class TrialRuntimeContextStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def save(self, trial_id: str, value: Mapping[str, Any]) -> None:
        if not re.fullmatch(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$", trial_id):
            raise TrialPreparationError("invalid trial_id")
        path = self.root / f"{trial_id}.json"
        path.write_text(
            json.dumps(dict(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)

    def load(self, trial_id: str) -> dict[str, Any]:
        path = self.root / f"{trial_id}.json"
        if not path.is_file():
            raise TrialPreparationError("trial runtime context is missing")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise TrialPreparationError("trial runtime context is invalid")
        return value


class PerTrialPreparer:
    def __init__(
        self,
        *,
        reset_verifier: ResetVerifier,
        target_resolver: TargetResolver,
        baseline_measurer: BaselineMeasurer,
        capability_issuer: BaselineCapabilityIssuer,
        context_store: TrialRuntimeContextStore,
        experiment_workload_session: ExperimentWorkloadSession | None = None,
    ):
        self.reset_verifier = reset_verifier
        self.target_resolver = target_resolver
        self.baseline_measurer = baseline_measurer
        self.capability_issuer = capability_issuer
        self.context_store = context_store
        self.experiment_workload_session = experiment_workload_session

    def __call__(self, ticket: TrialTicket, level: Mapping[str, Any]) -> Mapping[str, Any]:
        reset = dict(self.reset_verifier(ticket, level))
        if reset.get("verified") is not True:
            raise TrialPreparationError("pre-trial reset and residual verification failed")
        target = dict(self.target_resolver(ticket, level))
        required = {"namespace", "kind", "name", "uid", "component"}
        if target.get("kind") != "Pod" or not required <= set(target):
            raise TrialPreparationError("target resolver did not return one exact Pod identity")
        baseline = dict(self.baseline_measurer(ticket, level, target))
        summary = baseline.get("summary")
        if baseline.get("qualified") is not True or not isinstance(summary, Mapping):
            raise TrialPreparationError("formal per-trial baseline did not qualify")
        capability = self.capability_issuer.issue(
            trial_id=ticket.trial_id,
            run_id=ticket.run_id,
            namespace=str(target["namespace"]),
            target_name=str(target["name"]),
            target_uid=str(target["uid"]),
            summary=summary,
        )
        experiment_workload = (
            dict(self.experiment_workload_session.start(ticket, level, target))
            if self.experiment_workload_session is not None
            else {"status": "not_configured"}
        )
        if self.experiment_workload_session is not None and experiment_workload.get(
            "status"
        ) != "running":
            raise TrialPreparationError("experiment workload did not start")
        context = {
            "schema_version": "trial-runtime-context.v1",
            "status": "qualified",
            "trial_id": ticket.trial_id,
            "level_id": ticket.level_id,
            "attempt": ticket.attempt,
            "target": target,
            "cleanup_handle": new_cleanup_handle(),
            "baseline_gate_token_ref": capability["baseline_gate_token_ref"],
            "baseline_ledger_ref": capability["baseline_ledger_ref"],
            "baseline_summary_sha256": capability["summary_sha256"],
            "baseline_summary": dict(summary),
            "fresh_smoke_summary": baseline.get("fresh_smoke_summary"),
            "reset_evidence_refs": list(reset.get("evidence_refs", [])),
            "baseline_evidence_refs": list(baseline.get("evidence_refs", [])),
            "experiment_workload": experiment_workload,
        }
        self.context_store.save(ticket.trial_id, context)
        return context


class LiveResetVerifier:
    def __init__(self, runtime_adapter: RuntimeInventoryAdapter, namespace: str):
        self.runtime_adapter = runtime_adapter
        self.namespace = namespace

    def __call__(self, ticket: TrialTicket, level: Mapping[str, Any]) -> Mapping[str, Any]:
        runtime = self.runtime_adapter.scan(self.namespace)
        verified = (
            runtime.status is SnapshotStatus.QUALIFIED
            and runtime.chaosblade_global_count == 0
        )
        return {
            "verified": verified,
            "evidence_refs": [f"runtime://{ticket.trial_id}/pre-trial-inventory"],
            "runtime": runtime.model_dump(mode="json"),
        }


class LiveTargetResolver:
    def __init__(
        self,
        runtime_adapter: RuntimeInventoryAdapter,
        *,
        namespace: str,
        component: str,
    ):
        self.runtime_adapter = runtime_adapter
        self.namespace = namespace
        self.component = component

    def __call__(self, ticket: TrialTicket, level: Mapping[str, Any]) -> Mapping[str, Any]:
        runtime = self.runtime_adapter.scan(self.namespace)
        key = _component_key(self.component)
        matches = [
            target
            for target in runtime.targets
            if target.ready and target.component and _component_key(target.component) == key
        ]
        if len(matches) != 1:
            raise TrialPreparationError(
                f"component {self.component} resolved to {len(matches)} live Ready Pods"
            )
        target = matches[0]
        return {
            "namespace": self.namespace,
            "kind": "Pod",
            "name": target.name,
            "uid": target.uid,
            "component": self.component,
        }


class FormalOtelBaselineMeasurer:
    def __init__(
        self,
        *,
        kubeconfig: Path,
        workload_image: str,
        runner: Any | None = None,
        environment: Mapping[str, str] | None = None,
    ):
        self.kubeconfig = kubeconfig.expanduser().resolve()
        self.workload_image = workload_image
        self.runner = runner or SubprocessCommandRunner()
        self.environment = dict(environment or {})

    def __call__(
        self,
        ticket: TrialTicket,
        level: Mapping[str, Any],
        target: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        env = {**self.environment, LOCUST_IMAGE_ENV: self.workload_image}
        workload = wait_cleanup_workload(
            self.runner,
            "otel-demo",
            ticket.trial_id.lower(),
            self.kubeconfig,
            env,
            timeout_seconds=1200,
            duration_seconds=600,
        )
        wait_application_ready("otel-demo", self.kubeconfig, 900)
        summary = workload.get("summary")
        return {
            "qualified": isinstance(summary, Mapping) and summary.get("qualified") is True,
            "summary": summary,
            "measurement_mode": "formal-per-trial",
            "formal_run_eligible": True,
            "evidence_refs": [
                f"kubernetes://otel-demo/pvc/otel-demo-workload-results/{ticket.trial_id}"
            ],
        }


class EngineeringOtelBaselineMeasurer:
    """Combine a retained formal baseline with a fresh short health smoke.

    The retained 600-second summary remains the only input to the signed
    baseline capability. The fresh smoke proves that the live target still
    satisfies the SLO immediately before a trial, but is explicitly not a
    formal ranking window.
    """

    def __init__(
        self,
        *,
        kubeconfig: Path,
        workload_image: str,
        formal_baseline_report: Path,
        smoke_duration_seconds: int = 60,
        runner: Any | None = None,
        environment: Mapping[str, str] | None = None,
    ):
        if not 60 <= smoke_duration_seconds <= 300:
            raise TrialPreparationError(
                "engineering smoke duration must be between 60 and 300 seconds"
            )
        self.kubeconfig = kubeconfig.expanduser().resolve()
        self.workload_image = workload_image
        self.formal_baseline_report = formal_baseline_report.expanduser().resolve()
        self.smoke_duration_seconds = smoke_duration_seconds
        self.runner = runner or SubprocessCommandRunner()
        self.environment = dict(environment or {})

    def __call__(
        self,
        ticket: TrialTicket,
        level: Mapping[str, Any],
        target: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        formal_summary = self._load_formal_summary()
        env = {**self.environment, LOCUST_IMAGE_ENV: self.workload_image}
        smoke_run_id = f"smoke-{ticket.trial_id.lower()}"[:63].rstrip("-")
        workload = wait_cleanup_workload(
            self.runner,
            "otel-demo",
            smoke_run_id,
            self.kubeconfig,
            env,
            timeout_seconds=self.smoke_duration_seconds + 300,
            duration_seconds=self.smoke_duration_seconds,
        )
        wait_application_ready("otel-demo", self.kubeconfig, 300)
        smoke_summary = workload.get("summary")
        qualified = (
            isinstance(smoke_summary, Mapping)
            and smoke_summary.get("qualified") is True
        )
        return {
            "qualified": qualified,
            "summary": formal_summary,
            "fresh_smoke_summary": smoke_summary,
            "measurement_mode": "engineering-reference-plus-smoke",
            "formal_run_eligible": False,
            "evidence_refs": [
                f"file://{self.formal_baseline_report}",
                f"kubernetes://otel-demo/pvc/otel-demo-workload-results/{smoke_run_id}",
            ],
        }

    def _load_formal_summary(self) -> dict[str, Any]:
        if not self.formal_baseline_report.is_file():
            raise TrialPreparationError("retained formal baseline report is missing")
        try:
            report = json.loads(
                self.formal_baseline_report.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise TrialPreparationError(
                "retained formal baseline report is invalid"
            ) from exc
        summary = report.get("summary") if isinstance(report, Mapping) else None
        window = summary.get("measurementWindow") if isinstance(summary, Mapping) else None
        if (
            not isinstance(report, Mapping)
            or report.get("qualified") is not True
            or not isinstance(summary, Mapping)
            or summary.get("qualified") is not True
            or not isinstance(window, Mapping)
            or window.get("calibrationWindowEligible") is not True
            or int(window.get("durationSeconds") or 0) < 600
            or int(window.get("measurementWindowSeconds") or 0) < 300
        ):
            raise TrialPreparationError(
                "retained report is not a qualified 600/300-second formal baseline"
            )
        return dict(summary)


class OtelExperimentWorkloadSession:
    def __init__(
        self,
        *,
        kubeconfig: Path,
        workload_image: str,
        duration_seconds: int = 900,
        runner: Any | None = None,
        environment: Mapping[str, str] | None = None,
    ):
        if not 60 <= duration_seconds <= 3600:
            raise TrialPreparationError(
                "experiment workload duration must be between 60 and 3600 seconds"
            )
        self.kubeconfig = kubeconfig.expanduser().resolve()
        self.workload_image = workload_image
        self.duration_seconds = duration_seconds
        self.runner = runner or SubprocessCommandRunner()
        self.environment = dict(environment or {})

    def start(
        self,
        ticket: TrialTicket,
        level: Mapping[str, Any],
        target: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        run_id = _experiment_workload_run_id(ticket)
        env = {**self.environment, LOCUST_IMAGE_ENV: self.workload_image}
        command = workload_command(
            "otel-demo",
            run_id,
            self.kubeconfig,
            env,
            duration_seconds=self.duration_seconds,
        )
        try:
            run_checked(self.runner, command, timeout=120)
        except Exception:
            self._stop(run_id)
            raise
        return {
            "status": "running",
            "run_id": run_id,
            "duration_seconds": self.duration_seconds,
            "evidence_refs": [f"kubernetes://otel-demo/job/{run_id}"],
        }

    def finish(self, ticket: TrialTicket) -> Mapping[str, Any]:
        run_id = _experiment_workload_run_id(ticket)
        env = {**self.environment, LOCUST_IMAGE_ENV: self.workload_image}
        return wait_cleanup_workload(
            self.runner,
            "otel-demo",
            run_id,
            self.kubeconfig,
            env,
            timeout_seconds=self.duration_seconds + 300,
            duration_seconds=self.duration_seconds,
            start_job=False,
        )

    def _stop(self, run_id: str) -> None:
        selector = (
            "resiliencebenchmark.io/workload=otel-demo,"
            f"resiliencebenchmark.io/run-id={run_id}"
        )
        try:
            run_checked(
                self.runner,
                [
                    "kubectl",
                    "--kubeconfig",
                    str(self.kubeconfig),
                    "delete",
                    "job,configmap,pod",
                    "-n",
                    "otel-demo",
                    "-l",
                    selector,
                    "--ignore-not-found=true",
                ],
                timeout=60,
            )
        except (OSError, RuntimeError, ValueError):
            return


def _component_key(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]", "", value.lower())
    return normalized.removesuffix("service")


def _experiment_workload_run_id(ticket: TrialTicket) -> str:
    base = re.sub(r"[^a-z0-9-]", "-", ticket.trial_id.lower()).strip("-")
    return f"exp-{base}"[:63].rstrip("-")
