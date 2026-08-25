"""Source/runtime identity comparison for semantic scan qualification."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SourceRuntimeStatus = Literal[
    "exact",
    "official_source_with_runtime_drift",
    "unknown",
]


class SourceRuntimeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceSnapshot(SourceRuntimeModel):
    path: str
    expected_version: str
    expected_commit: str | None = None
    observed_commit: str | None = None
    status: Literal["exact", "mismatch", "unknown"]


class ImageReference(SourceRuntimeModel):
    image: str
    digest: str | None = None
    config_digest: str | None = None
    layers: list[str] = Field(default_factory=list)
    entrypoint: list[str] = Field(default_factory=list)
    command: list[str] = Field(default_factory=list)
    source_revision: str | None = None


class LiveImageBinding(SourceRuntimeModel):
    namespace: str
    workload_kind: str
    workload_name: str
    pod_name: str | None = None
    pod_uid: str | None = None
    container: str
    image: str
    image_id: str | None = None
    config_digest: str | None = None
    layers: list[str] = Field(default_factory=list)
    entrypoint: list[str] = Field(default_factory=list)
    command: list[str] = Field(default_factory=list)
    source_revision: str | None = None


class RuntimeComparison(SourceRuntimeModel):
    namespace: str
    workload_kind: str
    workload_name: str
    pod_name: str | None = None
    pod_uid: str | None = None
    container: str
    image: str
    image_id: str | None = None
    status: SourceRuntimeStatus
    reasons: list[str]
    official_reference: ImageReference | None = None


class SourceRuntimeManifest(SourceRuntimeModel):
    schema_version: Literal["source-runtime-manifest.v1"] = "source-runtime-manifest.v1"
    source: SourceSnapshot
    runtime: list[RuntimeComparison]
    counts: dict[str, int]
    manifest_sha256: str


def detect_git_snapshot(
    source_path: Path,
    *,
    expected_version: str,
    expected_commit: str | None = None,
) -> SourceSnapshot:
    observed = _git_head(source_path)
    if observed is None:
        status: Literal["exact", "mismatch", "unknown"] = "unknown"
    elif expected_commit is None:
        status = "unknown"
    elif observed == expected_commit:
        status = "exact"
    else:
        status = "mismatch"
    return SourceSnapshot(
        path=str(source_path),
        expected_version=expected_version,
        expected_commit=expected_commit,
        observed_commit=observed,
        status=status,
    )


def build_source_runtime_manifest(
    source: SourceSnapshot,
    live_bindings: list[LiveImageBinding],
    official_references: dict[str, ImageReference],
) -> SourceRuntimeManifest:
    runtime = [
        compare_runtime_binding(binding, official_references.get(binding.container))
        for binding in live_bindings
    ]
    counts: dict[str, int] = {"exact": 0, "official_source_with_runtime_drift": 0, "unknown": 0}
    for item in runtime:
        counts[item.status] += 1
    payload = {
        "source": source.model_dump(mode="json"),
        "runtime": [item.model_dump(mode="json") for item in runtime],
        "counts": counts,
    }
    return SourceRuntimeManifest(
        source=source,
        runtime=runtime,
        counts=counts,
        manifest_sha256=_sha(payload),
    )


def compare_runtime_binding(
    live: LiveImageBinding,
    official: ImageReference | None,
) -> RuntimeComparison:
    reasons: list[str] = []
    status: SourceRuntimeStatus = "unknown"
    if official is None:
        reasons.append("missing official image reference")
    else:
        live_digest = _image_digest(live.image_id) or _image_digest(live.image)
        official_digest = _image_digest(official.digest) or _image_digest(official.image)
        if live_digest and official_digest and live_digest == official_digest:
            status = "exact"
            reasons.append("live image digest matches official reference")
        elif _same_config_and_layers(live, official):
            status = "exact"
            reasons.append("image config and layer chain match official reference")
        elif _official_layers_preserved_with_drift(live, official):
            status = "official_source_with_runtime_drift"
            reasons.extend(_drift_reasons(live, official))
        elif _same_source_revision(live, official) and _has_runtime_difference(
            live, official
        ):
            status = "official_source_with_runtime_drift"
            reasons.append("source revision matches but image digest/config differs")
            reasons.extend(_drift_reasons(live, official))
        else:
            reasons.append("insufficient image metadata to prove source/runtime identity")
    return RuntimeComparison(
        namespace=live.namespace,
        workload_kind=live.workload_kind,
        workload_name=live.workload_name,
        pod_name=live.pod_name,
        pod_uid=live.pod_uid,
        container=live.container,
        image=live.image,
        image_id=live.image_id,
        status=status,
        reasons=reasons,
        official_reference=official,
    )


def live_bindings_from_kubernetes_resources(
    resources: list[dict[str, Any]],
) -> list[LiveImageBinding]:
    pods_by_container = _pod_status_index(resources)
    bindings: list[LiveImageBinding] = []
    for item in resources:
        resource = item.get("resource") if isinstance(item.get("resource"), dict) else {}
        kind = str(resource.get("kind") or "")
        if kind not in {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}:
            continue
        namespace = str(resource.get("namespace") or "")
        workload_name = str(resource.get("name") or "")
        for container in resource.get("containers", []):
            if not isinstance(container, dict):
                continue
            name = str(container.get("name") or "")
            pod_status = pods_by_container.get((namespace, name))
            bindings.append(
                LiveImageBinding(
                    namespace=namespace,
                    workload_kind=kind,
                    workload_name=workload_name,
                    pod_name=pod_status.get("pod_name") if pod_status else None,
                    pod_uid=pod_status.get("pod_uid") if pod_status else None,
                    container=name,
                    image=str(container.get("image") or ""),
                    image_id=pod_status.get("image_id") if pod_status else None,
                    command=[str(value) for value in container.get("command", [])],
                    entrypoint=[str(value) for value in container.get("command", [])],
                )
            )
    return bindings


def _pod_status_index(resources: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, str]]:
    output: dict[tuple[str, str], dict[str, str]] = {}
    for item in resources:
        resource = item.get("resource") if isinstance(item.get("resource"), dict) else {}
        if resource.get("kind") != "Pod":
            continue
        status = resource.get("status") if isinstance(resource.get("status"), dict) else {}
        for container in status.get("container_statuses", []):
            if not isinstance(container, dict):
                continue
            output[(str(resource.get("namespace") or ""), str(container.get("name") or ""))] = {
                "pod_name": str(resource.get("name") or ""),
                "pod_uid": str(resource.get("uid") or ""),
                "image_id": str(container.get("image_id") or ""),
            }
    return output


def _same_config_and_layers(live: LiveImageBinding, official: ImageReference) -> bool:
    return bool(
        live.config_digest
        and official.config_digest
        and live.config_digest == official.config_digest
        and live.layers
        and official.layers
        and live.layers == official.layers
    )


def _official_layers_preserved_with_drift(
    live: LiveImageBinding,
    official: ImageReference,
) -> bool:
    if not live.layers or not official.layers:
        return False
    official_prefix = live.layers[: len(official.layers)] == official.layers
    official_application_tail_preserved = (
        official.layers[-1] in live.layers
        and live.layers.index(official.layers[-1]) < len(live.layers) - 1
    )
    return (
        official_prefix or official_application_tail_preserved
    ) and (
        len(live.layers) > len(official.layers)
        or _entrypoint_or_command_changed(live, official)
    )


def _entrypoint_or_command_changed(
    live: LiveImageBinding,
    official: ImageReference,
) -> bool:
    return bool(
        (live.entrypoint or official.entrypoint)
        and live.entrypoint != official.entrypoint
        or (live.command or official.command)
        and live.command != official.command
    )


def _same_source_revision(live: LiveImageBinding, official: ImageReference) -> bool:
    return bool(
        live.source_revision
        and official.source_revision
        and live.source_revision == official.source_revision
    )


def _has_runtime_difference(live: LiveImageBinding, official: ImageReference) -> bool:
    live_digest = _image_digest(live.image_id) or _image_digest(live.image)
    official_digest = _image_digest(official.digest) or _image_digest(official.image)
    if live_digest and official_digest and live_digest != official_digest:
        return True
    if live.config_digest and official.config_digest and live.config_digest != official.config_digest:
        return True
    return _official_layers_preserved_with_drift(live, official)


def _drift_reasons(live: LiveImageBinding, official: ImageReference) -> list[str]:
    reasons: list[str] = []
    if live.layers and official.layers and len(live.layers) > len(official.layers):
        reasons.append(
            f"live image has {len(live.layers) - len(official.layers)} extra layer(s)"
        )
    if live.entrypoint != official.entrypoint:
        reasons.append("container entrypoint differs from official reference")
    if live.command != official.command:
        reasons.append("container command differs from official reference")
    if live.config_digest and official.config_digest and live.config_digest != official.config_digest:
        reasons.append("image config digest differs from official reference")
    return reasons or ["runtime image differs while preserving official source evidence"]


def _image_digest(value: str | None) -> str | None:
    if not value or "sha256:" not in value:
        return None
    return "sha256:" + value.rsplit("sha256:", 1)[1].split("@", 1)[0].split(":", 1)[0]


def _git_head(path: Path) -> str | None:
    git_dir = path / ".git"
    if git_dir.exists():
        try:
            from dulwich.objects import Tag
            from dulwich.repo import Repo

            repo = Repo(str(path))
            head = repo.refs[b"HEAD"]
            obj = repo[head]
            while isinstance(obj, Tag):
                _, peeled = obj.object
                head = peeled
                obj = repo[head]
            if getattr(obj, "type_name", None) == b"commit":
                return obj.id.decode("ascii")
        except Exception:  # noqa: BLE001 - unreadable Git metadata yields unknown identity.
            return None
    marker = path / ".source-revision"
    if marker.is_file():
        for line in marker.read_text(encoding="utf-8").splitlines():
            if line.startswith("commit="):
                value = line.removeprefix("commit=").strip()
                if len(value) == 40:
                    return value
    return None


def _sha(value: Any) -> str:
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(rendered.encode()).hexdigest()
