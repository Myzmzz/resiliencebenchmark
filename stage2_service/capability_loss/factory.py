"""Controller-private construction of D7/D8 runtime evidence adapters."""

from __future__ import annotations

import json
import math
import os
import re
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from stage2_service.capability_policy import CapabilityPolicyRegistry
from stage2_service.target_binding import current as current_target_binding
from stage2_service.platform_ledger import PlatformLedger

from .precheck import CapabilityLossPrecheck, PrecheckResult
from .records import CapabilityLossCase, CapabilityLossVariant, D7HistoricalSample, D8CanaryEvidence, FaultRunningWindow
from .runtime import CapabilityLossRuntime, _is_observation, _server_tool


QUALIFICATION_SCHEMA = "stage2-capability-loss-qualification.v1"
ORACLE_WINDOW_FILE = "oracle-fault-window.json"
ORACLE_WINDOW_SCHEMA = "stage2-capability-loss-oracle-window.v1"
# A Pod name the inventory falls back to when nothing was bound yet.
_UNBOUND_POD_NAME = "unbound"


@dataclass(frozen=True)
class OracleTarget:
    """The Pod the executed fault ran on, as the independent fault inventory records it.

    ``name`` is kept only when it certainly belongs to ``uid``; without it a
    returned series can identify the Pod by an explicit UID label alone.
    """

    namespace: str
    uid: str
    name: str | None = None


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
        if scope.get("application") != current_target_binding().application or scope.get("namespace") != runtime_context.target.namespace:
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

    def _oracle_target(self, runtime_context) -> OracleTarget | None:
        """Identify the executed fault's Pod from the inventory, never from the Agent."""
        uid = self._target_uid(runtime_context)
        if uid is None:
            return None
        trial = _mapping(_mapping(self.cleanup_backend.inventory_trial(runtime_context)).get("trial"))
        bound = runtime_context.target
        return oracle_target(
            uid=uid,
            namespace=str(trial.get("namespace") or bound.namespace),
            inventory_name=trial.get("target_name"),
            bound_name=bound.name,
            bound_uid=bound.uid,
        )

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
        path = directory / ORACLE_WINDOW_FILE
        payload = {
            "schema_version": ORACLE_WINDOW_SCHEMA,
            "trial_id": trial_id, "target_uid": trial.get("target_uid"),
            # Kept so a later re-score can identify the Pod and fault exactly as here.
            "namespace": trial.get("namespace"), "target_name": trial.get("target_name"),
            "fault_type": trial.get("fault_type"),
            "started_at": started.isoformat(), "ended_at": ended.isoformat() if ended else None,
            "ledger_match_count": trial.get("ledger_match_count"),
        }
        path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
        return oracle_window_ref(trial_id)

    def _d7_alternative_coverage(self, runtime: CapabilityLossRuntime, runtime_context, window: FaultRunningWindow | None) -> tuple[bool, tuple[str, ...]]:
        state = runtime.orchestrator.state(runtime.trial_id) if _state_exists(runtime) else None
        if state is None or window is None:
            return False, ()
        covering = first_covering_result(
            ((call, runtime.results.get(call_id)) for call_id, call in runtime.calls.items()),
            alternative_server=state.alternative_server,
            target=self._oracle_target(runtime_context),
            window=window,
            fault_type=self._d7_fault_type(runtime_context),
        )
        if covering is None:
            return False, (window.oracle_record_ref,)
        return True, (f"platform://tool-result/{covering}", window.oracle_record_ref)

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

def oracle_window_ref(trial_id: str) -> str:
    """The evidence reference of one Trial's Oracle fault-window record."""
    return f"private://capability-loss/{trial_id}/{ORACLE_WINDOW_FILE}"


def read_oracle_window(evidence_root: Path, trial_id: str) -> tuple[FaultRunningWindow, dict[str, Any]] | None:
    """Load the Oracle fault-window record the runtime wrote for one Trial.

    Returns the window plus the raw record, or None when the record is
    missing, not private Controller data, or not this Trial's.
    """
    if not trial_id or Path(trial_id).name != trial_id:
        return None
    path = Path(evidence_root) / trial_id / ORACLE_WINDOW_FILE
    if not _trusted_private_regular_file(path):
        return None
    try:
        record = json.loads(_read_private_regular_file(path))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(record, dict) or record.get("schema_version") != ORACLE_WINDOW_SCHEMA or record.get("trial_id") != trial_id:
        return None
    started = _time(record.get("started_at"))
    if started is None:
        return None
    window = FaultRunningWindow(
        started_at=started, ended_at=_time(record.get("ended_at")), oracle_record_ref=oracle_window_ref(trial_id),
    )
    return window, record


def oracle_target(
    *, uid: str, namespace: str, inventory_name: Any, bound_name: Any, bound_uid: Any,
) -> OracleTarget:
    """Pair the Oracle's fault UID with a Pod name only when the name is certainly that Pod's.

    The inventory reports the ledger's target name, or falls back to the
    runtime's bound name when the ledger has none.  A name that differs from
    the bound one can only have come from the ledger; a name equal to the
    bound one is trusted only when the bound UID is the fault's UID too.
    """
    name = inventory_name if isinstance(inventory_name, str) and inventory_name not in ("", _UNBOUND_POD_NAME) else None
    if name is not None and name == bound_name and bound_uid != uid:
        name = None
    return OracleTarget(namespace=namespace, uid=uid, name=name)


def first_covering_result(
    pairs, *, alternative_server: str, target: OracleTarget | None,
    window: FaultRunningWindow, fault_type: str | None,
) -> str | None:
    """Return the call id of the first alternative-path result that covers the fault window.

    ``pairs`` yields ``(ToolCall, ToolResult | None)``.  Query arguments merely
    describe an Agent request; only server-returned series can prove target
    scope and time coverage.  Current Coroot/telemetry trace and log outputs
    do not expose a stable target plus timestamped evidence contract, so they
    are deliberately insufficient rather than guessed into verification.
    """
    for call, result in pairs:
        server, tool = _server_tool(call.tool)
        if server != alternative_server or not _is_observation(server, tool) or result is None:
            continue
        if result.status != "completed" or result.payload.get("ok") is not True:
            continue
        if metric_result_covers_window(
            tool, call.arguments, result.payload, target, window, fault_type,
            observed_at=result.occurred_at,
        ):
            return call.call_id
    return None


def metric_result_covers_window(
    tool: str, arguments: Mapping[str, Any], payload: Mapping[str, Any],
    target: OracleTarget | None, window: FaultRunningWindow, fault_type: str | None,
    *, observed_at: datetime,
) -> bool:
    """Verify a returned Prometheus-style matrix, never its request envelope.

    Both ``coroot_metrics_range`` and telemetry's metric/query-range services
    return Prometheus matrices.  A successful substitute must satisfy three
    independent checks: a returned series belongs to the executed fault's Pod,
    its metric identifier belongs to the fault's documented effect family, and
    its observed samples reach from before the fault into the fault window.
    Trace/log response formats lack a guaranteed Pod/time contract, so they
    cannot establish D7 verification until those services expose one
    explicitly.
    """
    if target is None or fault_type not in _FAULT_METRIC_FAMILIES or not _metric_range_tool(tool):
        return False
    matrix = _prometheus_matrix(payload)
    if matrix is None:
        return False
    started = window.started_at.timestamp()
    ended = (window.ended_at or datetime.now(UTC)).timestamp()
    for series in matrix:
        if not isinstance(series, Mapping) or not _series_targets_pod(series, target):
            continue
        timestamps = _observed_sample_times(series.get("values"), observed_at)
        if not timestamps:
            continue
        if not _metric_relevant_to_fault(fault_type, arguments, payload, series):
            continue
        # A baseline observation at or before the fault started, and at least
        # one observation while it ran: the evidence an effect conclusion
        # needs.  Values need not look "bad": effect truth is deliberately
        # adjudicated by the independent Oracle.
        if any(stamp <= started for stamp in timestamps) and any(started < stamp <= ended for stamp in timestamps):
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


def _series_targets_pod(series: Mapping[str, Any], target: OracleTarget) -> bool:
    """Identify the Pod the way the D7 qualification probe does.

    Real series rarely carry a UID label: cAdvisor/Prometheus series name the
    Pod by ``namespace`` + ``pod`` (the cgroup ``id`` may embed the UID) and
    Coroot container series by ``container_id="/k8s/<ns>/<pod>/<container>"``.
    """
    labels = _mapping(series.get("metric"))
    if any(labels.get(key) == target.uid for key in ("pod_uid", "uid", "kubernetes_pod_uid", "k8s_pod_uid")):
        return True
    cgroup = str(labels.get("id") or "")
    if target.uid in cgroup or target.uid.replace("-", "_") in cgroup:
        return True
    if target.name is None:
        return False
    if labels.get("namespace") == target.namespace and labels.get("pod") == target.name:
        return True
    return str(labels.get("container_id") or "").startswith(f"/k8s/{target.namespace}/{target.name}/")


def _observed_sample_times(value: Any, observed_at: datetime) -> list[float]:
    """Timestamps of the finite samples the backend had observed when it answered.

    A range reaching past the query time comes back with null points or with
    the last sample carried forward (lookback); neither is an observation, so
    only finite points stamped no later than the result count.  One null
    point no longer discards the rest of its series.
    """
    if not isinstance(value, list):
        return []
    limit = observed_at.timestamp()
    timestamps: list[float] = []
    for point in value:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            continue
        timestamp = _finite_number(point[0])
        sample = _finite_number(point[1])
        if timestamp is not None and sample is not None and timestamp <= limit:
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
# Fault types whose effect D7 can verify from a metric family.
D7_EFFECT_FAULT_TYPES = frozenset(_FAULT_METRIC_FAMILIES)

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
