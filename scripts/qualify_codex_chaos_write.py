#!/usr/bin/env python3
"""Qualify Codex headless approval plus gated Chaos create/get/destroy."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from controller.runtime_secrets import (
    BaselineCapabilityIssuer,
    PrivateRuntimeSecretStore,
)
from controller.system_snapshot import KubectlReadOnlyAdapter, SnapshotStatus
from controller.trial_finalization import McpMainFaultControl
from mcp_servers.chaos_control.service import new_cleanup_handle
from scripts.local_control_stack import ENDPOINTS
from scripts.run_harness_trial import run_trial
from scripts.smoke_mcp_data_path import load_private_env


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stack-env", type=Path, required=True)
    parser.add_argument("--baseline-report", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--public-episode", type=Path, required=True)
    parser.add_argument("--model", default="gpt-5.5")
    parser.add_argument("--run-id", default="codex-chaos-write-smoke")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--execute", action="store_true")
    return parser


def execute(args: argparse.Namespace) -> dict:
    env = load_private_env(args.stack_env)
    controller_kubeconfig = Path(env["RESBENCH_KUBECONFIG"])
    runtime = KubectlReadOnlyAdapter(controller_kubeconfig).scan("otel-demo")
    if runtime.status is not SnapshotStatus.QUALIFIED:
        raise RuntimeError("OTel Demo is not qualified")
    targets = [
        target
        for target in runtime.targets
        if target.component == "frontend" and target.ready
    ]
    if len(targets) != 1:
        raise RuntimeError("frontend did not resolve to one Ready Pod")
    target = targets[0]
    baseline = json.loads(args.baseline_report.read_text(encoding="utf-8"))
    summary = baseline.get("summary")
    if baseline.get("status") != "qualified" or not isinstance(summary, dict):
        raise RuntimeError("formal baseline is not qualified")
    runtime_root = args.runtime_root.expanduser().resolve()
    secrets = PrivateRuntimeSecretStore(runtime_root / "private/secrets")
    capability = BaselineCapabilityIssuer(
        baseline_ledger_dir=runtime_root / "chaos-control/baseline",
        secret_store=secrets,
        controller_pod_uid=env["RESBENCH_CHAOS_CONTROLLER_POD_UID"],
    ).issue(
        trial_id=args.run_id,
        run_id=args.run_id,
        namespace="otel-demo",
        target_name=target.name,
        target_uid=target.uid,
        summary=summary,
    )
    cleanup_handle = new_cleanup_handle()
    trial_env = {
        **env,
        **ENDPOINTS,
        "RESBENCH_BASELINE_GATE_TOKEN": secrets.get(
            capability["baseline_gate_token_ref"]
        ),
        "RESBENCH_CLEANUP_HANDLE": cleanup_handle,
        "RESBENCH_AUTHORIZED_RUN_ID": args.run_id,
        "RESBENCH_AUTHORIZED_TARGET_JSON": json.dumps(
            {
                "namespace": "otel-demo",
                "kind": "Pod",
                "name": target.name,
                "uid": target.uid,
                "component": "frontend",
            },
            separators=(",", ":"),
        ),
        "RESBENCH_MAIN_FAULT_JSON": json.dumps(
            {
                "type": "network-delay",
                "actuator": "network-delay",
                "parameters": {"delay_ms": 10, "duration_seconds": 5},
            },
            separators=(",", ":"),
        ),
    }
    report = run_trial(
        Path(__file__).resolve().parents[1],
        "codex",
        args.model,
        prompt_ref="chaos_write_smoke",
        episode_file=args.public_episode,
        execute=True,
        artifact_root=runtime_root / "codex-write-smoke",
        timeout_seconds=300,
        parent_env=trial_env,
        trial_id=args.run_id,
    )
    control = McpMainFaultControl(
        url=env["RESBENCH_CHAOS_CONTROL_MCP_URL"],
        token=env["RESBENCH_MCP_TOKEN"],
    )
    inventory = dict(control.inventory("otel-demo"))
    fallback_cleanup = None
    if inventory.get("global_chaosblade_count") != 0:
        fallback_cleanup = dict(control.destroy(cleanup_handle))
        inventory = dict(control.inventory("otel-demo"))
    ledger_path = runtime_root / "chaos-control/active" / f"{cleanup_handle}.json"
    ledger = (
        json.loads(ledger_path.read_text(encoding="utf-8"))
        if ledger_path.is_file()
        else {}
    )
    qualified = (
        report.get("status") == "completed"
        and ledger.get("ever_active") is True
        and ledger.get("state") == "destroyed"
        and inventory.get("global_chaosblade_count") == 0
        and inventory.get("active_owned_count") == 0
    )
    return {
        "schema_version": "codex-chaos-write-qualification.v1",
        "status": "qualified" if qualified else "failed",
        "run_id": args.run_id,
        "harness_status": report.get("status"),
        "harness_artifact_ref": report.get("artifactRef"),
        "cleanup_handle": cleanup_handle,
        "fallback_cleanup_required": fallback_cleanup is not None,
        "ledger": {
            "ever_active": ledger.get("ever_active"),
            "state": ledger.get("state"),
            "target_uid": ledger.get("target_uid"),
            "fault_type": ledger.get("fault_type"),
        },
        "global_count_after": inventory.get("global_chaosblade_count"),
        "active_owned_after": inventory.get("active_owned_count"),
        "formal_run_eligible": False,
        "formal_run_blocker": "write smoke used 10 ms for 5 seconds and has no scored workload window",
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not re.fullmatch(r"^[a-z0-9][a-z0-9-]{2,62}$", args.run_id):
        print(json.dumps({"status": "blocked", "message": "invalid run id"}))
        return 2
    if not args.execute:
        report = {
            "schema_version": "codex-chaos-write-qualification.v1",
            "status": "planned",
            "fault": {"type": "network-delay", "delay_ms": 10, "duration_seconds": 5},
        }
    else:
        try:
            report = execute(args)
        except (OSError, RuntimeError, ValueError) as exc:
            report = {
                "schema_version": "codex-chaos-write-qualification.v1",
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
