"""Remove and verify only Controller workload objects owned by one Run."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts.reset_episode import Runner, run_checked

ALLOWED_KINDS = {"Job": "job", "ConfigMap": "configmap", "Pod": "pod"}


def cleanup_run_workloads(
    *,
    kubeconfig: Path,
    run_id: str,
    runner: Runner,
) -> dict[str, Any]:
    base = [
        "kubectl",
        "--kubeconfig",
        str(kubeconfig),
        "-n",
        "otel-demo",
    ]
    selector = "resiliencebenchmark.io/managed-by=controller"

    def owned() -> list[tuple[str, str, str]]:
        raw = run_checked(
            runner,
            [*base, "get", "jobs,configmaps,pods", "-l", selector, "-o", "json"],
            timeout=60,
        )
        payload = json.loads(raw)
        result = []
        for item in payload.get("items", []):
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "")
            metadata = item.get("metadata") or {}
            labels = metadata.get("labels") or {}
            workload_run_id = str(labels.get("resiliencebenchmark.io/run-id") or "")
            if kind in ALLOWED_KINDS and run_id in workload_run_id:
                result.append((kind, str(metadata.get("name") or ""), workload_run_id))
        return result

    before = owned()
    deleted = []
    for kind, name, workload_run_id in before:
        if not name:
            continue
        run_checked(
            runner,
            [
                *base,
                "delete",
                f"{ALLOWED_KINDS[kind]}/{name}",
                "--ignore-not-found=true",
                "--wait=true",
            ],
            timeout=180,
        )
        deleted.append(
            {"kind": kind, "name": name, "workload_run_id": workload_run_id}
        )
    after = owned()
    return {
        "verified": not after,
        "owned_before": len(before),
        "deleted": deleted,
        "owned_after": len(after),
        "evidence_refs": [f"kubernetes://otel-demo/controller-workloads/{run_id}"],
    }
