#!/usr/bin/env python3
"""Inventory deployed container image digests without exposing registry endpoints.

The command is read-only and requires an explicit kubeconfig plus namespace
allowlist. It records the Pod/container identity, a redacted repository tail,
and the runtime image digest reported by Kubernetes.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable


DNS_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}$")
MAX_NAMESPACES = 10
KUBECTL_TIMEOUT_SECONDS = 30


class InventoryError(RuntimeError):
    """Raised when a read-only runtime image inventory cannot be trusted."""


Runner = Callable[[list[str]], dict[str, Any]]


@dataclass(frozen=True)
class ImageObservation:
    namespace: str
    pod: str
    container: str
    container_kind: str
    image_ref: str
    image_digest: str | None
    pod_phase: str
    ready: bool
    restart_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "pod": self.pod,
            "container": self.container,
            "containerKind": self.container_kind,
            "imageRef": redacted_image_ref(self.image_ref),
            "imageRefSha256": hashlib.sha256(self.image_ref.encode()).hexdigest(),
            "imageDigest": self.image_digest,
            "podPhase": self.pod_phase,
            "ready": self.ready,
            "restartCount": self.restart_count,
        }


def kubectl_runner(argv: list[str]) -> dict[str, Any]:
    try:
        result = subprocess.run(
            argv,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=KUBECTL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise InventoryError("kubectl read-only image inventory timed out") from exc
    if result.returncode != 0:
        raise InventoryError("kubectl read-only image inventory failed")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise InventoryError("kubectl image inventory did not return JSON") from exc
    if not isinstance(payload, dict):
        raise InventoryError("kubectl image inventory response must be an object")
    return payload


def validate_namespace(namespace: str) -> str:
    if not DNS_NAME_RE.fullmatch(namespace):
        raise InventoryError("namespace must be a concrete Kubernetes DNS name")
    return namespace


def normalize_image_digest(image_id: str | None) -> str | None:
    if not image_id:
        return None
    match = re.search(r"sha256:[0-9a-f]{64}$", image_id)
    return match.group(0) if match else None


def redacted_image_ref(image_ref: str) -> str:
    """Keep a useful repository tail while removing registry host and tag."""

    value = image_ref.strip()
    if not value or any(marker in value for marker in ("?", "#", "\\")):
        return "<redacted-image-ref>"
    without_digest = value.split("@", 1)[0]
    last_slash = without_digest.rfind("/")
    repository = without_digest[last_slash + 1 :] if last_slash >= 0 else without_digest
    if ":" in repository:
        repository = repository.rsplit(":", 1)[0]
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", repository):
        return "<redacted-image-ref>"
    return f"<registry>/{repository}"


def observe_namespace(namespace: str, kubeconfig: Path, runner: Runner) -> list[ImageObservation]:
    data = runner(
        [
            "kubectl",
            "--kubeconfig",
            str(kubeconfig),
            "--request-timeout=30s",
            "get",
            "pods",
            "-n",
            namespace,
            "-o",
            "json",
        ]
    )
    observations: list[ImageObservation] = []
    for pod in data.get("items", []):
        metadata = pod.get("metadata", {}) if isinstance(pod, dict) else {}
        spec = pod.get("spec", {}) if isinstance(pod, dict) else {}
        status = pod.get("status", {}) if isinstance(pod, dict) else {}
        phase = str(status.get("phase") or "Unknown")
        if metadata.get("deletionTimestamp") or phase in {"Succeeded", "Failed"}:
            continue
        pod_name = str(metadata.get("name", ""))
        container_groups = (
            ("application", spec.get("containers", []), status.get("containerStatuses", [])),
            ("init", spec.get("initContainers", []), status.get("initContainerStatuses", [])),
            ("ephemeral", spec.get("ephemeralContainers", []), status.get("ephemeralContainerStatuses", [])),
        )
        for container_kind, declared, statuses in container_groups:
            image_by_container = {
                str(item.get("name", "")): str(item.get("image", ""))
                for item in declared or []
                if isinstance(item, dict)
            }
            for item in statuses or []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", ""))
                state = item.get("state", {}) if isinstance(item.get("state"), dict) else {}
                terminated = state.get("terminated", {}) if isinstance(state.get("terminated"), dict) else {}
                completed_init = (
                    container_kind == "init"
                    and terminated.get("exitCode") == 0
                )
                observations.append(
                    ImageObservation(
                        namespace=namespace,
                        pod=pod_name,
                        container=name,
                        container_kind=container_kind,
                        image_ref=image_by_container.get(name, ""),
                        image_digest=normalize_image_digest(item.get("imageID")),
                        pod_phase=phase,
                        ready=bool(item.get("ready", False)) or completed_init,
                        restart_count=int(item.get("restartCount") or 0),
                    )
                )
    return observations


def build_report(namespaces: list[str], kubeconfig: Path, runner: Runner = kubectl_runner) -> dict[str, Any]:
    if not kubeconfig.is_absolute() or not kubeconfig.is_file():
        raise InventoryError("kubeconfig must be an explicit absolute existing file")
    if not namespaces or len(namespaces) > MAX_NAMESPACES:
        raise InventoryError(f"one to {MAX_NAMESPACES} namespaces are required")
    selected = [validate_namespace(item) for item in namespaces]
    if len(set(selected)) != len(selected):
        raise InventoryError("namespace list contains duplicates")

    by_namespace = {
        namespace: observe_namespace(namespace, kubeconfig, runner)
        for namespace in selected
    }
    observations = [item for namespace in selected for item in by_namespace[namespace]]
    records = [item.as_dict() for item in observations]
    missing_digest = sum(1 for item in observations if item.image_digest is None)
    unready = sum(1 for item in observations if not item.ready)
    namespace_summary = {
        namespace: {
            "containers": len(items),
            "missingRuntimeDigest": sum(1 for item in items if item.image_digest is None),
            "unreadyContainers": sum(1 for item in items if not item.ready),
            "qualified": bool(items)
            and all(item.image_digest is not None and item.ready for item in items),
        }
        for namespace, items in by_namespace.items()
    }
    return {
        "apiVersion": "resiliencebenchmark.io/v1alpha1",
        "kind": "RuntimeImageInventory",
        "metadata": {"generatedAt": datetime.now(timezone.utc).isoformat()},
        "spec": {
            "mode": "read-only",
            "kubeconfigSource": "explicit-runtime-file",
            "namespaces": selected,
            "summary": {
                "containers": len(records),
                "missingRuntimeDigest": missing_digest,
                "unreadyContainers": unready,
                "qualified": bool(records)
                and missing_digest == 0
                and unready == 0
                and all(item["qualified"] for item in namespace_summary.values()),
            },
            "namespaceSummary": namespace_summary,
            "observations": records,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only Kubernetes runtime image digest inventory")
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--namespace", action="append", required=True)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = build_report(args.namespace, args.kubeconfig.expanduser().resolve())
    except InventoryError as exc:
        print(f"inventory_runtime_images: {exc}", file=sys.stderr)
        return 2
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0 if report["spec"]["summary"]["qualified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
