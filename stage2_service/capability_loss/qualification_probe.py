"""Generate the Controller-private D7/D8 capability-loss qualification file.

D7/D8 Trials refuse to start their disturbance unless a platform-owned file
(read by ``CapabilityLossRuntimeFactory._qualification``) proves that the
alternative path works.  This module produces that file from real probes and
records only what they observed:

* D7 binds each target Pod exactly as a Trial binds its target and performs one
  bounded metric read through the same client code and MCP environment that
  ``coroot_ro`` and ``telemetry_ro`` use.  A historical sample is written only
  when the server returned in-window data labelled with that Pod.
* D8 runs one short canary per alternative executor (``chaos_mesh_control``
  and ``chaos_control``) through the controlled-execution service behind that
  MCP server (every create gate and the cleanup ledger), destroys it in
  ``finally`` and records a canary only when both the applied experiment and
  its verified absence were observed.  D8 is bidirectional: the alternative is
  whichever executor the Agent did not validate on first.

Run it in the ``stage2`` container while no campaign is active::

    python -m stage2_service.capability_loss.qualification_probe [--d7] [--d8]
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import json
import math
import os
import secrets
import stat
import sys
import tempfile
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from mcp_servers.chaos_control.service import ChaosControlService
from mcp_servers.chaos_core.backends.chaos_mesh import UID_FENCE_LABEL, _fence_value
from mcp_servers.chaos_core.contracts import ChaosBackend, ChaosControlError
from mcp_servers.chaos_core.contracts import RuntimeConfig as ChaosRuntimeConfig
from mcp_servers.chaos_core.gates import _experiment_name
from mcp_servers.chaos_core.service import ControlledExecutionService, new_cleanup_handle
from mcp_servers.chaos_mesh_control.service import ChaosMeshControlService
from mcp_servers.common.scope import ScopeError
from mcp_servers.coroot_ro.service import CorootROError, CorootROService, CorootTransport
from mcp_servers.coroot_ro.service import RuntimeConfig as CorootRuntimeConfig
from mcp_servers.coroot_ro.service import _metric_query as coroot_metric_query
from mcp_servers.coroot_ro.service import error_envelope as coroot_error_envelope
from mcp_servers.telemetry_ro.service import TelemetryROError, TelemetryROService, TelemetryTransport
from mcp_servers.telemetry_ro.service import RuntimeConfig as TelemetryRuntimeConfig
from mcp_servers.telemetry_ro.service import error_envelope as telemetry_error_envelope
from stage2_service.preparation import _ready
from stage2_service.target_binding import current as current_target_binding
from stage2_service.runtime_lock import RuntimeLock, RuntimeLockError

from .factory import (
    QUALIFICATION_SCHEMA,
    CapabilityLossRuntimeFactory,
    _mapping,
    _read_private_regular_file,
    _time,
    _trusted_private_regular_file,
)
from .records import D7HistoricalSample, D8CanaryEvidence


EVIDENCE_SCHEMA = "stage2-capability-loss-qualification-evidence.v1"
GENERATOR = "stage2_service.capability_loss.qualification_probe"
# ``CapabilityLossRuntimeFactory._qualification`` accepts only the application
# this Controller instance is bound to.
APPLICATION = current_target_binding().application
OUTPUT_ENV = "STAGE2_SUBSTITUTION_QUALIFICATION_FILE"
OUTPUT_FILENAME = "capability-loss-qualification.json"
PRIVATE_ROOT_ENV = "STAGE2_PRIVATE_ROOT"
DEFAULT_PRIVATE_ROOT = "/var/lib/resbench-stage2/private"
EVIDENCE_RELATIVE_DIR = Path("capability-loss") / "qualification-evidence"
RECORD_REF_PREFIX = "private://capability-loss/qualification-evidence/"
DEFAULT_TARGET = "cart"
MAX_TTL_HOURS = 7 * 24

COROOT_SERVER = "coroot_ro"
TELEMETRY_SERVER = "telemetry_ro"
D7_SERVERS = (COROOT_SERVER, TELEMETRY_SERVER)

# D7 reads one bounded window of per-container CPU usage: every running
# container reports it whatever its instrumentation, and both backends name the
# Pod in the labels they return (Coroot ``container_id``, cAdvisor ``pod``).
D7_LOOKBACK_SECONDS = 600
COROOT_METRIC = "container_resources_cpu_usage_seconds_total"
TELEMETRY_METRIC = "container_cpu_usage_seconds_total"
TELEMETRY_STEP_SECONDS = 60

# A canary proves an executor path, not a fault effect: each one injects the
# smallest fault its executor accepts for at most 15 s (the cleanup-ledger
# deadline) and is destroyed as soon as the executor reports it applied.  The
# per-executor choices and their reasons are in ``CANARIES`` below.
CANARY_DURATION_SECONDS = 15
# Stop waiting before Chaos Mesh recovers on its own and before the ledger
# deadline, so "Running" is an observation of the live injection.
CANARY_RUNNING_TIMEOUT_SECONDS = 12
CANARY_POLL_INTERVAL_SECONDS = 1.0
# Per-Trial executor keys that must never leak into the canary configuration.
_TRIAL_ONLY_CHAOS_ENV = (
    "RESBENCH_MCP_POLICY_FILE",
    "RESBENCH_USER_DECISION_FILE",
    "RESBENCH_CHAOS_CONDITION_SAFETY_TTL_SECONDS",
    "RESBENCH_CHAOS_CREATE_UNCERTAINTY_VARIANT",
)
# Every one of these must be observed before a canary counts as destroyed.
_DESTROY_FACTS = (
    "destroy_verified_absent", "ledger_destroyed", "absent_for_executor",
    "absent_for_finalizer", "no_run_resources_left", "fence_label_absent",
    "target_uid_unchanged", "target_ready",
)
TARGET_BINDING = (
    "KubernetesTrialPreparer._resolve_target: the single Ready Pod labelled "
    "app.kubernetes.io/component=<component> or opentelemetry.io/name=<component>"
)
_PROVENANCE_KIND = {"d7": "d7_historical_sample", "d8": "d8_canary"}


@dataclass(frozen=True)
class CanarySpec:
    """One alternative executor and the smallest fault its D8 canary injects."""

    server: str
    executor_id: str
    run_tag: str
    fault_type: str
    intensity: Mapping[str, int]
    # What still ends the fault if this process dies between create and destroy.
    backstop: str

    def plan(self) -> dict[str, Any]:
        return {
            "fault_type": self.fault_type,
            "duration_seconds": CANARY_DURATION_SECONDS,
            "intensity": dict(self.intensity),
        }


CANARIES: dict[str, CanarySpec] = {
    # 1 ms of egress delay on the UID-fenced Pod, the delay WP8 BladeAI
    # qualification also uses.
    "chaos_mesh_control": CanarySpec(
        server="chaos_mesh_control", executor_id="chaos_mesh", run_tag="mesh",
        fault_type="network-delay", intensity={"delay_ms": 1},
        backstop=f"Chaos Mesh ends it after its {CANARY_DURATION_SECONDS}s duration and the platform TIMER reaps its ledger entry",
    ),
    # cpu-load is the ChaosBlade fault proven on this cluster (ChaosBlade 1.8.0
    # operator with the nsenter cgroup-v2 wrapper); its network faults have
    # never been exercised here, and a canary that may not apply proves nothing.
    # The policy accepts 0 < cpu_percent <= 100 and the manifest passes it as an
    # integer ``cpu-percent`` string, so 1 % is the smallest load it can carry.
    "chaos_control": CanarySpec(
        server="chaos_control", executor_id="chaosblade", run_tag="blade",
        fault_type="cpu-load", intensity={"cpu_percent": 1},
        backstop=(
            "ChaosBlade manifests carry no duration, so only the platform TIMER reaps it, and only while a "
            "chaos_control watchdog or condition monitor runs; the environment gate refuses new Trials "
            "while any ChaosBlade CR exists"
        ),
    ),
}
D8_SERVERS = tuple(CANARIES)


class ProbeError(RuntimeError):
    """The probe cannot run or cannot produce a trustworthy file."""


class _CanaryStopped(Exception):
    """A canary gate refused the plan before any Kubernetes mutation."""


@dataclass(frozen=True)
class TargetPod:
    """One live Pod, bound the way a Stage-2 Trial binds its target."""

    namespace: str
    component: str
    name: str
    uid: str
    created_at: datetime | None
    containers: tuple[str, ...]
    labels: Mapping[str, str]
    ready: bool

    def summary(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "component": self.component,
            "pod_name": self.name,
            "pod_uid": self.uid,
            "pod_created_at": _iso(self.created_at) if self.created_at else None,
            "containers": list(self.containers),
            "ready": self.ready,
        }


@dataclass(frozen=True)
class ProbePlatform:
    """Platform services the probe reuses; ``platform_from_stage2_runtime`` builds the real ones."""

    namespace: str
    private_root: Path
    resolve_target: Callable[[str], TargetPod]
    read_pod: Callable[[str], TargetPod | None]
    live_pod_uids: Callable[[], frozenset[str]]
    issue_baseline: Callable[[str, TargetPod], str]
    coroot_application_id: Callable[[TargetPod], str]
    # The environment McpSupervisor starts every MCP server with.
    mcp_environment: Mapping[str, str]
    # ``None`` selects the production transport/backend of each service.
    coroot_transport: CorootTransport | None = None
    telemetry_transport: TelemetryTransport | None = None
    chaos_mesh_backend: ChaosBackend | None = None
    chaosblade_backend: ChaosBackend | None = None


@dataclass(frozen=True)
class ProbeOutcome:
    """One probe result; ``entry`` is set only for a successful probe."""

    kind: str
    server: str
    target: TargetPod | None
    ok: bool
    reason: str
    entry: dict[str, Any] | None = None
    record_ref: str | None = None
    alert: str | None = None

    @property
    def probe(self) -> str:
        return f"{self.kind}:{self.server}"


class _NoTelemetryDisturbance:
    """Bypass the file-backed D3/D4 hook: qualification reads the backend itself."""

    async def before_tool(self, tool: str) -> None:
        return None

    def after_tool(self, tool: str, response: Mapping[str, Any]) -> dict[str, Any]:
        return dict(response)


def kubernetes_target_access(
    preparer: Any, namespace: str,
) -> tuple[Callable[[str], TargetPod], Callable[[str], TargetPod | None], Callable[[], frozenset[str]]]:
    """Target lookups built on the preparer a campaign uses to bind Trial targets."""

    core_api = preparer.core_api

    def read_pod(name: str) -> TargetPod | None:
        # ``list`` needs only the permission the preparer already uses, and a
        # missing Pod is an empty list rather than an API error.
        pods = core_api.list_namespaced_pod(namespace=namespace, field_selector=f"metadata.name={name}").items
        return _target_pod(pods[0], namespace=namespace, component="") if pods else None

    def resolve_target(component: str) -> TargetPod:
        # The exact binding KubernetesTrialPreparer.prepare applies to a
        # Controller-explicit Trial target.
        bound = preparer._resolve_target(namespace, component)
        pod = read_pod(bound.name)
        if pod is None or pod.uid != bound.uid:
            raise ProbeError(f"target {component} was replaced while it was being resolved")
        return replace(pod, component=component)

    def live_pod_uids() -> frozenset[str]:
        pods = core_api.list_namespaced_pod(namespace=namespace).items
        return frozenset(
            str(pod.metadata.uid) for pod in pods
            if pod.metadata.uid and not getattr(pod.metadata, "deletion_timestamp", None)
        )

    return resolve_target, read_pod, live_pod_uids


def platform_from_stage2_runtime(namespace: str) -> ProbePlatform:
    """Compose the probe from the objects a Stage-2 campaign builds for a Trial."""

    # Imported lazily: the runtime factory loads every Harness adapter and is
    # only usable inside the Controller container.
    from stage2_service.contracts import RuntimeTarget
    from stage2_service.episode import load_fixed_episode
    from stage2_service.harness_runtime import _coroot_application_id
    from stage2_service.matrix import fixed_otel_episode_ref
    from stage2_service.runtime_factory import Stage2RuntimeConfig, Stage2System

    config = Stage2RuntimeConfig.from_env()
    system = Stage2System(config)
    episode = load_fixed_episode(fixed_otel_episode_ref(config.repo_root), root=config.repo_root)
    # No Harness runs here, so no model routing is needed.
    components = system.build_runtime(episode, {}, namespace=namespace)
    base_environment = dict(components.supervisor.base_environment)
    resolve_target, read_pod, live_pod_uids = kubernetes_target_access(components.preparer, namespace)

    def issue_baseline(run_id: str, target: TargetPod) -> str:
        # The Trial create gate: a Controller capability issued only while
        # application-owned traffic is observed, bound to this Pod UID.
        runtime_target = RuntimeTarget(
            namespace=target.namespace, component=target.component, name=target.name, uid=target.uid,
        )
        return components.issuer.issue(run_id, namespace=namespace, target=runtime_target)

    return ProbePlatform(
        namespace=namespace,
        private_root=config.private_root,
        resolve_target=resolve_target,
        read_pod=read_pod,
        live_pod_uids=live_pod_uids,
        issue_baseline=issue_baseline,
        coroot_application_id=lambda target: _coroot_application_id(base_environment, target),
        # McpSupervisor launches each server with os.environ plus this base.
        mcp_environment={**os.environ, **base_environment},
    )


class QualificationProbe:
    """Run the D7/D8 probes and maintain the qualification file they justify."""

    def __init__(
        self,
        *,
        platform: ProbePlatform,
        targets: Sequence[str],
        canary_target: str,
        output_path: Path,
        ttl: timedelta,
        clock: Callable[[], datetime],
        sleep: Callable[[float], Awaitable[None]],
    ) -> None:
        if ttl <= timedelta(0):
            raise ValueError("ttl must be positive")
        self.platform = platform
        self.targets = tuple(dict.fromkeys(targets))
        self.canary_target = canary_target
        self.output_path = Path(output_path).absolute()
        self.evidence_dir = platform.private_root / EVIDENCE_RELATIVE_DIR
        self.ttl = ttl
        self.clock = clock
        self.sleep = sleep

    def run(self, *, d7: bool, d8_servers: Sequence[str]) -> dict[str, Any]:
        """Run the selected probes, then merge, write and re-validate the file."""

        d8_servers = tuple(dict.fromkeys(d8_servers))
        outcomes = asyncio.run(self._probe(d7=d7, d8_servers=d8_servers))
        # Liveness is read after the probes so a Pod replaced meanwhile still
        # invalidates every sample bound to its old UID.
        live_uids = self.platform.live_pod_uids()
        _ensure_private_directory(self.output_path.parent)
        with _exclusive_file_lock(self.output_path):
            existing, existing_state = _load_existing(self.output_path, self.platform.namespace)
            document, dropped = merge_qualification(
                existing, outcomes, live_uids=live_uids, namespace=self.platform.namespace,
                issued_at=self.clock(), ttl=self.ttl,
            )
            _write_private_json(self.output_path, document)
        accepted, prechecks = self._loader_check()
        failures = [
            {"probe": outcome.probe, "target": _target_label(outcome.target),
             "reason": outcome.reason, "evidence": outcome.record_ref}
            for outcome in outcomes if not outcome.ok
        ]
        return {
            "ok": not failures and accepted and all(prechecks.values()),
            "dry_run": False,
            "modes": [mode for mode, selected in (("d7", d7), ("d8", bool(d8_servers))) if selected],
            "d8_servers": list(d8_servers),
            "namespace": self.platform.namespace,
            "output": str(self.output_path),
            "existing_file": existing_state,
            "issued_at": document["scope"]["issued_at"],
            "expires_at": document["scope"]["expires_at"],
            "d7_historical_samples": document["d7_historical_samples"],
            "d8_canaries": document["d8_canaries"],
            "failures": failures,
            "alerts": [outcome.alert for outcome in outcomes if outcome.alert],
            "dropped": dropped,
            "loader_accepted": accepted,
            "prechecks": prechecks,
        }

    async def _probe(self, *, d7: bool, d8_servers: tuple[str, ...]) -> list[ProbeOutcome]:
        components = list(self.targets) if d7 else []
        if d8_servers and self.canary_target not in components:
            components.append(self.canary_target)
        bound: dict[str, TargetPod] = {}
        outcomes: list[ProbeOutcome] = []
        for component in components:
            try:
                bound[component] = self.platform.resolve_target(component)
            except Exception as exc:  # noqa: BLE001 - reported as failed probes
                reason = f"target {component} cannot be bound like a Trial target: {type(exc).__name__}: {exc}"
                if d7 and component in self.targets:
                    outcomes.extend(ProbeOutcome("d7", server, None, False, reason) for server in D7_SERVERS)
                if component == self.canary_target:
                    outcomes.extend(ProbeOutcome("d8", server, None, False, reason) for server in d8_servers)
        if d7:
            for component in self.targets:
                if (target := bound.get(component)) is not None:
                    outcomes.append(await self._probe_coroot(target))
                    outcomes.append(await self._probe_telemetry(target))
        canary_target = bound.get(self.canary_target)
        if canary_target is not None:
            # Canaries run one after another, each destroyed before the next.
            for server in d8_servers:
                if any(outcome.alert for outcome in outcomes):
                    # Never stack a second fault on a Pod whose earlier canary may remain.
                    outcomes.append(ProbeOutcome(
                        "d8", server, canary_target, False, "skipped: an earlier canary's cleanup was not verified",
                    ))
                    continue
                outcomes.append(await self._run_canary(CANARIES[server], canary_target))
        return outcomes

    # D7: bounded reads through the observation MCP servers' own client code.

    async def _probe_coroot(self, target: TargetPod) -> ProbeOutcome:
        started = self.clock()
        end = int(started.timestamp())
        start = end - D7_LOOKBACK_SECONDS
        environment = {
            **self.platform.mcp_environment,
            "RESBENCH_COROOT_APPLICATION_ID": self.platform.coroot_application_id(target),
        }
        try:
            # coroot_ro reads its scope only from the process environment, so
            # expose the exact MCP environment while its own parser runs.
            with _environ_overlay(environment):
                config = CorootRuntimeConfig.from_env()
            service = CorootROService(config=config, transport=self.platform.coroot_transport)
        except (CorootROError, ScopeError) as exc:
            return self._d7_outcome(COROOT_SERVER, target, started, [], [], None, error=coroot_error_envelope(exc))
        prefix = f"/k8s/{target.namespace}/{target.name}/"
        attempts: list[dict[str, Any]] = []
        matches: list[dict[str, Any]] = []
        latest: float | None = None
        for container in target.containers:
            labels = {"container_id": prefix + container}
            response = await _guarded_read(
                service.metrics_range(metric=COROOT_METRIC, start=start, end=end, labels=labels),
                (CorootROError, ScopeError), coroot_error_envelope,
            )
            series = _mapping(response.get("data")).get("result")
            found, found_latest = _matching_series(
                series, lambda labels_: str(labels_.get("container_id", "")).startswith(prefix),
                not_before=_timestamp(target.created_at), not_after=end,
            )
            attempts.append({
                "request": {
                    "tool": "coroot_metrics_range", "metric": COROOT_METRIC, "start": start, "end": end,
                    "labels": labels, "promql": _coroot_promql(config.scope.namespace, labels),
                },
                "response": _response_summary(response, series, found),
            })
            matches.extend(found)
            latest = _later(latest, found_latest)
            if found:
                break
        return self._d7_outcome(COROOT_SERVER, target, started, attempts, matches, latest)

    async def _probe_telemetry(self, target: TargetPod) -> ProbeOutcome:
        started = self.clock()
        end = int(started.timestamp())
        start = end - D7_LOOKBACK_SECONDS
        try:
            with _environ_overlay(self.platform.mcp_environment):
                config = TelemetryRuntimeConfig.from_env()
            # Trial disturbances are injected through the telemetry hook; a
            # platform qualification must observe the backend itself.
            service = TelemetryROService(
                config=config, transport=self.platform.telemetry_transport,
                disturbance_hook=_NoTelemetryDisturbance(),
            )
        except TelemetryROError as exc:
            return self._d7_outcome(TELEMETRY_SERVER, target, started, [], [], None, error=telemetry_error_envelope(exc))
        labels = {"pod": target.name}
        response = await _guarded_read(
            service.prometheus_metric_range(
                metric=TELEMETRY_METRIC, start=start, end=end, step=TELEMETRY_STEP_SECONDS, labels=labels,
            ),
            (TelemetryROError,), telemetry_error_envelope,
        )
        series = response.get("result")
        found, latest = _matching_series(
            series,
            lambda labels_: labels_.get("pod") == target.name and labels_.get("namespace") == target.namespace,
            not_before=_timestamp(target.created_at), not_after=end,
        )
        for match in found:
            # Corroboration only: cAdvisor's cgroup path usually embeds the UID.
            cgroup = str(match["labels"].get("id") or "")
            match["pod_uid_in_cgroup_id"] = target.uid in cgroup or target.uid.replace("-", "_") in cgroup
        attempts = [{
            "request": {
                "tool": "telemetry_prom_metric_range", "metric": TELEMETRY_METRIC, "start": start, "end": end,
                "step": TELEMETRY_STEP_SECONDS, "labels": labels, "promql": response.get("query"),
            },
            "response": _response_summary(response, series, found),
        }]
        return self._d7_outcome(TELEMETRY_SERVER, target, started, attempts, found, latest)

    def _d7_outcome(
        self,
        server: str,
        target: TargetPod,
        started: datetime,
        attempts: list[dict[str, Any]],
        matches: list[dict[str, Any]],
        latest: float | None,
        *,
        error: Mapping[str, Any] | None = None,
    ) -> ProbeOutcome:
        current, read_error = self._read_pod_safely(target.name)
        if error is not None:
            reason = f"{server} could not be configured like its MCP server: {_error_text(error)}"
        elif read_error is not None or current is None or current.uid != target.uid:
            reason = "target Pod was replaced or unreadable during the probe"
        elif latest is None:
            reason = f"{server} returned no data labelled with Pod {target.name} in the last {D7_LOOKBACK_SECONDS}s"
        else:
            reason = None
        observed_at = datetime.fromtimestamp(latest, UTC) if reason is None and latest is not None else None
        record_ref = self._write_evidence(f"d7-{server}-{target.uid}-{_stamp(started)}.json", {
            "kind": "d7_historical_sample",
            "server": server,
            "target": target.summary(),
            "binding": TARGET_BINDING,
            "lookback_seconds": D7_LOOKBACK_SECONDS,
            "attempts": attempts,
            "configuration_error": dict(error) if error else None,
            "target_after_probe": current.summary() if current else None,
            "target_read_error": read_error,
            "accepted": reason is None,
            "reason": reason or "server returned in-window data labelled with the target Pod",
            "observed_at": _iso(observed_at) if observed_at else None,
            "probe_started_at": _iso(started),
            "probe_finished_at": _iso(self.clock()),
        })
        if reason is not None or observed_at is None:
            return ProbeOutcome("d7", server, target, False, reason or "no observation", record_ref=record_ref)
        entry = {"server": server, "target_uid": target.uid, "observed_at": _iso(observed_at), "record_ref": record_ref}
        D7HistoricalSample.model_validate(entry)  # fail loudly if the record contract drifts
        return ProbeOutcome("d7", server, target, True, "observed", entry, record_ref)

    # D8: one canary per alternative executor, through its MCP server's service.

    async def _run_canary(self, spec: CanarySpec, target: TargetPod) -> ProbeOutcome:
        started = self.clock()
        run_id = f"d8-canary-{spec.run_tag}-{started.astimezone(UTC):%Y%m%dt%H%M%S}-{secrets.token_hex(3)}"
        cleanup_handle = new_cleanup_handle()
        plan = spec.plan()
        evidence: dict[str, Any] = {
            "kind": "d8_canary",
            "alternative_server": spec.server,
            "executor_id": spec.executor_id,
            "run_id": run_id,
            "cleanup_handle": cleanup_handle,
            "experiment_name": _experiment_name(run_id, spec.fault_type),
            "target": target.summary(),
            "binding": TARGET_BINDING,
            "plan": plan,
            "started_at": _iso(started),
            "steps": [],
            "polls": [],
        }
        fence_before = _has_fence(target, target.uid)
        service: ControlledExecutionService | None = None
        create_attempted = False
        observed_running = False
        failure: str | None = None
        facts: dict[str, Any] = {}
        try:
            token = self.platform.issue_baseline(run_id, target)
            self._step(evidence, "baseline_capability", {"issued": True})
            service = self._executor_service(spec, run_id, token, cleanup_handle)
            request = {
                "run_id": run_id, "namespace": target.namespace, "target_name": target.name,
                "target_uid": target.uid, "fault_type": spec.fault_type,
                "duration_seconds": CANARY_DURATION_SECONDS, "intensity": dict(plan["intensity"]),
            }
            validation = await service.validate_plan(**request)
            self._step(evidence, "validate_plan", validation)
            if validation.get("ok") is not True:
                raise _CanaryStopped(f"{spec.server} validate_plan rejected the canary plan")
            create_attempted = True
            created = await service.create_experiment(
                **request,
                kubeconfig=service.config.kubeconfig or "",
                controller_token_ref=service.config.controller_token_ref or "",
                expected_controller_pod_uid=service.config.controller_pod_uid or "",
                baseline_gate_token=token,
                cleanup_handle=cleanup_handle,
            )
            self._step(evidence, "create_experiment", created)
            evidence["experiment_name"] = str(_mapping(created.get("created")).get("name") or evidence["experiment_name"])
            observed_running = await self._await_running(service, cleanup_handle, evidence["polls"])
            if not observed_running:
                failure = f"experiment was not observed applied and Running within {CANARY_RUNNING_TIMEOUT_SECONDS}s"
        except ChaosControlError as exc:
            failure = f"{exc.code}: {exc.message}"
            self._step(evidence, "error", exc.as_response())
        except _CanaryStopped as exc:
            failure = str(exc)
        except Exception as exc:  # noqa: BLE001 - every failure must still reach cleanup
            failure = f"{type(exc).__name__}: {exc}"
            self._step(evidence, "error", _exception_payload(exc))
        finally:
            # Destroy whenever create was attempted, whatever happened after it:
            # a failed or ambiguous create may still have left an object behind.
            if create_attempted and service is not None:
                facts = await self._destroy_and_verify(
                    service, target, run_id, cleanup_handle, str(evidence["experiment_name"]), fence_before, evidence,
                )
        destroy_verified = create_attempted and all(facts.get(name) is True for name in _DESTROY_FACTS)
        ok = observed_running and destroy_verified
        alert = None
        if create_attempted and facts.get("nothing_left_behind") is not True:
            alert = (
                f"{spec.server} experiment {evidence['experiment_name']} for {target.namespace}/{target.name} "
                f"(cleanup handle {cleanup_handle}) may remain; {spec.backstop}. Verify and delete it before "
                "the next Trial"
            )
        if not ok and failure is None:
            failure = "destroy could not be verified: " + ", ".join(
                name for name in _DESTROY_FACTS if facts.get(name) is not True
            )
        reason = "canary observed Running and then verified absent" if ok else str(failure)
        evidence.update({
            "create_verified": observed_running,
            "destroy_verified": destroy_verified,
            "verified_facts": facts,
            "accepted": ok,
            "reason": reason,
            "completed_at": _iso(self.clock()),
        })
        record_ref = self._write_evidence(f"{run_id}.json", evidence)
        if not ok:
            return ProbeOutcome("d8", spec.server, target, False, reason, record_ref=record_ref, alert=alert)
        entry = {
            "alternative_server": spec.server, "create_verified": True,
            "destroy_verified": True, "record_ref": record_ref,
        }
        D8CanaryEvidence.model_validate(entry)  # fail loudly if the record contract drifts
        return ProbeOutcome("d8", spec.server, target, True, reason, entry, record_ref)

    def _executor_service(
        self, spec: CanarySpec, run_id: str, baseline_token: str, cleanup_handle: str,
    ) -> ControlledExecutionService:
        """Configure the executor exactly as its MCP server is configured for a Trial."""

        environment = {
            **self.platform.mcp_environment,
            **_canary_environment(spec, run_id, baseline_token, cleanup_handle),
        }
        for key in _TRIAL_ONLY_CHAOS_ENV:
            environment.pop(key, None)
        config = ChaosRuntimeConfig.from_env(environment, server_name=spec.server)
        if spec.server == "chaos_control":
            return ChaosControlService(config, backend=self.platform.chaosblade_backend)
        return ChaosMeshControlService(config, backend=self.platform.chaos_mesh_backend)

    async def _await_running(
        self, service: ControlledExecutionService, cleanup_handle: str, polls: list[dict[str, Any]],
    ) -> bool:
        deadline = self.clock() + timedelta(seconds=CANARY_RUNNING_TIMEOUT_SECONDS)
        max_polls = int(CANARY_RUNNING_TIMEOUT_SECONDS / CANARY_POLL_INTERVAL_SECONDS) + 1
        for attempt in range(max_polls):
            status = await service.operation_status(
                operation_id=cleanup_handle, cleanup_handle=cleanup_handle, kubeconfig=service.config.kubeconfig,
            )
            live = _mapping(status.get("live"))
            polls.append({
                "at": _iso(self.clock()), "operation_outcome": status.get("operation_outcome"),
                "phase": live.get("phase"), "matches_ledger": live.get("matches_ledger"),
                "started_at": status.get("started_at"),
            })
            # "Running" is the platform's own applied fact for both executors
            # (the test _observe_fault_window uses): Chaos Mesh AllInjected on
            # the UID-fenced target, or the ChaosBlade operator's reported phase.
            if (
                status.get("operation_outcome") == "applied"
                and live.get("matches_ledger") is True
                and live.get("phase") == "Running"
            ):
                return True
            if attempt + 1 == max_polls or self.clock() >= deadline:
                break
            await self.sleep(CANARY_POLL_INTERVAL_SECONDS)
        return False

    async def _destroy_and_verify(
        self,
        service: ControlledExecutionService,
        target: TargetPod,
        run_id: str,
        cleanup_handle: str,
        experiment_name: str,
        fence_before: bool,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        """Destroy through the ledger, then verify absence and recovery independently."""

        facts: dict[str, Any] = {}
        ledger_exists = True
        try:
            destroyed = await service.destroy_experiment(
                cleanup_handle=cleanup_handle, kubeconfig=service.config.kubeconfig or "",
                principal="CONTROLLER_FALLBACK",
            )
            self._step(evidence, "destroy_experiment", destroyed)
            facts["destroy_verified_absent"] = destroyed.get("verified_absent") is True
        except ChaosControlError as exc:
            # Without a ledger, create stopped before any Kubernetes object existed.
            ledger_exists = exc.code != "UNKNOWN_CLEANUP_HANDLE"
            self._step(evidence, "destroy_experiment", exc.as_response())
            facts["destroy_verified_absent"] = False
        except Exception as exc:  # noqa: BLE001 - verification below still runs
            self._step(evidence, "destroy_experiment", _exception_payload(exc))
            facts["destroy_verified_absent"] = False
        facts["ledger_destroyed"] = facts["absent_for_executor"] = False if ledger_exists else None
        if ledger_exists:
            try:
                status = await service.recovery_status(cleanup_handle=cleanup_handle, kubeconfig=service.config.kubeconfig)
                self._step(evidence, "recovery_status", status)
                facts["ledger_destroyed"] = status.get("ledger_state") == "destroyed"
                facts["absent_for_executor"] = status.get("resource_absent") is True
            except Exception as exc:  # noqa: BLE001
                self._step(evidence, "recovery_status", _exception_payload(exc))
        finalizer = service.config.cleanup_kubeconfig or ""
        try:
            # Independent read with the finalizer identity, the one that deletes.
            # No object of this run may remain in any phase: the shared destroy
            # already requires absence, and the environment gate refuses a new
            # Trial while any ChaosBlade CR exists, even a Destroyed one.
            record = await service.backend.get_experiment(target.namespace, experiment_name, finalizer)
            facts["absent_for_finalizer"] = record is None
            left = sorted(
                item.name for item in await service.backend.list_experiments(finalizer, target.namespace)
                if item.run_id == run_id
            )
            facts["run_resources_left"] = left
            facts["no_run_resources_left"] = not left
        except Exception as exc:  # noqa: BLE001
            self._step(evidence, "finalizer_inventory", _exception_payload(exc))
            facts["absent_for_finalizer"] = facts["no_run_resources_left"] = False
        pod, read_error = self._read_pod_safely(target.name)
        if (
            not ledger_exists and not fence_before and facts.get("no_run_resources_left") is True
            and pod is not None and _has_fence(pod, target.uid)
        ):
            # Create stopped after installing the UID fence but before writing
            # its ledger: remove only the fence this canary added.
            try:
                await service.backend.clear_target_fence(target.namespace, target.name, target.uid, finalizer)
            except Exception as exc:  # noqa: BLE001
                self._step(evidence, "clear_target_fence", _exception_payload(exc))
            pod, read_error = self._read_pod_safely(target.name)
        fence_left = pod is not None and pod.uid == target.uid and _has_fence(pod, target.uid)
        facts["fence_label_absent"] = read_error is None and not fence_left
        facts["target_uid_unchanged"] = pod is not None and pod.uid == target.uid
        facts["target_ready"] = pod is not None and pod.ready
        facts["target_read_error"] = read_error
        facts["nothing_left_behind"] = (
            facts.get("absent_for_finalizer") is True
            and facts.get("no_run_resources_left") is True
            and (facts["fence_label_absent"] or fence_before)
        )
        return facts

    # Shared helpers.

    def _read_pod_safely(self, name: str) -> tuple[TargetPod | None, str | None]:
        try:
            return self.platform.read_pod(name), None
        except Exception as exc:  # noqa: BLE001 - an unreadable Pod is not a verified one
            return None, f"{type(exc).__name__}: {exc}"

    def _step(self, evidence: dict[str, Any], name: str, payload: Mapping[str, Any]) -> None:
        evidence["steps"].append({"step": name, "at": _iso(self.clock()), "result": dict(payload)})

    def _write_evidence(self, filename: str, payload: Mapping[str, Any]) -> str:
        _ensure_private_directory(self.evidence_dir.parent)
        _ensure_private_directory(self.evidence_dir)
        record_ref = RECORD_REF_PREFIX + filename
        _write_private_json(self.evidence_dir / filename, {
            "schema_version": EVIDENCE_SCHEMA, "record_ref": record_ref, "generator": GENERATOR, **payload,
        })
        return record_ref

    def _loader_check(self) -> tuple[bool, dict[str, bool]]:
        """Re-read the written file through the platform's own loader and prechecks."""

        factory = CapabilityLossRuntimeFactory(
            cleanup_backend=None, qualification_path=self.output_path,
            evidence_root=self.evidence_dir.parent, now=self.clock,
        )
        context = SimpleNamespace(target=SimpleNamespace(namespace=self.platform.namespace))
        qualification = factory._qualification(context)
        if qualification is None:
            return False, {}
        checks: dict[str, bool] = {}
        for sample in qualification.get("d7_historical_samples") or []:
            server, uid = str(sample.get("server")), str(sample.get("target_uid"))
            checks[f"d7:{server}:{uid}"] = factory._d7_precheck(qualification, server, uid).valid
        for canary in qualification.get("d8_canaries") or []:
            server = str(canary.get("alternative_server"))
            checks[f"d8:{server}"] = factory._d8_precheck(qualification, server).valid
        return True, checks


def merge_qualification(
    existing: Mapping[str, Any] | None,
    outcomes: Sequence[ProbeOutcome],
    *,
    live_uids: frozenset[str],
    namespace: str,
    issued_at: datetime,
    ttl: timedelta,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Combine this run's entries with still-valid ones from the previous file.

    Each entry keeps its own ``valid_until`` (in ``provenance``, because the
    record models forbid extra fields) and the scope never outlives any entry,
    so a D7-only refresh cannot extend an old canary.  Entries without probe
    provenance, bound to a UID that is no longer live, or contradicted by this
    run's probe of the same server and target are dropped.
    """

    previous = _mapping(existing)
    carried_provenance = _mapping(_mapping(previous.get("provenance")).get("entries"))
    d7_attempts = {
        (outcome.server, outcome.target.uid): outcome.ok
        for outcome in outcomes if outcome.kind == "d7" and outcome.target is not None
    }
    d8_attempts = {outcome.server: outcome.ok for outcome in outcomes if outcome.kind == "d8"}
    samples: list[dict[str, Any]] = []
    canaries: list[dict[str, Any]] = []
    provenance: dict[str, dict[str, Any]] = {}
    dropped: list[dict[str, Any]] = []

    def carried_meta(kind: str, record_ref: str) -> tuple[dict[str, Any], str | None]:
        meta = dict(_mapping(carried_provenance.get(record_ref)))
        valid_until = _time(meta.get("valid_until"))
        if meta.get("kind") != kind or valid_until is None:
            return meta, "unknown_provenance"
        return meta, ("expired" if valid_until <= issued_at else None)

    seen_samples: set[tuple[str, str]] = set()
    for raw in previous.get("d7_historical_samples") or []:
        try:
            sample = D7HistoricalSample.model_validate(raw)
        except Exception:  # noqa: BLE001 - an unreadable entry is simply not carried
            dropped.append({"kind": "d7_historical_sample", "record_ref": _mapping(raw).get("record_ref"), "reason": "invalid_entry"})
            continue
        key = (sample.server, sample.target_uid)
        meta, reason = carried_meta("d7_historical_sample", sample.record_ref)
        if reason is None and sample.target_uid not in live_uids:
            reason = "target_uid_not_live"
        if reason is None and key in d7_attempts:
            reason = "replaced_by_new_sample" if d7_attempts[key] else "latest_probe_failed"
        if reason is None and key in seen_samples:
            reason = "duplicate"
        if reason is not None:
            dropped.append({"kind": "d7_historical_sample", "record_ref": sample.record_ref, "reason": reason})
            continue
        seen_samples.add(key)
        samples.append(dict(raw))
        provenance[sample.record_ref] = meta

    seen_canaries: set[str] = set()
    for raw in previous.get("d8_canaries") or []:
        try:
            canary = D8CanaryEvidence.model_validate(raw)
        except Exception:  # noqa: BLE001
            dropped.append({"kind": "d8_canary", "record_ref": _mapping(raw).get("record_ref"), "reason": "invalid_entry"})
            continue
        meta, reason = carried_meta("d8_canary", canary.record_ref)
        if reason is None and not (canary.create_verified and canary.destroy_verified):
            reason = "incomplete_canary"
        if reason is None and canary.alternative_server in d8_attempts:
            reason = "replaced_by_new_canary" if d8_attempts[canary.alternative_server] else "latest_canary_failed"
        if reason is None and canary.alternative_server in seen_canaries:
            reason = "duplicate"
        if reason is not None:
            dropped.append({"kind": "d8_canary", "record_ref": canary.record_ref, "reason": reason})
            continue
        seen_canaries.add(canary.alternative_server)
        canaries.append(dict(raw))
        provenance[canary.record_ref] = meta

    valid_until = issued_at + ttl
    for outcome in outcomes:
        if not outcome.ok or outcome.entry is None:
            continue
        (samples if outcome.kind == "d7" else canaries).append(dict(outcome.entry))
        provenance[str(outcome.entry["record_ref"])] = {
            "kind": _PROVENANCE_KIND[outcome.kind],
            "server": outcome.server,
            "target_component": outcome.target.component if outcome.target else None,
            "target_uid": outcome.target.uid if outcome.target else None,
            "produced_at": _iso(issued_at),
            "valid_until": _iso(valid_until),
        }
    expires_at = min([valid_until, *(_time(meta.get("valid_until")) or valid_until for meta in provenance.values())])
    document = {
        "schema_version": QUALIFICATION_SCHEMA,
        "scope": {
            "application": APPLICATION,
            "namespace": namespace,
            "issued_at": _iso(issued_at),
            "expires_at": _iso(expires_at),
        },
        "d7_historical_samples": samples,
        "d8_canaries": canaries,
        "provenance": {"generator": GENERATOR, "entries": provenance},
    }
    return document, dropped


def _load_existing(path: Path, namespace: str) -> tuple[dict[str, Any] | None, str]:
    """Return a previous file only when the platform loader could have trusted it."""

    if not os.path.lexists(path):
        return None, "absent"
    if not _trusted_private_regular_file(path):
        return None, "ignored_untrusted_file"
    try:
        value = json.loads(_read_private_regular_file(path))
    except (OSError, ValueError):
        return None, "ignored_unreadable_file"
    if not isinstance(value, dict) or value.get("schema_version") != QUALIFICATION_SCHEMA:
        return None, "ignored_foreign_schema"
    scope = _mapping(value.get("scope"))
    if scope.get("application") != APPLICATION or scope.get("namespace") != namespace:
        return None, "ignored_other_scope"
    return value, "merged"


def _write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Atomically replace ``path`` with owner-only JSON (0600 file in a 0700 directory)."""

    parent = _ensure_private_directory(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False, default=str)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)
    with contextlib.suppress(OSError):
        directory = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _ensure_private_directory(path: Path) -> Path:
    """Create or tighten a Controller-owned 0700 directory; refuse anything else."""

    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ProbeError(f"{path} must be a real directory, not a symlink")
    if info.st_uid != os.getuid():
        raise ProbeError(f"{path} is not owned by the Controller user")
    if stat.S_IMODE(info.st_mode) & 0o077:
        os.chmod(path, 0o700)
    return path


@contextlib.contextmanager
def _exclusive_file_lock(path: Path) -> Iterator[None]:
    """Serialise read-merge-write so concurrent probes cannot drop each other's entries."""

    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path.with_name(f".{path.name}.lock"), flags, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@contextlib.contextmanager
def _environ_overlay(values: Mapping[str, str]) -> Iterator[None]:
    """Expose an MCP server environment to ``from_env`` parsers, then restore."""

    previous = {key: os.environ.get(key) for key in values}
    os.environ.update({key: str(value) for key, value in values.items()})
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


async def _guarded_read(
    operation: Awaitable[dict[str, Any]],
    known_errors: tuple[type[Exception], ...],
    envelope: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    """Return the tool's own error envelope instead of raising, as its MCP server does."""

    try:
        return await operation
    except known_errors as exc:
        return envelope(exc)
    except Exception as exc:  # noqa: BLE001 - transport failures are probe failures
        return {"ok": False, "error": _exception_payload(exc)}


def _matching_series(
    series: Any,
    is_target: Callable[[Mapping[str, Any]], bool],
    *,
    not_before: float | None,
    not_after: float,
) -> tuple[list[dict[str, Any]], float | None]:
    """Summarise returned series whose own labels name the target and carry in-window samples.

    Query arguments only describe the request; like the D7 Oracle, only the
    server-returned labels can prove which Pod the data belongs to.  Samples
    before the Pod's creation are ignored so a reused name cannot stand in
    for this UID.
    """

    matches: list[dict[str, Any]] = []
    latest: float | None = None
    for item in series if isinstance(series, list) else []:
        labels = _mapping(item.get("metric")) if isinstance(item, Mapping) else {}
        if not labels or not is_target(labels):
            continue
        timestamps = [
            timestamp for timestamp in _finite_sample_times(item.get("values"))
            if timestamp <= not_after and (not_before is None or timestamp >= not_before)
        ]
        if not timestamps:
            continue
        latest = _later(latest, max(timestamps))
        matches.append({
            "labels": labels,
            "samples_in_window": len(timestamps),
            "first_sample_at": _iso(datetime.fromtimestamp(min(timestamps), UTC)),
            "last_sample_at": _iso(datetime.fromtimestamp(max(timestamps), UTC)),
        })
    return matches, latest


def _finite_sample_times(values: Any) -> list[float]:
    """Timestamps of samples whose value is a finite number (Coroot gaps are null)."""

    times: list[float] = []
    for point in values if isinstance(values, list) else []:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            continue
        timestamp, value = _finite(point[0]), _finite(point[1])
        if timestamp is not None and value is not None:
            times.append(timestamp)
    return times


def _response_summary(response: Mapping[str, Any], series: Any, matches: list[dict[str, Any]]) -> dict[str, Any]:
    items = series if isinstance(series, list) else []
    return {
        "ok": response.get("ok") is True,
        "error": response.get("error"),
        "series_returned": len(items),
        "returned_labels": [_mapping(item.get("metric")) for item in items[:20] if isinstance(item, Mapping)],
        "matching_series": matches,
        "truncated": bool(response.get("truncated") or _mapping(response.get("data")).get("truncated")),
    }


def _coroot_promql(namespace: str, labels: Mapping[str, str]) -> str | None:
    try:
        return coroot_metric_query(metric=COROOT_METRIC, namespace=namespace, labels=labels)
    except CorootROError:
        return None


def _canary_environment(spec: CanarySpec, run_id: str, baseline_token: str, cleanup_handle: str) -> dict[str, str]:
    """The per-Trial keys harness_runtime adds for a chaos MCP server, set for one canary."""

    return {
        "RESBENCH_AUTHORIZED_RUN_ID": run_id,
        "RESBENCH_BASELINE_GATE_TOKEN": baseline_token,
        "RESBENCH_CLEANUP_HANDLE": cleanup_handle,
        # Capability and contract narrowed to the one canary fault, as for an
        # explicit-contract Trial.  Every parameter is fixed by the
        # Controller, so there is no user decision left to ask for.
        "RESBENCH_CHAOS_ALLOWED_FAULT_TYPES": spec.fault_type,
        "RESBENCH_CHAOS_EXPECTED_FAULT_JSON": json.dumps(spec.plan(), separators=(",", ":"), sort_keys=True),
        "RESBENCH_DECISION_POLICY": "agent_delegated",
    }


def _target_pod(pod: Any, *, namespace: str, component: str) -> TargetPod:
    metadata = pod.metadata
    created = metadata.creation_timestamp
    if isinstance(created, datetime) and created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return TargetPod(
        namespace=namespace,
        component=component,
        name=str(metadata.name),
        uid=str(metadata.uid or ""),
        created_at=created if isinstance(created, datetime) else None,
        containers=tuple(str(container.name) for container in (getattr(pod.spec, "containers", None) or [])),
        labels=dict(metadata.labels or {}),
        ready=_ready(pod),
    )


def _has_fence(pod: TargetPod, uid: str) -> bool:
    return pod.labels.get(UID_FENCE_LABEL) == _fence_value(uid)


def _target_label(target: TargetPod | None) -> str | None:
    return f"{target.component}/{target.name} ({target.uid})" if target else None


def _exception_payload(exc: BaseException) -> dict[str, str]:
    return {"code": type(exc).__name__, "message": str(exc)[:500]}


def _error_text(error: Mapping[str, Any]) -> str:
    detail = _mapping(error.get("error")) or _mapping(error)
    return f"{detail.get('code')}: {detail.get('message')}"


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _later(first: float | None, second: float | None) -> float | None:
    if first is None:
        return second
    return first if second is None else max(first, second)


def _timestamp(value: datetime | None) -> float | None:
    return value.timestamp() if value else None


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _stamp(value: datetime) -> str:
    return f"{value.astimezone(UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"


def default_private_root() -> Path:
    """``Stage2RuntimeConfig.private_root`` without loading the whole runtime configuration."""

    return Path(os.environ.get(PRIVATE_ROOT_ENV, DEFAULT_PRIVATE_ROOT)).resolve()


def default_output_path(private_root: Path) -> Path:
    """The path ``runtime_factory`` hands to ``CapabilityLossRuntimeFactory``."""

    return Path(os.environ.get(OUTPUT_ENV, str(private_root / OUTPUT_FILENAME))).absolute()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m stage2_service.capability_loss.qualification_probe",
        description=(
            "Refresh the private D7/D8 capability-loss qualification file from real probes. "
            "Without --d7/--d8 both run. Runs only while no Stage-2 campaign is active."
        ),
    )
    parser.add_argument("--d7", action="store_true", help="refresh D7 historical samples for coroot_ro and telemetry_ro")
    parser.add_argument("--d8", action="store_true", help="run the D8 canary of every alternative executor")
    parser.add_argument(
        "--d8-server", action="append", choices=D8_SERVERS, metavar="SERVER",
        help=f"run only this D8 canary ({' or '.join(D8_SERVERS)}); repeatable; implies --d8",
    )
    parser.add_argument(
        "--namespace",
        default=current_target_binding().application_namespace,
        choices=(current_target_binding().application_namespace,),
        help="application namespace",
    )
    parser.add_argument(
        "--target", action="append", metavar="COMPONENT",
        help=f"logical target component, bound like a Trial target; repeatable (default: {DEFAULT_TARGET})",
    )
    parser.add_argument("--canary-target", metavar="COMPONENT", help="component the D8 canary targets (default: first --target)")
    parser.add_argument("--ttl-hours", type=float, default=24.0, help="validity of the entries written now (default: 24)")
    parser.add_argument(
        "--output", type=Path,
        help=f"qualification file (default: ${OUTPUT_ENV} or $STAGE2_PRIVATE_ROOT/{OUTPUT_FILENAME}, as the platform reads it)",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the plan; touch nothing")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    platform_factory: Callable[[str], ProbePlatform] | None = None,
    runtime_lock: RuntimeLock | None = None,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> int:
    """CLI entry point; prints exactly one JSON summary line on stdout."""

    parser = build_parser()
    options = parser.parse_args(argv)
    if not 0 < options.ttl_hours <= MAX_TTL_HOURS:
        parser.error(f"--ttl-hours must be greater than 0 and at most {MAX_TTL_HOURS}")
    explicit = options.d7 or options.d8 or bool(options.d8_server)
    run_d7 = options.d7 or not explicit
    # D8 needs both canaries by default: an Agent may validate first on either executor.
    d8_servers = tuple(dict.fromkeys(options.d8_server or (D8_SERVERS if options.d8 or not explicit else ())))
    targets = list(dict.fromkeys(options.target or [DEFAULT_TARGET]))
    canary_target = options.canary_target or targets[0]
    lock = runtime_lock or RuntimeLock.from_environment()
    if options.dry_run:
        _emit(_dry_run_plan(options, run_d7, d8_servers, targets, canary_target, lock))
        return 0
    try:
        # The runtime lock is the one every campaign and qualification run
        # takes: the probe refuses to start while a campaign is active and no
        # campaign can start while the canary's fault may exist.
        with lock.acquire(owner=f"capability-loss-qualification:{os.getpid()}"):
            platform = (platform_factory or platform_from_stage2_runtime)(options.namespace)
            output = (options.output or default_output_path(platform.private_root)).absolute()
            summary = QualificationProbe(
                platform=platform, targets=targets, canary_target=canary_target, output_path=output,
                ttl=timedelta(hours=options.ttl_hours), clock=clock or (lambda: datetime.now(UTC)),
                sleep=sleep or asyncio.sleep,
            ).run(d7=run_d7, d8_servers=d8_servers)
    except RuntimeLockError as exc:
        return _abort(f"{exc}; the qualification probe runs only while no Stage-2 campaign is active")
    except Exception as exc:  # noqa: BLE001 - one clear line instead of a traceback
        return _abort(f"qualification probe aborted: {type(exc).__name__}: {exc}")
    for failure in summary["failures"]:
        print(
            f"qualification probe failed: {failure['probe']} {failure['target']}: {failure['reason']} "
            f"(evidence {failure['evidence']})",
            file=sys.stderr,
        )
    for alert in summary["alerts"]:
        print(f"CLEANUP NOT VERIFIED: {alert}", file=sys.stderr)
    if not summary["loader_accepted"]:
        print("the platform loader rejected the written qualification file", file=sys.stderr)
    _emit(summary)
    return 0 if summary["ok"] else 1


def _dry_run_plan(
    options: argparse.Namespace, run_d7: bool, d8_servers: tuple[str, ...], targets: list[str],
    canary_target: str, lock: RuntimeLock,
) -> dict[str, Any]:
    private_root = default_private_root()
    namespace = options.namespace
    return {
        "ok": True,
        "dry_run": True,
        "actions_performed": "none",
        "modes": [mode for mode, selected in (("d7", run_d7), ("d8", bool(d8_servers))) if selected],
        "namespace": namespace,
        "output": str((options.output or default_output_path(private_root)).absolute()),
        "evidence_dir": str(private_root / EVIDENCE_RELATIVE_DIR),
        "ttl_hours": options.ttl_hours,
        "runtime_lock": str(lock.path),
        "target_binding": TARGET_BINDING,
        "d7_reads": [
            read for component in targets for read in (
                {"target": component, "server": COROOT_SERVER, "tool": "coroot_metrics_range", "metric": COROOT_METRIC,
                 "labels": {"container_id": f"/k8s/{namespace}/<pod>/<container>"}, "lookback_seconds": D7_LOOKBACK_SECONDS},
                {"target": component, "server": TELEMETRY_SERVER, "tool": "telemetry_prom_metric_range",
                 "metric": TELEMETRY_METRIC, "labels": {"pod": "<pod>"}, "step_seconds": TELEMETRY_STEP_SECONDS,
                 "lookback_seconds": D7_LOOKBACK_SECONDS},
            )
        ] if run_d7 else [],
        "d8_canaries": [
            {
                "server": spec.server, "executor_id": spec.executor_id, "target": canary_target,
                **spec.plan(), "running_timeout_seconds": CANARY_RUNNING_TIMEOUT_SECONDS,
                "destroy": "always in finally; absence verified with the finalizer identity",
                "if_the_probe_dies": spec.backstop,
            }
            for spec in (CANARIES[server] for server in d8_servers)
        ],
    }


def _abort(message: str) -> int:
    print(message, file=sys.stderr)
    _emit({"ok": False, "dry_run": False, "error": message})
    return 1


def _emit(payload: Mapping[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str))


if __name__ == "__main__":
    raise SystemExit(main())
