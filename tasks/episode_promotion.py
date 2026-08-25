"""Promote an internal Episode design into locked public and multi-level contracts."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema

from controller.run_contracts import RunSpec
from controller.system_snapshot import SnapshotStatus, SystemSnapshot
from disturbances.types import DisturbanceType
from progression.builder import build_multi_level_episode

REPO_ROOT = Path(__file__).resolve().parents[1]


class EpisodePromotionError(RuntimeError):
    pass


@dataclass(frozen=True)
class PromotionQualification:
    independent_observers_qualified: bool
    cleanup_path_qualified: bool


@dataclass(frozen=True)
class PromotedEpisode:
    internal_plan: dict[str, Any]
    public_episode: dict[str, Any]
    multi_level_episode: dict[str, Any]


FAULT_POLICIES: dict[str, tuple[str, dict[str, Any]]] = {
    "latency": ("network-delay", {"delay_ms": 100, "duration_seconds": 60}),
    "network-delay": ("network-delay", {"delay_ms": 100, "duration_seconds": 60}),
    "packet_loss": ("network-loss", {"loss_percent": 5, "duration_seconds": 60}),
    "network-loss": ("network-loss", {"loss_percent": 5, "duration_seconds": 60}),
    "pod_restart": ("pod-kill", {"pod_count": 1, "duration_seconds": 30}),
    "pod-kill": ("pod-kill", {"pod_count": 1, "duration_seconds": 30}),
    "cpu_pressure": ("cpu-load", {"cpu_percent": 20, "duration_seconds": 60}),
    "cpu-load": ("cpu-load", {"cpu_percent": 20, "duration_seconds": 60}),
    "memory_pressure": ("memory-stress", {"mem_percent": 20, "duration_seconds": 60}),
    "memory-stress": ("memory-stress", {"mem_percent": 20, "duration_seconds": 60}),
}

EXECUTABLE_DISTURBANCE_PROFILES: dict[str, tuple[DisturbanceType, ...]] = {
    "standard-l1": (),
    "standard-l2": (DisturbanceType.TARGET_DRIFT,),
    "standard-l3": (
        DisturbanceType.TARGET_DRIFT,
        DisturbanceType.METRIC_DATA_GAP,
    ),
    # Engineering profiles exercise the same disturbance semantics as the
    # standard profiles. Only the observation windows differ at execution
    # time, so a fast end-to-end qualification cannot silently change the
    # question or the injected conditions.
    "engineering-l1": (),
    "engineering-l2": (DisturbanceType.TARGET_DRIFT,),
    "engineering-l3": (
        DisturbanceType.TARGET_DRIFT,
        DisturbanceType.METRIC_DATA_GAP,
    ),
}


def promote_episode(
    internal_episode: Mapping[str, Any],
    snapshot: SystemSnapshot,
    run_spec: RunSpec,
    qualification: PromotionQualification,
) -> PromotedEpisode:
    blockers = _blockers(internal_episode, snapshot, qualification)
    if blockers:
        raise EpisodePromotionError("; ".join(blockers))
    plan = deepcopy(dict(internal_episode))
    plan["episode_id"] = _public_episode_id(
        str(internal_episode["episode_id"]), snapshot.snapshot_id
    )
    candidate_services = plan.get("application_snapshot", {}).get("candidate_services", [])
    if not isinstance(candidate_services, list) or len(candidate_services) != 1:
        raise EpisodePromotionError("internal Episode must identify exactly one candidate component")
    component = str(candidate_services[0])
    target = _resolve_target(component, snapshot)
    trigger_classes = plan.get("action_space", {}).get("allowed_trigger_classes", [])
    trigger = str(trigger_classes[0]) if isinstance(trigger_classes, list) and trigger_classes else ""
    policy = FAULT_POLICIES.get(trigger)
    if policy is None:
        raise EpisodePromotionError(f"no registered bounded main-fault policy for trigger class {trigger}")
    actuator, parameters = policy
    plan["application_snapshot"]["runtime_target"] = {
        "component": component,
        "kind": "Pod",
        "name": target.name,
        "uid": target.uid,
        "status": "resolved",
    }
    plan["action_space"]["selected_actuator"] = actuator
    plan["action_space"]["parameters"] = [
        {"name": key, "value": value, "status": "provided"}
        for key, value in parameters.items()
    ]
    plan["budget"]["max_experiments"] = run_spec.progression.total_retry_budget
    plan["readiness"] = {
        "ready_for_execution": True,
        "ready_for_lock": True,
        "execution_blockers": [],
        "promotion_requirements": [],
    }
    public = _public_episode(plan, snapshot, actuator)
    _assert_public_safe(public)
    _validate(public, REPO_ROOT / "tasks/schemas/episode-public.schema.json")
    disturbance_order = EXECUTABLE_DISTURBANCE_PROFILES.get(
        run_spec.progression.profile_id
    )
    if disturbance_order is None:
        raise EpisodePromotionError(
            f"disturbance profile is not registered: {run_spec.progression.profile_id}"
        )
    multi_level = build_multi_level_episode(
        plan,
        level_count=run_spec.progression.max_levels,
        total_retry_budget=run_spec.progression.total_retry_budget,
        agent_visible_task=public,
        disturbance_order=disturbance_order,
    )
    _validate(multi_level, REPO_ROOT / "tasks/schemas/multi-level-episode.schema.json")
    return PromotedEpisode(
        internal_plan=plan,
        public_episode=public,
        multi_level_episode=multi_level,
    )


def _blockers(
    episode: Mapping[str, Any],
    snapshot: SystemSnapshot,
    qualification: PromotionQualification,
) -> list[str]:
    blockers = []
    if snapshot.source.status is not SnapshotStatus.QUALIFIED:
        blockers.append("source snapshot is not qualified")
    if snapshot.runtime.status is not SnapshotStatus.QUALIFIED:
        blockers.append("live runtime snapshot is not qualified")
    if snapshot.workload.calibration_status != "qualified":
        blockers.append("formal workload baseline is not qualified")
    if snapshot.observers.status is not SnapshotStatus.QUALIFIED:
        blockers.append("live metric, trace, and log observers are not qualified")
    if not qualification.independent_observers_qualified:
        blockers.append("independent observers are not qualified")
    if not qualification.cleanup_path_qualified:
        blockers.append("Controller per-trial cleanup path is not qualified")
    if not episode.get("episode_id"):
        blockers.append("internal Episode id is missing")
    return blockers


def _resolve_target(component: str, snapshot: SystemSnapshot):
    normalized = _component_key(component)
    matches = [
        target
        for target in snapshot.runtime.targets
        if target.ready
        and (
            _component_key(target.component) == normalized
            if target.component
            else _component_key(target.name.split("-", 1)[0]) == normalized
        )
    ]
    if len(matches) != 1:
        raise EpisodePromotionError(
            f"candidate component {component} resolved to {len(matches)} live Ready Pods"
        )
    return matches[0]


def _component_key(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]", "", value.lower())
    return normalized.removesuffix("service")


def _public_episode_id(internal_id: str, snapshot_id: str) -> str:
    digest = hashlib.sha256(f"{internal_id}\x1f{snapshot_id}".encode()).hexdigest()
    return f"EPI-LOCKED-{digest[:12].upper()}"


def _public_episode(
    plan: Mapping[str, Any],
    snapshot: SystemSnapshot,
    actuator: str,
) -> dict[str, Any]:
    application = str(plan["application_snapshot"]["application"])
    namespace = str(plan["application_snapshot"]["namespace"])
    workload = snapshot.workload
    return {
        "schema_version": "episode-public.v0.1",
        "episode_id": str(plan["episode_id"]),
        "title": f"Bounded resilience investigation for {application}",
        "status": "locked",
        "application": {
            "name": application,
            "namespace": namespace,
            "candidate_services": _public_candidate_services(application),
            "release_ref": snapshot.source.commit,
        },
        "agent_goal": (
            "Use only the granted evidence and Controller-approved action to test one "
            "bounded resilience hypothesis, verify recovery, and report uncertainty."
        ),
        "environment_snapshot": {
            "snapshot_id": snapshot.snapshot_id,
            "health_prerequisites": [
                "All application controllers and Pods are Ready.",
                "The registered deterministic workload satisfies its formal baseline window.",
                "Independent observation and Controller cleanup paths are qualified.",
            ],
            "reset_contract": [
                "Destroy only the run-owned fault handle.",
                "Verify fault-object absence and business SLO recovery before the next attempt.",
            ],
        },
        "workload": {
            "profile": workload.profile_ref,
            "slo": [
                f"success rate >= {workload.minimum_success_rate}",
                f"error rate <= {workload.maximum_error_rate}",
                f"p95 latency <= {workload.maximum_p95_latency_ms} ms",
                f"throughput >= {workload.minimum_throughput_rps} requests/s",
            ],
        },
        "observability": {
            "metrics": ["scoped Prometheus metric tools"],
            "traces": ["scoped Jaeger trace tools"],
            "logs": ["scoped Loki log tools"],
            "kubernetes": ["namespace-scoped Kubernetes inventory, status, events, and logs"],
        },
        "source_access": {
            "mode": "read_only",
            "allowed_paths": [f"/workspace/src/{application}"],
            "forbidden_paths": ["/workspace/private", "/workspace/ground_truth"],
        },
        "action_space": {
            "allowed_trigger_classes": [actuator],
            "allowed_target_scope": ["one Controller-qualified Pod in the application namespace"],
            "forbidden_actions": [
                "Do not use shell or direct Kubernetes writes.",
                "Do not access evaluator-only data.",
                "Do not run more than one active fault.",
            ],
        },
        "budget": {
            "max_experiments": int(plan["budget"]["max_experiments"]),
            "max_duration_minutes": int(plan["budget"].get("max_duration_minutes") or 30),
            "max_concurrent_faults": 1,
        },
        "safety_constraints": [
            "Re-query the exact Pod UID before each mutation.",
            "Stop when an abort condition or budget limit is reached.",
            "Treat API acknowledgement as insufficient recovery evidence.",
        ],
        "expected_agent_output": [
            "A confirmed, rejected, or inconclusive hypothesis.",
            "Run-scoped evidence references for effect, diagnosis, and recovery.",
            "Remaining uncertainty and safety status.",
        ],
        "leakage_controls": [
            "No candidate identifier, hidden mechanism, exact answer, or evaluator verdict is present.",
            "Runtime disturbances are Controller-owned and omitted from the prompt.",
        ],
    }


def _public_candidate_services(application: str) -> list[str]:
    if application == "otel-demo":
        return ["frontend", "checkout", "cart", "payment", "shipping"]
    raise EpisodePromotionError(f"no public candidate-service policy for {application}")


def _assert_public_safe(value: Mapping[str, Any]) -> None:
    serialized = json.dumps(value, ensure_ascii=False)
    if re.search(r"RBD-[0-9]+", serialized, flags=re.IGNORECASE):
        raise EpisodePromotionError("public Episode contains a private defect identifier")

    def inspect_keys(item: Any, path: str = "$") -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                normalized = re.sub(r"[-_]", "", str(key).lower())
                if normalized in {"candidateid", "defectref", "groundtruth", "hiddentruth"}:
                    raise EpisodePromotionError(
                        f"public Episode contains forbidden private key at {path}.{key}"
                    )
                inspect_keys(child, f"{path}.{key}")
        elif isinstance(item, list):
            for index, child in enumerate(item):
                inspect_keys(child, f"{path}[{index}]")

    inspect_keys(value)


def _validate(value: Mapping[str, Any], schema_path: Path) -> None:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    try:
        jsonschema.validate(dict(value), schema)
    except jsonschema.ValidationError as exc:
        raise EpisodePromotionError(f"promoted Episode failed schema: {exc.message}") from exc
