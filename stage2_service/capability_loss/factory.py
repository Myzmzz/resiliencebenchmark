"""Controller-private construction of D7/D8 runtime evidence adapters."""

from __future__ import annotations

import json
import math
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from stage2_service.capability_policy import CapabilityPolicyRegistry
from stage2_service.platform_ledger import PlatformLedger

from .precheck import CapabilityLossPrecheck, PrecheckResult
from .records import CapabilityLossCase, CapabilityLossVariant, D7HistoricalSample, D8CanaryEvidence, FaultRunningWindow
from .runtime import CapabilityLossRuntime, _is_observation, _server_tool


QUALIFICATION_SCHEMA = "stage2-capability-loss-qualification.v1"


class CapabilityLossRuntimeFactory:
    """Build D7/D8 adapters from Controller-only qualification and Oracle data."""

    def __init__(
        self,
        *,
        cleanup_backend: Any,
        qualification_path: Path,
        evidence_root: Path,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.cleanup_backend = cleanup_backend
        # ``resolve`` would erase precisely the symlink evidence that this
        # Controller-private input must reject.  Keep the final pathname intact
        # and validate the final file and immediate private parent below.
        self.qualification_path = Path(qualification_path).absolute()
        self.evidence_root = Path(evidence_root).resolve()
        self.evidence_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.evidence_root, 0o700)
        self.now = now or (lambda: datetime.now(UTC))

    def build(
        self,
        trial_id: str,
        case_id: str | CapabilityLossCase,
        variant: str | CapabilityLossVariant,
        runtime_context,
        policy_registry: CapabilityPolicyRegistry,
        platform_ledger: PlatformLedger,
    ) -> CapabilityLossRuntime:
        case = CapabilityLossCase(str(getattr(case_id, "value", case_id)))
        selected_variant = CapabilityLossVariant(str(getattr(variant, "value", variant)))
        qualification = self._qualification(runtime_context)
        from .orchestrator import CapabilityLossOrchestrator

        orchestrator = CapabilityLossOrchestrator(
            root=self.evidence_root / "state", policy_registry=policy_registry,
            ledger=platform_ledger,
        )
        return CapabilityLossRuntime(
            trial_id=trial_id, case=case, variant=selected_variant,
            orchestrator=orchestrator, target_uid=runtime_context.target.uid,
            oracle_target_uid=lambda: self._target_uid(runtime_context),
            d7_precheck=lambda _primary, alternative, target_uid: self._d7_precheck(
                qualification, alternative, target_uid
            ),
            d8_precheck=lambda alternative: self._d8_precheck(qualification, alternative),
            oracle_fault_window=lambda: self._fault_window(runtime_context),
        )

    def finish_inputs(self, runtime: CapabilityLossRuntime, runtime_context, finalization: Any) -> dict[str, Mapping[str, Any]]:
        """Build only independent Oracle/finalizer inputs for ``runtime.finish``."""
        recovery = _mapping(finalization)
        window = self._fault_window(runtime_context)
        if runtime.case is CapabilityLossCase.D7:
            covered, refs = self._d7_alternative_coverage(runtime, runtime_context, window)
            oracle = {
                "fault_window": window.model_dump(mode="json") if window else None,
                "evidence_covers_fault_window": covered,
                "effect_verified": recovery.get("fault_effect_verified") if isinstance(recovery.get("fault_effect_verified"), bool) else None,
                "evidence_refs": tuple(dict.fromkeys((*refs, *tuple(recovery.get("evidence_refs") or ())))),
            }
        else:
            evidence = _mapping(recovery.get("fault_effect_evidence"))
            oracle = {
                "target_uid": self._target_uid(runtime_context),
                "parameters_within_envelope": evidence.get("parameters_within_envelope") is True,
                "fault_running_verified": recovery.get("main_fault_ever_active") is True and recovery.get("main_fault_target_verified") is True,
                "evidence_refs": tuple(recovery.get("evidence_refs") or ()),
            }
        return {"oracle": oracle, "finalizer": recovery}

    def _qualification(self, runtime_context) -> dict[str, Any] | None:
        if not _trusted_private_regular_file(self.qualification_path):
            return None
        try:
            value = json.loads(_read_private_regular_file(self.qualification_path))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(value, dict) or value.get("schema_version") != QUALIFICATION_SCHEMA:
            return None
        scope = _mapping(value.get("scope"))
        if scope.get("application") != "otel-demo" or scope.get("namespace") != runtime_context.target.namespace:
            return None
        issued = _time(scope.get("issued_at"))
        expires = _time(scope.get("expires_at"))
        now = self.now()
        if issued is None or expires is None or issued > now or expires <= now:
            return None
        return value

    def _d7_precheck(self, qualification: Mapping[str, Any] | None, alternative: str, target_uid: str) -> PrecheckResult:
        if qualification is None:
            return PrecheckResult(False, "qualification_evidence_missing_or_invalid", ())
        samples = []
        for raw in qualification.get("d7_historical_samples") or []:
            try:
                samples.append(D7HistoricalSample.model_validate(raw))
            except Exception:
                continue
        return CapabilityLossPrecheck.d7_history(
            alternative_server=alternative, target_uid=target_uid,
            samples=tuple(samples), now=self.now(),
        )

    def _d8_precheck(self, qualification: Mapping[str, Any] | None, alternative: str) -> PrecheckResult:
        if qualification is None:
            return PrecheckResult(False, "qualification_evidence_missing_or_invalid", ())
        for raw in qualification.get("d8_canaries") or []:
            try:
                canary = D8CanaryEvidence.model_validate(raw)
            except Exception:
                continue
            if canary.alternative_server == alternative:
                return CapabilityLossPrecheck.d8_canary(alternative_server=alternative, canary=canary)
        return PrecheckResult(False, "alternative_canary_missing", ())

    def _target_uid(self, runtime_context) -> str | None:
        inventory = _mapping(self.cleanup_backend.inventory_trial(runtime_context))
        trial = _mapping(inventory.get("trial"))
        uid = trial.get("target_uid")
        return str(uid) if inventory.get("qualified") is True and isinstance(uid, str) and uid and uid != "unbound" else None

    def _fault_window(self, runtime_context) -> FaultRunningWindow | None:
        inventory = _mapping(self.cleanup_backend.inventory_trial(runtime_context))
        trial = _mapping(inventory.get("trial"))
        started = _time(trial.get("started_at"))
        if inventory.get("qualified") is not True or trial.get("ever_active") is not True or started is None:
            return None
        uid = self._target_uid(runtime_context)
        if uid is None or trial.get("target_uid") != uid or int(trial.get("ledger_match_count") or 0) != 1:
            return None
        ended = _time(trial.get("ended_at"))
        ref = self._write_oracle_window(runtime_context.trial_id, trial, started, ended)
        return FaultRunningWindow(started_at=started, ended_at=ended, oracle_record_ref=ref)

    def _write_oracle_window(self, trial_id: str, trial: Mapping[str, Any], started: datetime, ended: datetime | None) -> str:
        directory = (self.evidence_root / trial_id).resolve()
        directory.relative_to(self.evidence_root)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = directory / "oracle-fault-window.json"
        payload = {
            "schema_version": "stage2-capability-loss-oracle-window.v1",
            "trial_id": trial_id, "target_uid": trial.get("target_uid"),
            "started_at": started.isoformat(), "ended_at": ended.isoformat() if ended else None,
            "ledger_match_count": trial.get("ledger_match_count"),
        }
        path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
        return f"private://capability-loss/{trial_id}/oracle-fault-window.json"

    def _d7_alternative_coverage(self, runtime: CapabilityLossRuntime, runtime_context, window: FaultRunningWindow | None) -> tuple[bool, tuple[str, ...]]:
        state = runtime.orchestrator.state(runtime.trial_id) if _state_exists(runtime) else None
        if state is None or window is None:
            return False, ()
        for call_id, call in runtime.calls.items():
            server, tool = _server_tool(call.tool)
            result = runtime.results.get(call_id)
            if server != state.alternative_server or not _is_observation(server, tool) or result is None:
                continue
            if result.status != "completed" or result.payload.get("ok") is not True:
                continue
            # Query arguments merely describe an Agent request.  Only the
            # server-returned series can prove target scope and time coverage.
            # Current Coroot/telemetry trace and log outputs do not expose a
            # stable target-UID plus timestamped evidence contract, so they are
            # deliberately insufficient rather than guessed into verification.
            if not _metric_result_covers_window(
                tool, call.arguments, result.payload, self._target_uid(runtime_context),
                window, self._d7_fault_type(runtime_context),
            ):
                continue
            return True, (f"platform://tool-result/{call_id}", window.oracle_record_ref)
        return False, (window.oracle_record_ref,)

    def _d7_fault_type(self, runtime_context) -> str | None:
        fault = _mapping(getattr(runtime_context, "main_fault", None))
        value = fault.get("fault_type")
        if isinstance(value, str) and value in _FAULT_METRIC_FAMILIES:
            return value
        inventory = _mapping(self.cleanup_backend.inventory_trial(runtime_context))
        trial = _mapping(inventory.get("trial"))
        value = trial.get("fault_type")
        if (
            inventory.get("qualified") is True
            and int(trial.get("ledger_match_count") or 0) == 1
            and isinstance(value, str)
            and value in _FAULT_METRIC_FAMILIES
        ):
            return value
        return None

def _metric_result_covers_window(
    tool: str, arguments: Mapping[str, Any], payload: Mapping[str, Any],
    target_uid: str | None, window: FaultRunningWindow, fault_type: str | None,
) -> bool:
    """Verify a returned Prometheus-style matrix, never its request envelope.

    Both ``coroot_metrics_range`` and telemetry's metric/query-range services
    return Prometheus matrices.  A successful substitute must satisfy three
    independent checks: the returned series targets the bound UID, its samples
    cover the independently observed fault window, and its structured metric
    identifier belongs to the executed fault's documented effect family.
    Trace/log response formats lack a guaranteed pod-UID/time contract, so
    they cannot establish D7 verification until those services expose one
    explicitly.
    """
    if target_uid is None or fault_type not in _FAULT_METRIC_FAMILIES or not _metric_range_tool(tool):
        return False
    matrix = _prometheus_matrix(payload)
    if matrix is None:
        return False
    started = window.started_at.timestamp()
    ended = (window.ended_at or datetime.now(UTC)).timestamp()
    for series in matrix:
        if not isinstance(series, Mapping) or not _series_targets_uid(series, target_uid):
            continue
        timestamps = _finite_series_timestamps(series.get("values"))
        if timestamps is None:
            continue
        if not _metric_relevant_to_fault(fault_type, arguments, payload, series):
            continue
        # Require actual finite observations on both sides of the independently
        # observed fault window.  Values need not look "bad": effect truth is
        # deliberately adjudicated by the independent Oracle.
        if min(timestamps) <= started and max(timestamps) >= ended:
            return True
    return False


def _metric_range_tool(tool: str) -> bool:
    return tool in {
        "coroot_metrics_range",
        "telemetry_prom_metric_range",
        "telemetry_prom_query_range",
    }


def _prometheus_matrix(payload: Mapping[str, Any]) -> list[Any] | None:
    candidate = _mapping(payload.get("data"))
    if not candidate:
        candidate = _mapping(payload)
    if candidate.get("resultType") != "matrix":
        return None
    result = candidate.get("result")
    return list(result) if isinstance(result, list) and result else None


def _series_targets_uid(series: Mapping[str, Any], target_uid: str) -> bool:
    labels = _mapping(series.get("metric"))
    return any(labels.get(key) == target_uid for key in (
        "pod_uid", "uid", "kubernetes_pod_uid", "k8s_pod_uid",
    ))


def _finite_series_timestamps(value: Any) -> list[float] | None:
    if not isinstance(value, list) or not value:
        return None
    timestamps: list[float] = []
    for point in value:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            return None
        timestamp = _finite_number(point[0])
        sample = _finite_number(point[1])
        if timestamp is None or sample is None:
            return None
        timestamps.append(timestamp)
    return timestamps


_PROMQL_METRIC_RE = re.compile(r"(?<![A-Za-z0-9_:])([A-Za-z_:][A-Za-z0-9_:]*)(?=\s*(?:\{|\[))")
_PROMQL_BARE_RE = re.compile(r"^[A-Za-z_:][A-Za-z0-9_:]*$")

_FAULT_METRIC_FAMILIES: dict[str, tuple[str, ...]] = {
    "network-delay": (
        "latency", "duration", "request_duration", "response_time", "roundtrip", "rtt",
        "http_server_duration", "http_client_duration", "http_request_duration",
        "grpc_server_handling",
    ),
    "network-loss": (
        "latency", "duration", "request_duration", "response_time", "error", "errors",
        "failure", "failures", "failed", "timeout", "timeouts", "retry", "retries",
        "grpc_server_handling",
    ),
    "cpu-load": ("cpu", "processor", "cfs_throttled", "throttle"),
    "memory-stress": ("memory", "working_set", "workingset", "rss", "heap", "oom"),
}

_METADATA_ONLY_MARKERS = (
    "kube_pod_status_ready", "kube_pod_status_phase", "kube_pod_container_status_ready",
    "kube_pod_container_status_restarts_total", "kube_pod_info", "kube_pod_labels",
    "kube_pod_owner", "kube_pod_created", "kube_replicaset", "kube_deployment",
    "kube_node", "target_info", "build_info",
)
_GENERIC_LIVENESS_METRICS = {"up"}


def _metric_relevant_to_fault(
    fault_type: str, arguments: Mapping[str, Any], payload: Mapping[str, Any],
    series: Mapping[str, Any],
) -> bool:
    identifiers = _metric_identifiers(arguments, payload, series)
    if not identifiers:
        return False
    if any(_metadata_only_metric(identifier) for identifier in identifiers):
        return False
    family = _FAULT_METRIC_FAMILIES[fault_type]
    return any(any(marker in identifier for marker in family) for identifier in identifiers)


def _metric_identifiers(
    arguments: Mapping[str, Any], payload: Mapping[str, Any], series: Mapping[str, Any],
) -> tuple[str, ...]:
    values: list[str] = []
    for source in (arguments, payload):
        for key in ("metric", "__name__"):
            value = source.get(key)
            if isinstance(value, str):
                values.append(value)
        query = source.get("query")
        if isinstance(query, str):
            values.extend(_promql_metric_names(query))
    labels = _mapping(series.get("metric"))
    for key in ("__name__", "metric", "name"):
        value = labels.get(key)
        if isinstance(value, str):
            values.append(value)
    return tuple(dict.fromkeys(_normalize_metric_identifier(value) for value in values if value))


def _promql_metric_names(query: str) -> tuple[str, ...]:
    stripped = query.strip()
    if _PROMQL_BARE_RE.fullmatch(stripped):
        return (_normalize_metric_identifier(stripped),)
    return tuple(_normalize_metric_identifier(match.group(1)) for match in _PROMQL_METRIC_RE.finditer(query))


def _normalize_metric_identifier(value: str) -> str:
    return value.strip().lower().replace("-", "_")


def _metadata_only_metric(identifier: str) -> bool:
    if identifier in _GENERIC_LIVENESS_METRICS:
        return True
    return any(marker in identifier for marker in _METADATA_ONLY_MARKERS)


def _state_exists(runtime: CapabilityLossRuntime) -> bool:
    try:
        runtime.orchestrator.state(runtime.trial_id)
        return True
    except KeyError:
        return False


def _mapping(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return dict(value) if isinstance(value, Mapping) else {}


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _finite_number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _trusted_private_regular_file(path: Path) -> bool:
    """Accept only a non-symlink Controller-owned private file and parent."""
    try:
        parent = path.parent.lstat()
        final = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(parent.st_mode)
        and not stat.S_ISLNK(parent.st_mode)
        and parent.st_uid == os.getuid()
        and not parent.st_mode & 0o077
        and stat.S_ISREG(final.st_mode)
        and not stat.S_ISLNK(final.st_mode)
        and final.st_uid == os.getuid()
        and not final.st_mode & 0o077
    )


def _read_private_regular_file(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        final = os.fstat(descriptor)
        if not stat.S_ISREG(final.st_mode) or final.st_uid != os.getuid() or final.st_mode & 0o077:
            raise OSError("qualification file is not private regular data")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            return handle.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
