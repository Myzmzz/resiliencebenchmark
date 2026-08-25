#!/usr/bin/env python3
"""Qualify exact-UID target drift and a bounded post-replacement recovery smoke."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from controller.system_snapshot import KubectlReadOnlyAdapter, SnapshotStatus
from disturbances.kubernetes_runtime import KubernetesDisturbanceClient
from scripts.reset_episode import (
    LOCUST_IMAGE_ENV,
    SubprocessCommandRunner,
    wait_application_ready,
    wait_cleanup_workload,
)

IMAGE_RE = re.compile(r"^.+:[^/@]+@sha256:[0-9a-f]{64}$")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--run-id", default="target-drift-qualification")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--execute", action="store_true")
    return parser


def execute(kubeconfig: Path, image: str, run_id: str) -> dict:
    runtime_before = KubectlReadOnlyAdapter(kubeconfig).scan("otel-demo")
    if runtime_before.status is not SnapshotStatus.QUALIFIED:
        raise RuntimeError("OTel Demo is not qualified before target drift")
    targets = [
        target
        for target in runtime_before.targets
        if target.component == "frontend" and target.ready
    ]
    if len(targets) != 1:
        raise RuntimeError("frontend did not resolve to one Ready Pod")
    old = targets[0]
    replacement = KubernetesDisturbanceClient.from_kubeconfig(
        kubeconfig
    ).restart_exact_pod(
        namespace="otel-demo",
        name=old.name,
        expected_uid=old.uid,
        timeout_seconds=180,
        labels={"benchmark.run_id": run_id, "benchmark.level_id": "qualification"},
    )
    runtime_after = KubectlReadOnlyAdapter(kubeconfig).scan("otel-demo")
    replacement_matches = [
        target
        for target in runtime_after.targets
        if target.component == "frontend" and target.ready
    ]
    if (
        runtime_after.status is not SnapshotStatus.QUALIFIED
        or len(replacement_matches) != 1
        or replacement_matches[0].uid != replacement["uid"]
        or replacement["uid"] == old.uid
    ):
        raise RuntimeError("replacement frontend Pod did not qualify with a new UID")
    recovery = wait_cleanup_workload(
        SubprocessCommandRunner(),
        "otel-demo",
        run_id,
        kubeconfig,
        {LOCUST_IMAGE_ENV: image},
        timeout_seconds=300,
        duration_seconds=60,
    )
    wait_application_ready("otel-demo", kubeconfig, 300)
    qualified = recovery.get("summary", {}).get("qualified") is True
    return {
        "schema_version": "target-drift-live-qualification.v1",
        "status": "qualified" if qualified else "failed",
        "run_id": run_id,
        "old_target": {"name": old.name, "uid": old.uid},
        "replacement_target": replacement,
        "uid_changed": replacement["uid"] != old.uid,
        "runtime": {
            "controllers_desired": runtime_after.controllers_desired,
            "controllers_ready": runtime_after.controllers_ready,
            "pods_total": runtime_after.pods_total,
            "pods_ready": runtime_after.pods_ready,
        },
        "recovery_smoke": recovery.get("summary"),
        "formal_run_eligible": False,
        "formal_run_blocker": "qualification used a 60-second recovery smoke, not a scored 300-second window",
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    kubeconfig = args.kubeconfig.expanduser().resolve()
    if not kubeconfig.is_file() or not IMAGE_RE.fullmatch(args.image):
        print(json.dumps({"status": "blocked", "message": "scoped kubeconfig and pinned image required"}))
        return 2
    if not re.fullmatch(r"^[a-z0-9][a-z0-9-]{2,62}$", args.run_id):
        print(json.dumps({"status": "blocked", "message": "invalid run id"}))
        return 2
    if not args.execute:
        report = {
            "schema_version": "target-drift-live-qualification.v1",
            "status": "planned",
            "mutation": "delete one exact frontend Pod with UID precondition",
            "recovery_smoke_seconds": 60,
        }
    else:
        try:
            report = execute(kubeconfig, args.image, args.run_id)
        except (OSError, RuntimeError, ValueError) as exc:
            report = {
                "schema_version": "target-drift-live-qualification.v1",
                "status": "failed",
                "error_type": type(exc).__name__,
                "message": str(exc)[:500],
            }
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0 if report["status"] in {"planned", "qualified"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
