#!/usr/bin/env python3
"""Qualify one minimal real ChaosBlade create/get/destroy path, then prove absence."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path
from typing import Any

from controller.runtime_secrets import (
    BaselineCapabilityIssuer,
    PrivateRuntimeSecretStore,
)
from controller.system_snapshot import KubectlReadOnlyAdapter, SnapshotStatus
from mcp_servers.chaos_control.service import (
    ChaosControlService,
    RuntimeConfig,
    new_cleanup_handle,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--controller-kubeconfig", type=Path, required=True)
    parser.add_argument("--chaos-kubeconfig", type=Path, required=True)
    parser.add_argument("--baseline-report", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--run-id", default="local-chaos-qualification")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--execute", action="store_true")
    return parser


def _target(controller_kubeconfig: Path) -> dict[str, str]:
    runtime = KubectlReadOnlyAdapter(controller_kubeconfig).scan("otel-demo")
    if runtime.status is not SnapshotStatus.QUALIFIED:
        raise RuntimeError("OTel Demo runtime is not qualified")
    matches = [
        target
        for target in runtime.targets
        if target.component == "frontend" and target.ready
    ]
    if len(matches) != 1:
        raise RuntimeError("frontend did not resolve to one Ready Pod")
    target = matches[0]
    return {
        "namespace": "otel-demo",
        "name": target.name,
        "uid": target.uid,
    }


async def execute(args: argparse.Namespace) -> dict[str, Any]:
    runtime_root = args.runtime_root.expanduser().resolve()
    baseline_report = json.loads(
        args.baseline_report.expanduser().read_text(encoding="utf-8")
    )
    summary = baseline_report.get("summary")
    if baseline_report.get("status") != "qualified" or not isinstance(summary, dict):
        raise RuntimeError("formal baseline report is not qualified")
    target = _target(args.controller_kubeconfig.expanduser().resolve())
    controller_id = "local-controller-qualification"
    secret_store = PrivateRuntimeSecretStore(runtime_root / "private/secrets")
    capability = BaselineCapabilityIssuer(
        baseline_ledger_dir=runtime_root / "chaos-control/baseline",
        secret_store=secret_store,
        controller_pod_uid=controller_id,
    ).issue(
        trial_id=args.run_id,
        run_id=args.run_id,
        namespace=target["namespace"],
        target_name=target["name"],
        target_uid=target["uid"],
        summary=summary,
    )
    baseline_token = secret_store.get(capability["baseline_gate_token_ref"])
    cleanup_handle = new_cleanup_handle()
    chaos_kubeconfig = args.chaos_kubeconfig.expanduser().resolve()
    service = ChaosControlService(
        RuntimeConfig(
            execute_enabled=True,
            kubeconfig=str(chaos_kubeconfig),
            namespace_allowlist=frozenset({"otel-demo"}),
            controller_token_ref="runtime://local-controller/qualification",
            controller_pod_uid=controller_id,
            ledger_dir=runtime_root / "chaos-control/active",
            baseline_ledger_dir=runtime_root / "chaos-control/baseline",
        )
    )
    before = await service.inventory_run(
        namespace="otel-demo", kubeconfig=str(chaos_kubeconfig)
    )
    if before["global_chaosblade_count"] != 0:
        raise RuntimeError("global ChaosBlade inventory is not empty")
    created: dict[str, Any] | None = None
    cleanup: dict[str, Any] | None = None
    observed_phase: str | None = None
    try:
        created = await service.create_experiment(
            run_id=args.run_id,
            namespace="otel-demo",
            target_name=target["name"],
            target_uid=target["uid"],
            fault_type="network-delay",
            duration_seconds=5,
            intensity={"delay_ms": 10},
            kubeconfig=str(chaos_kubeconfig),
            controller_token_ref="runtime://local-controller/qualification",
            expected_controller_pod_uid=controller_id,
            baseline_gate_token=baseline_token,
            cleanup_handle=cleanup_handle,
        )
        if created.get("ok") is not True:
            raise RuntimeError("ChaosBlade create did not pass")
        name = str(created["created"]["name"])
        for _ in range(30):
            observed = await service.get_experiment(
                namespace="otel-demo",
                name=name,
                kubeconfig=str(chaos_kubeconfig),
            )
            if observed.get("found") is not True:
                raise RuntimeError("created ChaosBlade object was not observable")
            observed_phase = str(observed.get("experiment", {}).get("phase") or "")
            if observed_phase == "Running":
                break
            await asyncio.sleep(1)
        if observed_phase != "Running":
            raise RuntimeError("ChaosBlade object did not reach Running before timeout")
    finally:
        if created is not None:
            cleanup = await service.destroy_experiment(
                cleanup_handle=cleanup_handle,
                kubeconfig=str(chaos_kubeconfig),
            )
    after = await service.inventory_run(
        namespace="otel-demo", kubeconfig=str(chaos_kubeconfig)
    )
    qualified = (
        created is not None
        and observed_phase == "Running"
        and cleanup is not None
        and cleanup.get("verified_absent") is True
        and after["global_chaosblade_count"] == 0
    )
    return {
        "schema_version": "chaos-control-live-qualification.v1",
        "status": "qualified" if qualified else "failed",
        "run_id": args.run_id,
        "controller_identity_mode": "local-qualification-reference",
        "fault": {"type": "network-delay", "delay_ms": 10, "duration_seconds": 5},
        "target": {"namespace": "otel-demo", "name": target["name"], "uid": target["uid"]},
        "created": {
            "name": created.get("created", {}).get("name") if created else None,
            "phase": observed_phase,
            "target_uid_verified": created.get("safety", {}).get("target_uid_verified")
            if created
            else None,
        },
        "cleanup": {
            "verified_absent": cleanup.get("verified_absent") if cleanup else False,
        },
        "global_count_before": before["global_chaosblade_count"],
        "global_count_after": after["global_chaosblade_count"],
        "formal_run_eligible": False,
        "formal_run_blocker": "local qualification identity is not a live Controller Pod",
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not re.fullmatch(r"^[a-z0-9][a-z0-9-]{2,62}$", args.run_id):
        print("invalid qualification run id")
        return 2
    if not args.execute:
        report = {
            "schema_version": "chaos-control-live-qualification.v1",
            "status": "planned",
            "chaos_writes": False,
            "planned_fault": {
                "type": "network-delay",
                "delay_ms": 10,
                "duration_seconds": 5,
            },
        }
    else:
        try:
            report = asyncio.run(execute(args))
        except (OSError, RuntimeError, ValueError) as exc:
            report = {
                "schema_version": "chaos-control-live-qualification.v1",
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
