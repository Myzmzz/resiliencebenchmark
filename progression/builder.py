"""Build deterministic, progressively harder levels from one defect plan."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Sequence

from disturbances.types import (
    DisturbanceDefinition,
    DisturbanceType,
    derive_replay_seed,
    load_disturbance_library,
)


RELEVANT_DISTURBANCES: Mapping[str, tuple[DisturbanceType, ...]] = {
    "latency": (
        DisturbanceType.FAULT_EFFECT_DEVIATION,
        DisturbanceType.TELEMETRY_INSTABILITY,
        DisturbanceType.CLEANUP_DELAY,
    ),
    "network-delay": (
        DisturbanceType.FAULT_EFFECT_DEVIATION,
        DisturbanceType.TELEMETRY_INSTABILITY,
        DisturbanceType.CLEANUP_DELAY,
    ),
    "packet_loss": (
        DisturbanceType.FAULT_EFFECT_DEVIATION,
        DisturbanceType.METRIC_DATA_GAP,
        DisturbanceType.CLEANUP_DELAY,
    ),
    "network-loss": (
        DisturbanceType.FAULT_EFFECT_DEVIATION,
        DisturbanceType.METRIC_DATA_GAP,
        DisturbanceType.CLEANUP_DELAY,
    ),
    "cpu_pressure": (
        DisturbanceType.RESOURCE_QUOTA_REDUCTION,
        DisturbanceType.TELEMETRY_INSTABILITY,
        DisturbanceType.SAFETY_THRESHOLD_PRESSURE,
    ),
    "cpu-load": (
        DisturbanceType.RESOURCE_QUOTA_REDUCTION,
        DisturbanceType.TELEMETRY_INSTABILITY,
        DisturbanceType.SAFETY_THRESHOLD_PRESSURE,
    ),
    "memory_pressure": (
        DisturbanceType.RESOURCE_QUOTA_REDUCTION,
        DisturbanceType.METRIC_DATA_GAP,
        DisturbanceType.SAFETY_THRESHOLD_PRESSURE,
    ),
    "memory-stress": (
        DisturbanceType.RESOURCE_QUOTA_REDUCTION,
        DisturbanceType.METRIC_DATA_GAP,
        DisturbanceType.SAFETY_THRESHOLD_PRESSURE,
    ),
    "pod_restart": (
        DisturbanceType.TARGET_DRIFT,
        DisturbanceType.TELEMETRY_INSTABILITY,
        DisturbanceType.CLEANUP_DELAY,
    ),
    "pod-kill": (
        DisturbanceType.TARGET_DRIFT,
        DisturbanceType.TELEMETRY_INSTABILITY,
        DisturbanceType.CLEANUP_DELAY,
    ),
}

DEFAULT_DISTURBANCES = (
    DisturbanceType.TARGET_DRIFT,
    DisturbanceType.TELEMETRY_INSTABILITY,
    DisturbanceType.CLEANUP_DELAY,
)

FORBIDDEN_AGENT_VISIBLE_KEYS = frozenset(
    {
        "groundtruth",
        "hiddentruth",
        "oracle",
        "evaluatororacle",
        "oraclerawverdicts",
        "injecteddefect",
        "injecteddefectmanifest",
        "scoringrubricweights",
    }
)


def build_multi_level_episode(
    plan: Mapping[str, Any],
    *,
    level_count: int = 3,
    total_retry_budget: int | None = None,
    agent_visible_task: Mapping[str, Any] | None = None,
    library: Mapping[DisturbanceType, DisturbanceDefinition] | None = None,
) -> dict[str, Any]:
    """Return a deterministic multi-level episode for one main-fault plan.

    ``agent_visible_task`` is intentionally explicit. Internal Episode designs
    can contain hidden hypotheses and must not be copied into a harness prompt.
    """

    if level_count < 1 or level_count > 8:
        raise ValueError("level_count must be between 1 and 8")
    episode_id = str(plan.get("episode_id") or "")
    if not episode_id:
        raise ValueError("plan requires episode_id")
    configured_budget = _configured_budget(plan)
    budget = int(total_retry_budget if total_retry_budget is not None else configured_budget)
    if budget < level_count:
        raise ValueError(
            f"total_retry_budget {budget} cannot fund one attempt for each of {level_count} levels"
        )
    definitions = dict(library or load_disturbance_library())
    ordered_types = _recommended_types(_fault_type(plan))
    retry_budgets = _allocate_attempts(level_count, budget)
    levels: list[dict[str, Any]] = []
    for index in range(level_count):
        level_id = f"L{index + 1}"
        selected = _types_for_level(index, ordered_types)
        disturbances = []
        for item_index, disturbance_type in enumerate(selected, start=1):
            definition = definitions[disturbance_type]
            disturbance_id = f"{level_id}-D{item_index}-{disturbance_type.value}"
            seed = derive_replay_seed(episode_id, level_id, disturbance_type.value)
            disturbances.append(
                definition.instantiate(
                    disturbance_id=disturbance_id,
                    replay_seed=seed,
                ).as_dict()
            )
        levels.append(
            {
                "level_id": level_id,
                "complexity": _complexity_name(index, level_count),
                "disturbances": disturbances,
                "pass_criteria": (
                    "Precondition, fault_effect, diagnosis, recovery, and safety gates all pass"
                    + ("; every configured disturbance response behavior is independently evidenced" if disturbances else "")
                ),
                "retry_budget": retry_budgets[index],
            }
        )
    base_task = _base_task(plan)
    if agent_visible_task is not None:
        _assert_no_hidden_agent_keys(agent_visible_task)
        base_task["agent_visible_task"] = deepcopy(dict(agent_visible_task))
    result = {
        "schema_version": "multi-level-episode.v1",
        "episode_id": episode_id,
        "base_task": base_task,
        "levels": levels,
        "total_retry_budget": budget,
        "determinism": {
            "sequence_key": "episode_id+level_id+disturbance_type",
            "agent_independent": True,
            "run_replay_fallback": "run_id+level_id+disturbance_id",
        },
    }
    validate_multi_level_episode(result)
    return result


def wrap_single_level_episode(
    public_episode: Mapping[str, Any],
    *,
    target: Mapping[str, Any],
    main_fault: Mapping[str, Any],
) -> dict[str, Any]:
    """Materialize an existing public Episode as the L1-only special case.

    The public contract does not contain an immutable runtime UID, so callers
    must provide the Controller-qualified target binding explicitly.
    """

    episode_id = str(public_episode.get("episode_id") or "")
    if not episode_id:
        raise ValueError("public episode requires episode_id")
    budget = public_episode.get("budget")
    max_experiments = (
        int(budget.get("max_experiments", 1)) if isinstance(budget, Mapping) else 1
    )
    plan = {
        "episode_id": episode_id,
        "base_task": {
            "defect_ref": "PUBLIC-CONTRACT",
            "target": deepcopy(dict(target)),
            "main_fault": deepcopy(dict(main_fault)),
        },
        "budget": {"max_experiments": max_experiments},
    }
    return build_multi_level_episode(
        plan,
        level_count=1,
        total_retry_budget=max_experiments,
        agent_visible_task=public_episode,
    )


def validate_multi_level_episode(episode: Mapping[str, Any]) -> None:
    levels = episode.get("levels")
    if not isinstance(levels, list) or not levels:
        raise ValueError("multi-level episode requires at least one level")
    if levels[0].get("level_id") != "L1" or levels[0].get("disturbances") != []:
        raise ValueError("L1 must be the disturbance-free baseline")
    expected_ids = [f"L{index}" for index in range(1, len(levels) + 1)]
    if [level.get("level_id") for level in levels] != expected_ids:
        raise ValueError("level ids must be contiguous L1..Ln")
    previous_count = -1
    retry_sum = 0
    for level in levels:
        disturbances = level.get("disturbances")
        if not isinstance(disturbances, list):
            raise ValueError("level disturbances must be a list")
        if len(disturbances) < previous_count:
            raise ValueError("disturbance complexity must not decrease across levels")
        previous_count = len(disturbances)
        retry_budget = level.get("retry_budget")
        if not isinstance(retry_budget, int) or retry_budget < 1:
            raise ValueError("every level retry_budget must fund at least one attempt")
        retry_sum += retry_budget
        ids = [item.get("disturbance_id") for item in disturbances]
        if len(ids) != len(set(ids)):
            raise ValueError("disturbance ids must be unique within a level")
    if retry_sum != int(episode.get("total_retry_budget", -1)):
        raise ValueError("sum(level.retry_budget) must equal total_retry_budget")


def _types_for_level(index: int, ordered: Sequence[DisturbanceType]) -> tuple[DisturbanceType, ...]:
    if index == 0:
        return ()
    return tuple(ordered[: min(index, len(ordered))])


def _recommended_types(fault_type: str) -> tuple[DisturbanceType, ...]:
    configured = RELEVANT_DISTURBANCES.get(fault_type, DEFAULT_DISTURBANCES)
    tail = tuple(item for item in DisturbanceType if item not in configured)
    return (*configured, *tail)


def _configured_budget(plan: Mapping[str, Any]) -> int:
    budget = plan.get("budget")
    if isinstance(budget, Mapping) and budget.get("max_experiments") is not None:
        return int(budget["max_experiments"])
    if plan.get("total_retry_budget") is not None:
        return int(plan["total_retry_budget"])
    return 6


def _allocate_attempts(level_count: int, total: int) -> list[int]:
    result = [1] * level_count
    remaining = total - level_count
    index = level_count - 1
    while remaining:
        result[index] += 1
        remaining -= 1
        index = (index - 1) % level_count
    return result


def _fault_type(plan: Mapping[str, Any]) -> str:
    base_task = plan.get("base_task")
    if isinstance(base_task, Mapping):
        main_fault = base_task.get("main_fault")
        if isinstance(main_fault, Mapping):
            return str(main_fault.get("type") or main_fault.get("fault_type") or "")
    action_space = plan.get("action_space")
    if isinstance(action_space, Mapping):
        trigger_classes = action_space.get("allowed_trigger_classes")
        if isinstance(trigger_classes, list) and trigger_classes:
            return str(trigger_classes[0])
        selected = action_space.get("selected_actuator")
        if selected:
            return str(selected)
    return ""


def _base_task(plan: Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(plan.get("base_task"), Mapping):
        return deepcopy(dict(plan["base_task"]))
    snapshot = plan.get("application_snapshot")
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    target = snapshot.get("runtime_target")
    target = dict(target) if isinstance(target, Mapping) else {}
    target.setdefault("namespace", snapshot.get("namespace"))
    target.setdefault("application", snapshot.get("application"))
    action_space = plan.get("action_space")
    action_space = action_space if isinstance(action_space, Mapping) else {}
    parameters = action_space.get("parameters", [])
    parameter_map: dict[str, Any] = {}
    if isinstance(parameters, list):
        parameter_map = {
            str(item.get("name")): item.get("value")
            for item in parameters
            if isinstance(item, Mapping) and item.get("name")
        }
    return {
        "defect_ref": str(plan.get("defect_ref") or "UNSPECIFIED"),
        "target": target,
        "main_fault": {
            "type": _fault_type(plan) or "unspecified",
            "actuator": action_space.get("selected_actuator"),
            "parameters": parameter_map,
        },
    }


def _complexity_name(index: int, level_count: int) -> str:
    if index == 0:
        return "baseline"
    if index == 1:
        return "single"
    if index == level_count - 1 and index >= 3:
        return "full_chain"
    return "compound"


def _assert_no_hidden_agent_keys(value: Any, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "").replace("_", "")
            if any(forbidden in normalized for forbidden in FORBIDDEN_AGENT_VISIBLE_KEYS):
                raise ValueError(f"agent_visible_task contains forbidden key at {path}.{key}")
            _assert_no_hidden_agent_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_no_hidden_agent_keys(item, f"{path}[{index}]")
