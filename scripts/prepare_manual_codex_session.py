#!/usr/bin/env python3
"""Issue one short-lived, target-bound chaos capability for a manual Codex TUI."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_BASELINE_DIR = Path("/var/lib/resiliencebenchmark/chaos-control/baseline")
DEFAULT_OUTPUT = Path("/run/resiliencebenchmark/manual-codex/session.json")
SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,62}$")


def private_write(path: Path, payload: dict) -> None:
    parent = path.parent.resolve()
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(parent, 0o700)
    if stat.S_ISLNK(parent.lstat().st_mode):
        raise ValueError("session directory must not be a symlink")
    descriptor, raw_path = tempfile.mkstemp(prefix=".manual-session-", dir=parent)
    temporary = Path(raw_path)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def kubectl_json(kubeconfig: Path, *arguments: str) -> dict:
    completed = subprocess.run(
        ["kubectl", "--kubeconfig", str(kubeconfig), *arguments, "-o", "json"],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if completed.returncode:
        raise RuntimeError("scoped kubectl inventory failed")
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise TypeError("scoped kubectl inventory returned a non-object")
    return value


def issue_baseline_capability(
    *,
    baseline_dir: Path,
    controller_id: str,
    run_id: str,
    target_name: str,
    target_uid: str,
    summary: dict,
    ttl_seconds: int,
) -> tuple[str, str]:
    window = summary.get("measurementWindow")
    if (
        summary.get("qualified") is not True
        or not isinstance(window, dict)
        or int(window.get("durationSeconds", 0)) != 600
        or int(window.get("measurementWindowSeconds", 0)) != 300
        or window.get("calibrationWindowEligible") is not True
    ):
        raise ValueError("formal baseline summary does not meet the 600/300 contract")
    if not 60 <= ttl_seconds <= 3600:
        raise ValueError("capability ttl must be between 60 and 3600 seconds")
    baseline_root = baseline_dir.expanduser().resolve()
    baseline_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(baseline_root, 0o700)
    if stat.S_ISLNK(baseline_root.lstat().st_mode):
        raise ValueError("baseline ledger directory must not be a symlink")
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=ttl_seconds)
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    summary_json = json.dumps(
        summary, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    private_write(
        baseline_root / f"{token_hash}.json",
        {
            "schema_version": "baseline-capability.v1",
            "passed": True,
            "run_id": run_id,
            "trial_id": run_id,
            "namespace": "otel-demo",
            "target_name": target_name,
            "target_uid": target_uid,
            "controller_pod_uid": controller_id,
            "issued_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
            "summary_sha256": hashlib.sha256(summary_json.encode()).hexdigest(),
        },
    )
    return token, expires_at.isoformat()


def validate_recovery_report(path: Path, *, maximum_age_seconds: int) -> dict:
    resolved = path.expanduser().resolve()
    age = time.time() - resolved.stat().st_mtime
    if age < 0 or age > maximum_age_seconds:
        raise ValueError("recovery report is stale")
    report = json.loads(resolved.read_text(encoding="utf-8"))
    required_gates = {
        "fault_effect_verified",
        "controller_cleanup_verified",
        "global_inventory_zero",
        "target_identity_stable",
        "target_ready_without_restart",
        "business_recovery_verified",
    }
    gates = report.get("gates")
    if (
        report.get("qualified") is not True
        or not isinstance(gates, dict)
        or any(gates.get(key) is not True for key in required_gates)
    ):
        raise ValueError("recovery report did not pass every required gate")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--baseline-report", type=Path, required=True)
    parser.add_argument("--recovery-report", type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--target-component", required=True)
    parser.add_argument("--controller-id", default="manual-codex-controller")
    parser.add_argument(
        "--controller-token-ref",
        default="runtime://manual-codex-controller/token",
    )
    parser.add_argument("--baseline-dir", type=Path, default=DEFAULT_BASELINE_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--ttl-seconds", type=int, default=1800)
    parser.add_argument("--maximum-baseline-age-seconds", type=int, default=900)
    parser.add_argument("--maximum-recovered-baseline-age-seconds", type=int, default=7200)
    parser.add_argument("--maximum-recovery-age-seconds", type=int, default=900)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not SAFE_ID.fullmatch(args.run_id):
        raise SystemExit("run-id must be a safe lowercase identifier")
    baseline_path = args.baseline_report.expanduser().resolve()
    age = time.time() - baseline_path.stat().st_mtime
    recovery = (
        validate_recovery_report(
            args.recovery_report,
            maximum_age_seconds=args.maximum_recovery_age_seconds,
        )
        if args.recovery_report is not None
        else None
    )
    maximum_baseline_age = (
        args.maximum_recovered_baseline_age_seconds
        if recovery is not None
        else args.maximum_baseline_age_seconds
    )
    if age < 0 or age > maximum_baseline_age:
        raise SystemExit("formal baseline report is stale")
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    summary = baseline.get("summary")
    if baseline.get("status") != "qualified" or not isinstance(summary, dict):
        raise SystemExit("formal baseline is not qualified")

    kubeconfig = args.kubeconfig.expanduser().resolve()
    nodes = kubectl_json(kubeconfig, "get", "nodes")
    if not nodes.get("items") or any(
        not any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in item.get("status", {}).get("conditions", [])
        )
        for item in nodes["items"]
    ):
        raise SystemExit("not all Kubernetes nodes are Ready")
    chaos = kubectl_json(kubeconfig, "get", "chaosblades.chaosblade.io")
    if chaos.get("items"):
        raise SystemExit("a ChaosBlade resource already exists")
    pods = kubectl_json(kubeconfig, "-n", "otel-demo", "get", "pods")
    targets = [
        item
        for item in pods.get("items", [])
        if (
            item.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/component")
            == args.target_component
            and any(
                condition.get("type") == "Ready" and condition.get("status") == "True"
                for condition in item.get("status", {}).get("conditions", [])
            )
        )
    ]
    if len(targets) != 1:
        raise SystemExit("target component did not resolve to exactly one Ready Pod")
    target = targets[0]
    target_name = str(target["metadata"]["name"])
    target_uid = str(target["metadata"]["uid"])
    if recovery is not None and (
        recovery.get("target", {}).get("uid") != target_uid
        or recovery.get("target", {}).get("component") != args.target_component
    ):
        raise SystemExit("recovery report does not match the current target")
    token, expires_at = issue_baseline_capability(
        baseline_dir=args.baseline_dir,
        controller_id=args.controller_id,
        run_id=args.run_id,
        target_name=target_name,
        target_uid=target_uid,
        summary=summary,
        ttl_seconds=args.ttl_seconds,
    )
    output = args.output.expanduser().resolve()
    private_write(
        output,
        {
            "schema_version": "manual-codex-chaos-session.v1",
            "run_id": args.run_id,
            "target": {
                "namespace": "otel-demo",
                "kind": "Pod",
                "name": target_name,
                "uid": target_uid,
                "component": args.target_component,
            },
            "controller_token_ref": args.controller_token_ref,
            "expected_controller_pod_uid": args.controller_id,
            "baseline_gate_token": token,
            "cleanup_handle": "cleanup-" + secrets.token_hex(18),
            "expires_at": expires_at,
        },
    )
    print(
        json.dumps(
            {
                "status": "prepared",
                "run_id": args.run_id,
                "target_name": target_name,
                "target_uid": target_uid,
                "expires_at": expires_at,
                "session_file": str(output),
                "session_file_mode": "0600",
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
