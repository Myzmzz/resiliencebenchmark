"""Normalize trusted repository facts and read-only runtime inventory.

This module is the boundary between provider-specific files/API responses and
the versioned snapshot consumed by matchers and Episode qualification.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, Protocol
from urllib.parse import quote

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .run_contracts import RunMode, RunSpec


class SnapshotError(RuntimeError):
    pass


class RuntimeScanError(SnapshotError):
    pass


class ObservationScanError(SnapshotError):
    pass


class SnapshotStatus(str, Enum):
    QUALIFIED = "qualified"
    UNQUALIFIED = "unqualified"
    NOT_REQUESTED = "not_requested"
    UNAVAILABLE = "unavailable"


class SnapshotModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceRootRef(SnapshotModel):
    alias: str
    repository_ref: str


class SourceSnapshot(SnapshotModel):
    lock_id: str
    commit: str
    archive_sha256: str
    materialized: bool
    verification_artifact_matched: bool
    runtime_mapping_status: str
    status: SnapshotStatus


class WorkloadSnapshot(SnapshotModel):
    profile_ref: str
    random_seed: int
    duration_seconds: int
    evaluation_window_seconds: int
    minimum_success_rate: float
    maximum_error_rate: float
    maximum_p95_latency_ms: float
    baseline_throughput_rps: float | None = None
    minimum_throughput_rps: float | None = None
    calibration_status: str


class RuntimeTargetSnapshot(SnapshotModel):
    kind: str
    name: str
    uid: str
    ready: bool
    component: str | None = None


class RuntimeSnapshot(SnapshotModel):
    status: SnapshotStatus
    observed_at: datetime | None = None
    nodes_total: int = 0
    nodes_ready: int = 0
    controllers_desired: int = 0
    controllers_ready: int = 0
    pods_total: int = 0
    pods_ready: int = 0
    active_application: str | None = None
    chaosblade_global_count: int | None = None
    targets: list[RuntimeTargetSnapshot] = Field(default_factory=list)
    error: str | None = None


class ObserverSnapshot(SnapshotModel):
    status: SnapshotStatus = SnapshotStatus.NOT_REQUESTED
    observed_at: datetime | None = None
    prometheus_series: int = 0
    jaeger_trace_count: int = 0
    jaeger_services: list[str] = Field(default_factory=list)
    loki_streams: int = 0
    error: str | None = None


class SystemSnapshot(SnapshotModel):
    schema_version: str = "system-snapshot.v1"
    snapshot_id: str
    run_id: str
    application: str
    namespace: str
    captured_at: datetime
    config_fingerprint_sha256: str
    source: SourceSnapshot
    workload: WorkloadSnapshot
    runtime: RuntimeSnapshot
    observers: ObserverSnapshot = Field(default_factory=ObserverSnapshot)
    evidence_roots: list[EvidenceRootRef]
    source_files: list[str]
    limitations: list[str]


class RuntimeInventoryAdapter(Protocol):
    def scan(self, namespace: str) -> RuntimeSnapshot: ...


class ObservationInventoryAdapter(Protocol):
    def scan(self, namespace: str) -> ObserverSnapshot: ...


class CommandRunner(Protocol):
    def __call__(self, argv: list[str], timeout_seconds: int) -> str: ...


def subprocess_runner(argv: list[str], timeout_seconds: int) -> str:
    try:
        result = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeScanError("read-only kubectl scan timed out") from exc
    if result.returncode != 0:
        message = " ".join((result.stderr or result.stdout).split())[:500]
        message = re.sub(
            r"(?i)(token|password|secret|api[_-]?key)\s*[:=]\s*\S+",
            r"\1=<redacted>",
            message,
        )
        raise RuntimeScanError(f"read-only kubectl scan failed: {message or 'no output'}")
    return result.stdout


class KubectlReadOnlyAdapter:
    def __init__(self, kubeconfig: Path, *, runner: CommandRunner = subprocess_runner):
        self.kubeconfig = kubeconfig.expanduser().resolve()
        if not self.kubeconfig.is_file():
            raise RuntimeScanError("configured kubeconfig does not exist")
        self.runner = runner

    def _get(self, *arguments: str) -> dict[str, Any]:
        raw = self.runner(
            ["kubectl", "--kubeconfig", str(self.kubeconfig), "get", *arguments, "-o", "json"],
            60,
        )
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeScanError("kubectl returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise RuntimeScanError("kubectl returned a non-object JSON payload")
        return value

    def scan(self, namespace: str) -> RuntimeSnapshot:
        nodes = self._get("nodes")
        workloads = self._get("deployment,statefulset,pod", "-n", namespace)
        try:
            marker = self._get("configmap", "resbench-active-system", "-n", namespace)
        except RuntimeScanError:
            marker = {}
        chaos = self._get("chaosblades.chaosblade.io")
        observed_at = datetime.now(UTC)

        node_items = _items(nodes)
        workload_items = _items(workloads)
        targets: list[RuntimeTargetSnapshot] = []
        controllers_desired = 0
        controllers_ready = 0
        pods_total = 0
        pods_ready = 0
        for item in workload_items:
            kind = str(item.get("kind", ""))
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            labels = metadata.get("labels") if isinstance(metadata.get("labels"), dict) else {}
            spec = item.get("spec") if isinstance(item.get("spec"), dict) else {}
            status = item.get("status") if isinstance(item.get("status"), dict) else {}
            if kind in {"Deployment", "StatefulSet"}:
                desired = int(spec.get("replicas") or 0)
                ready = int(status.get("readyReplicas") or 0)
                controllers_desired += desired
                controllers_ready += ready
            elif kind == "Pod":
                pods_total += 1
                ready = _pod_ready(item)
                pods_ready += int(ready)
                targets.append(
                    RuntimeTargetSnapshot(
                        kind="Pod",
                        name=str(metadata.get("name", "")),
                        uid=str(metadata.get("uid", "")),
                        ready=ready,
                        component=(
                            str(
                                labels.get("app.kubernetes.io/component")
                                or labels.get("opentelemetry.io/name")
                                or ""
                            )
                            or None
                        ),
                    )
                )

        active_application = None
        data = marker.get("data") if isinstance(marker.get("data"), dict) else {}
        for key in ("application", "active-system", "name"):
            if isinstance(data.get(key), str) and data[key]:
                active_application = data[key]
                break
        nodes_ready = sum(1 for node in node_items if _condition_true(node, "Ready"))
        qualified = (
            bool(node_items)
            and nodes_ready == len(node_items)
            and controllers_desired > 0
            and controllers_ready == controllers_desired
            and pods_total > 0
            and pods_ready == pods_total
            and len(_items(chaos)) == 0
        )
        return RuntimeSnapshot(
            status=SnapshotStatus.QUALIFIED if qualified else SnapshotStatus.UNQUALIFIED,
            observed_at=observed_at,
            nodes_total=len(node_items),
            nodes_ready=nodes_ready,
            controllers_desired=controllers_desired,
            controllers_ready=controllers_ready,
            pods_total=pods_total,
            pods_ready=pods_ready,
            active_application=active_application,
            chaosblade_global_count=len(_items(chaos)),
            targets=targets,
        )


class KubectlObservationAdapter:
    """Query fixed observability APIs through Kubernetes Service proxy GETs."""

    def __init__(self, kubeconfig: Path, *, runner: CommandRunner = subprocess_runner):
        self.kubeconfig = kubeconfig.expanduser().resolve()
        if not self.kubeconfig.is_file():
            raise ObservationScanError("configured kubeconfig does not exist")
        self.runner = runner

    def _raw(self, path: str) -> dict[str, Any]:
        try:
            raw = self.runner(
                [
                    "kubectl",
                    "--kubeconfig",
                    str(self.kubeconfig),
                    "get",
                    "--raw",
                    path,
                ],
                60,
            )
            value = json.loads(raw)
        except (RuntimeScanError, json.JSONDecodeError) as exc:
            raise ObservationScanError("observability service proxy query failed") from exc
        if not isinstance(value, dict):
            raise ObservationScanError("observability API returned a non-object payload")
        return value

    def scan(self, namespace: str) -> ObserverSnapshot:
        if not re.fullmatch(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", namespace):
            raise ObservationScanError("invalid observation namespace")
        now = datetime.now(UTC)
        start_seconds = int(now.timestamp()) - 3600
        end_seconds = int(now.timestamp())
        promql = quote(f'count({{namespace="{namespace}"}})', safe="")
        prometheus = self._raw(
            "/api/v1/namespaces/observability/services/http:prometheus:9090/proxy"
            f"/api/v1/query?query={promql}"
        )
        result = _dig(prometheus, "data", "result")
        prometheus_series = 0
        if isinstance(result, list) and result:
            value = result[0].get("value") if isinstance(result[0], dict) else None
            if isinstance(value, list) and len(value) >= 2:
                prometheus_series = int(float(value[1]))

        service_payload = self._raw(
            "/api/v1/namespaces/observability/services/http:jaeger-query:16686/proxy/api/services"
        )
        all_services = service_payload.get("data", [])
        expected_services = ["frontend", "frontend-proxy", "checkout", "cart"]
        jaeger_services = [
            item for item in expected_services if isinstance(all_services, list) and item in all_services
        ]
        traces = self._raw(
            "/api/v1/namespaces/observability/services/http:jaeger-query:16686/proxy"
            f"/api/traces?service=frontend&start={start_seconds * 1_000_000}"
            f"&end={end_seconds * 1_000_000}&limit=1"
        )
        trace_data = traces.get("data", [])
        trace_count = len(trace_data) if isinstance(trace_data, list) else 0

        selector = quote(f'{{namespace="{namespace}"}}', safe="")
        loki = self._raw(
            "/api/v1/namespaces/observability/services/http:loki:3100/proxy"
            f"/loki/api/v1/series?match%5B%5D={selector}"
            f"&start={start_seconds * 1_000_000_000}&end={end_seconds * 1_000_000_000}"
        )
        streams = loki.get("data", [])
        loki_streams = len(streams) if isinstance(streams, list) else 0
        qualified = (
            prometheus_series > 0
            and trace_count > 0
            and len(jaeger_services) >= 2
            and loki_streams > 0
        )
        return ObserverSnapshot(
            status=SnapshotStatus.QUALIFIED if qualified else SnapshotStatus.UNQUALIFIED,
            observed_at=now,
            prometheus_series=prometheus_series,
            jaeger_trace_count=trace_count,
            jaeger_services=jaeger_services,
            loki_streams=loki_streams,
        )


class SystemScanner:
    EVIDENCE_ROOTS: ClassVar[dict[str, str]] = {
        "benchmark-app": "environment/applications",
        "benchmark-config": "environment/kubernetes/{application}",
        "benchmark-workload": "environment/workloads/{application}",
    }

    def __init__(self, repo_root: Path, source_root: Path):
        self.repo_root = repo_root.resolve()
        self.source_root = source_root.resolve()
        if not (self.repo_root / "benchmarkfactory.yaml").is_file():
            raise SnapshotError("repo_root is not a ResilienceBenchmark repository")

    def scan(
        self,
        run_id: str,
        spec: RunSpec,
        *,
        runtime_adapter: RuntimeInventoryAdapter | None = None,
        observation_adapter: ObservationInventoryAdapter | None = None,
    ) -> SystemSnapshot:
        application = spec.scan.application
        namespace = spec.scan.namespace
        app_ref = f"environment/applications/{application}.yaml"
        lock_ref = "environment/shared/source-locks.yaml"
        profiles_ref = "environment/workloads/deterministic-profiles.yaml"
        baseline_ref = "environment/workloads/calibrated-baselines.yaml"
        fixture_ref = f"environment/workloads/{application}/runtime-fixture.example.yaml"
        source_files = [app_ref, lock_ref, profiles_ref, baseline_ref, fixture_ref]
        documents = {ref: self._load_yaml(ref) for ref in source_files}

        application_doc = documents[app_ref]
        live_reference = _dig(application_doc, "spec", "namespace", "liveReference")
        if live_reference != namespace:
            raise SnapshotError(
                f"RunSpec namespace {namespace} does not match trusted application liveReference"
            )
        locks = _dig(documents[lock_ref], "spec", "locks")
        lock = next(
            (
                item
                for item in locks
                if isinstance(item, dict) and item.get("id") == spec.scan.source_lock_id
            ),
            None,
        )
        if lock is None or lock.get("application") != application:
            raise SnapshotError("source_lock_id is not registered for the requested application")
        profile = _find_application(documents[profiles_ref], application)
        baseline = _find_application(documents[baseline_ref], application)
        defaults = _dig(documents[profiles_ref], "spec", "defaults")

        materialized = self.source_root / spec.scan.source_lock_id
        verification_ref = f"artifacts/source-verification-{application}.json"
        verification_matched = self._verification_matches(verification_ref, lock)
        source_status = (
            SnapshotStatus.QUALIFIED
            if materialized.is_dir() and verification_matched
            else SnapshotStatus.UNQUALIFIED
        )
        source = SourceSnapshot(
            lock_id=str(lock["id"]),
            commit=str(lock["commit"]),
            archive_sha256=str(lock["archiveSha256"]),
            materialized=materialized.is_dir(),
            verification_artifact_matched=verification_matched,
            runtime_mapping_status=str(lock.get("runtimeMappingStatus", "unknown")),
            status=source_status,
        )
        entry_slo = profile["entrySlo"]
        workload = WorkloadSnapshot(
            profile_ref=str(profile["executor"]["profileRef"]),
            random_seed=int(profile["determinism"]["randomSeed"]),
            duration_seconds=int(defaults["durationSeconds"]),
            evaluation_window_seconds=int(defaults["evaluationWindowSeconds"]),
            minimum_success_rate=float(entry_slo["minimumSuccessRate"]),
            maximum_error_rate=float(entry_slo["maximumErrorRate"]),
            maximum_p95_latency_ms=float(entry_slo["p95LatencyMs"]),
            baseline_throughput_rps=_optional_float(baseline.get("baselineThroughputRps")),
            minimum_throughput_rps=_optional_float(baseline.get("minimumThroughputRps")),
            calibration_status=str(baseline.get("status", "unknown")),
        )

        limitations: list[str] = []
        if spec.mode is RunMode.DRY_RUN or not spec.scan.include_live_runtime:
            runtime = RuntimeSnapshot(status=SnapshotStatus.NOT_REQUESTED)
            limitations.append("Live Kubernetes and telemetry state was not requested for this dry run.")
        elif runtime_adapter is None:
            runtime = RuntimeSnapshot(
                status=SnapshotStatus.UNAVAILABLE,
                error="no trusted runtime adapter configured",
            )
            limitations.append("Live runtime inventory is unavailable; execution qualification must block.")
        else:
            try:
                runtime = runtime_adapter.scan(namespace)
            except RuntimeScanError as exc:
                runtime = RuntimeSnapshot(status=SnapshotStatus.UNAVAILABLE, error=str(exc))
                limitations.append("Live runtime inventory failed; execution qualification must block.")

        if spec.mode is RunMode.DRY_RUN or not spec.scan.include_live_runtime:
            observers = ObserverSnapshot(status=SnapshotStatus.NOT_REQUESTED)
        elif observation_adapter is None:
            observers = ObserverSnapshot(
                status=SnapshotStatus.UNAVAILABLE,
                error="no trusted observation adapter configured",
            )
            limitations.append(
                "Independent metric, trace, and log channels are unavailable; execution must block."
            )
        else:
            try:
                observers = observation_adapter.scan(namespace)
            except ObservationScanError as exc:
                observers = ObserverSnapshot(status=SnapshotStatus.UNAVAILABLE, error=str(exc))
                limitations.append("Independent observation qualification failed; execution must block.")

        evidence_roots = []
        for alias in spec.scan.evidence_roots:
            template = self.EVIDENCE_ROOTS.get(alias)
            if template is None:
                raise SnapshotError(f"evidence root alias is not registered: {alias}")
            evidence_roots.append(
                EvidenceRootRef(
                    alias=alias,
                    repository_ref=template.format(application=application),
                )
            )
        fingerprint = _fingerprint(self.repo_root, source_files)
        identity_material = {
            "application": application,
            "namespace": namespace,
            "config": fingerprint,
            "source_commit": source.commit,
            "runtime": runtime.model_dump(mode="json"),
            "observers": observers.model_dump(mode="json"),
        }
        snapshot_id = "snap-" + hashlib.sha256(
            json.dumps(identity_material, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:20]
        return SystemSnapshot(
            snapshot_id=snapshot_id,
            run_id=run_id,
            application=application,
            namespace=namespace,
            captured_at=datetime.now(UTC),
            config_fingerprint_sha256=fingerprint,
            source=source,
            workload=workload,
            runtime=runtime,
            observers=observers,
            evidence_roots=evidence_roots,
            source_files=[*source_files, verification_ref],
            limitations=limitations,
        )

    def _load_yaml(self, relative: str) -> dict[str, Any]:
        path = (self.repo_root / relative).resolve()
        try:
            path.relative_to(self.repo_root)
        except ValueError as exc:
            raise SnapshotError("trusted repository reference escaped repo_root") from exc
        if not path.is_file():
            raise SnapshotError(f"required scan input is missing: {relative}")
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise SnapshotError(f"scan input is not a YAML object: {relative}")
        return value

    def _verification_matches(self, relative: str, lock: dict[str, Any]) -> bool:
        path = self.repo_root / relative
        if not path.is_file():
            return False
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            sources = _dig(manifest, "spec", "sources")
        except (json.JSONDecodeError, KeyError, TypeError):
            return False
        return any(
            isinstance(item, dict)
            and item.get("id") == lock.get("id")
            and item.get("commit") == lock.get("commit")
            and item.get("archiveSha256") == lock.get("archiveSha256")
            for item in sources
        )


def _items(document: dict[str, Any]) -> list[dict[str, Any]]:
    items = document.get("items", [])
    if not isinstance(items, list):
        raise RuntimeScanError("Kubernetes list payload has invalid items")
    return [item for item in items if isinstance(item, dict)]


def _condition_true(resource: dict[str, Any], condition_type: str) -> bool:
    status = resource.get("status") if isinstance(resource.get("status"), dict) else {}
    conditions = status.get("conditions") if isinstance(status.get("conditions"), list) else []
    return any(
        isinstance(item, dict)
        and item.get("type") == condition_type
        and item.get("status") == "True"
        for item in conditions
    )


def _pod_ready(resource: dict[str, Any]) -> bool:
    return _condition_true(resource, "Ready")


def _dig(value: dict[str, Any], *keys: str) -> Any:
    current: Any = value
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            raise SnapshotError("missing required scan field: " + ".".join(keys))
        current = current[key]
    return current


def _find_application(document: dict[str, Any], application: str) -> dict[str, Any]:
    items = _dig(document, "spec", "applications")
    matches = [
        item for item in items if isinstance(item, dict) and item.get("id") == application
    ]
    if len(matches) != 1:
        raise SnapshotError(f"expected one workload entry for {application}")
    return matches[0]


def _fingerprint(root: Path, relative_files: list[str]) -> str:
    digest = hashlib.sha256()
    for relative in sorted(relative_files):
        payload = (root / relative).read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(payload).digest())
    return digest.hexdigest()


def _optional_float(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None
