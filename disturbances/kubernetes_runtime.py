"""Real Kubernetes client for Controller-owned typed disturbances."""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from decimal import ROUND_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

DNS_NAME = re.compile(r"^[a-z0-9]([-.a-z0-9]*[a-z0-9])?$")


class KubernetesRuntimeError(RuntimeError):
    pass


class KubernetesDisturbanceClient:
    def __init__(
        self,
        core_api: Any,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.core_api = core_api
        self.sleep = sleep
        self.monotonic = monotonic

    @classmethod
    def from_kubeconfig(cls, kubeconfig: Path) -> KubernetesDisturbanceClient:
        path = kubeconfig.expanduser().resolve()
        if not path.is_file():
            raise KubernetesRuntimeError("configured kubeconfig does not exist")
        try:
            from kubernetes import client, config
        except ImportError as exc:
            raise KubernetesRuntimeError(
                "the kubernetes Python package is required for live disturbances"
            ) from exc
        config.load_kube_config(config_file=str(path), persist_config=False)
        return cls(client.CoreV1Api())

    def restart_exact_pod(
        self,
        *,
        namespace: str,
        name: str,
        expected_uid: str,
        timeout_seconds: int,
        labels: Mapping[str, str],
    ) -> dict[str, str]:
        _safe_name(namespace, "namespace")
        _safe_name(name, "Pod name")
        if not expected_uid or len(expected_uid) > 128:
            raise KubernetesRuntimeError("expected_uid is required")
        if not 5 <= timeout_seconds <= 600:
            raise KubernetesRuntimeError("replacement timeout must be between 5 and 600 seconds")
        pod = self.core_api.read_namespaced_pod(name=name, namespace=namespace)
        actual_uid = str(_attr(_attr(pod, "metadata"), "uid") or "")
        if actual_uid != expected_uid:
            raise KubernetesRuntimeError(
                "target Pod UID drifted before restart; refusing stale-target deletion"
            )
        owner_uid = _controller_owner_uid(pod)
        if not owner_uid:
            raise KubernetesRuntimeError(
                "target Pod has no supported controller owner; replacement is not guaranteed"
            )
        body = {
            "apiVersion": "v1",
            "kind": "DeleteOptions",
            "gracePeriodSeconds": 30,
            "preconditions": {"uid": expected_uid},
        }
        self.core_api.delete_namespaced_pod(name=name, namespace=namespace, body=body)
        deadline = self.monotonic() + timeout_seconds
        while self.monotonic() < deadline:
            pods = self.core_api.list_namespaced_pod(namespace=namespace).items
            for candidate in pods:
                candidate_uid = str(_attr(_attr(candidate, "metadata"), "uid") or "")
                if (
                    candidate_uid
                    and candidate_uid != expected_uid
                    and _controller_owner_uid(candidate) == owner_uid
                    and _pod_ready(candidate)
                ):
                    return {
                        "name": str(_attr(_attr(candidate, "metadata"), "name") or ""),
                        "uid": candidate_uid,
                    }
            self.sleep(1)
        raise KubernetesRuntimeError(
            "replacement Pod with the same controller owner did not become Ready before timeout"
        )

    def read_resource_quota(self, *, namespace: str, name: str) -> dict[str, Any]:
        _safe_name(namespace, "namespace")
        _safe_name(name, "ResourceQuota name")
        quota = self.core_api.read_namespaced_resource_quota(name=name, namespace=namespace)
        metadata = _attr(quota, "metadata")
        spec = _attr(quota, "spec")
        return {
            "hard": dict(_attr(spec, "hard") or {}),
            "labels": dict(_attr(metadata, "labels") or {}),
            "uid": str(_attr(metadata, "uid") or ""),
        }

    def patch_resource_quota(
        self,
        *,
        namespace: str,
        name: str,
        cpu_percent: float,
        memory_percent: float,
        labels: Mapping[str, str],
    ) -> dict[str, Any]:
        previous = self.read_resource_quota(namespace=namespace, name=name)
        _bounded_percent(cpu_percent, "cpu_percent")
        _bounded_percent(memory_percent, "memory_percent")
        hard: dict[str, str] = {}
        changed = 0
        for key, raw in previous["hard"].items():
            if key == "cpu" or key.endswith(".cpu"):
                hard[key] = _scale_quantity(str(raw), Decimal(str(cpu_percent)) / 100, "cpu")
                changed += 1
            elif key == "memory" or key.endswith(".memory"):
                hard[key] = _scale_quantity(
                    str(raw), Decimal(str(memory_percent)) / 100, "memory"
                )
                changed += 1
            else:
                hard[key] = str(raw)
        if changed == 0:
            raise KubernetesRuntimeError("ResourceQuota has no CPU or memory hard limits")
        merged_labels = {**previous["labels"], **dict(labels)}
        response = self.core_api.patch_namespaced_resource_quota(
            name=name,
            namespace=namespace,
            body={"metadata": {"labels": merged_labels}, "spec": {"hard": hard}},
        )
        return {
            "uid": str(_attr(_attr(response, "metadata"), "uid") or previous["uid"]),
            "hard": hard,
            "labels": merged_labels,
        }

    def restore_resource_quota(
        self,
        *,
        namespace: str,
        name: str,
        previous: Mapping[str, Any],
    ) -> dict[str, Any]:
        current = self.read_resource_quota(namespace=namespace, name=name)
        if previous.get("uid") and current.get("uid") != previous.get("uid"):
            raise KubernetesRuntimeError(
                "ResourceQuota UID changed; refusing to restore a stale object"
            )
        response = self.core_api.patch_namespaced_resource_quota(
            name=name,
            namespace=namespace,
            body={
                "metadata": {"labels": dict(previous.get("labels") or {})},
                "spec": {"hard": dict(previous.get("hard") or {})},
            },
        )
        return {
            "uid": str(_attr(_attr(response, "metadata"), "uid") or current["uid"]),
            "hard": dict(previous.get("hard") or {}),
            "labels": dict(previous.get("labels") or {}),
        }


def _attr(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _controller_owner_uid(pod: Any) -> str | None:
    owners = _attr(_attr(pod, "metadata"), "owner_references") or []
    for owner in owners:
        if bool(_attr(owner, "controller")) and str(_attr(owner, "kind")) in {
            "ReplicaSet",
            "StatefulSet",
        }:
            uid = str(_attr(owner, "uid") or "")
            return uid or None
    return None


def _pod_ready(pod: Any) -> bool:
    conditions = _attr(_attr(pod, "status"), "conditions") or []
    return any(
        str(_attr(condition, "type")) == "Ready"
        and str(_attr(condition, "status")) == "True"
        for condition in conditions
    )


def _safe_name(value: str, description: str) -> None:
    if not DNS_NAME.fullmatch(value):
        raise KubernetesRuntimeError(f"invalid {description}")


def _bounded_percent(value: float, name: str) -> None:
    if not 1 <= float(value) <= 100:
        raise KubernetesRuntimeError(f"{name} must be between 1 and 100")


def _scale_quantity(raw: str, ratio: Decimal, resource: str) -> str:
    value = _parse_quantity(raw, resource) * ratio
    if resource == "cpu":
        milli = (value * 1000).to_integral_value(rounding=ROUND_UP)
        return f"{milli}m"
    return str(value.to_integral_value(rounding=ROUND_UP))


def _parse_quantity(raw: str, resource: str) -> Decimal:
    suffixes = {
        "Ki": Decimal(1024),
        "Mi": Decimal(1024) ** 2,
        "Gi": Decimal(1024) ** 3,
        "Ti": Decimal(1024) ** 4,
        "K": Decimal(1000),
        "M": Decimal(1000) ** 2,
        "G": Decimal(1000) ** 3,
        "T": Decimal(1000) ** 4,
    }
    if resource == "cpu" and raw.endswith("m"):
        number, multiplier = raw[:-1], Decimal("0.001")
    else:
        suffix = next((item for item in suffixes if raw.endswith(item)), "")
        number = raw[: -len(suffix)] if suffix else raw
        multiplier = suffixes.get(suffix, Decimal(1))
    try:
        return Decimal(number) * multiplier
    except InvalidOperation as exc:
        raise KubernetesRuntimeError(f"unsupported Kubernetes quantity: {raw}") from exc
