#!/usr/bin/env python3
"""Durable worker for queued benchmark control-plane Runs."""

from __future__ import annotations

import argparse
import json
import os
import socket
import time
from pathlib import Path

from controller.discovery_workflow import DiscoveryWorkflow, ResilienceAnalysisEngine
from controller.run_contracts import RunPhase
from controller.run_service import RunControlService
from controller.system_snapshot import (
    KubectlObservationAdapter,
    KubectlReadOnlyAdapter,
    SystemScanner,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = REPO_ROOT.parent
SOURCE_ROOT_ENV = "RESBENCH_SOURCE_ROOT"
KUBECONFIG_ENV = "RESBENCH_KUBECONFIG"
DATABASE_PATH_ENV = "BENCHMARK_CONTROL_DB_PATH"
ARTIFACTS_PATH_ENV = "BENCHMARK_RUN_ARTIFACTS_PATH"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Process at most one queued Run.")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--worker-id")
    parser.add_argument("--database", type=Path)
    parser.add_argument("--artifacts-root", type=Path)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--kubeconfig", type=Path)
    return parser


def _path(value: Path | None, env_name: str, default: Path) -> Path:
    if value is not None:
        return value.expanduser().resolve()
    raw = os.environ.get(env_name)
    return Path(raw).expanduser().resolve() if raw else default.resolve()


def _worker_id(value: str | None) -> str:
    if value:
        return value
    host = "".join(character for character in socket.gethostname().lower() if character.isalnum())
    return f"worker-{host[:24] or 'local'}-{os.getpid()}"[:64].rstrip("-")


def build_workflow(args: argparse.Namespace) -> tuple[RunControlService, DiscoveryWorkflow]:
    source_root = _path(
        args.source_root,
        SOURCE_ROOT_ENV,
        WORKSPACE_ROOT / "benchmark-sources" / "materialized",
    )
    database = _path(
        args.database,
        DATABASE_PATH_ENV,
        REPO_ROOT / "runs" / "control-plane.sqlite3",
    )
    artifacts_root = _path(
        args.artifacts_root,
        ARTIFACTS_PATH_ENV,
        REPO_ROOT / "artifacts" / "runs",
    )
    kubeconfig = args.kubeconfig
    if kubeconfig is None and os.environ.get(KUBECONFIG_ENV):
        kubeconfig = Path(os.environ[KUBECONFIG_ENV])
    runtime_adapter = KubectlReadOnlyAdapter(kubeconfig) if kubeconfig else None
    observation_adapter = KubectlObservationAdapter(kubeconfig) if kubeconfig else None
    service = RunControlService.create(
        database_path=database,
        artifacts_root=artifacts_root,
    )
    workflow = DiscoveryWorkflow(
        service,
        SystemScanner(REPO_ROOT, source_root),
        ResilienceAnalysisEngine(REPO_ROOT, source_root),
        runtime_adapter=runtime_adapter,
        observation_adapter=observation_adapter,
    )
    return service, workflow


def process_once(
    service: RunControlService,
    workflow: DiscoveryWorkflow,
    worker_id: str,
) -> dict[str, object] | None:
    lease = service.claim_next_run(
        worker_id,
        phases=(
            RunPhase.CREATED,
            RunPhase.SCANNING,
            RunPhase.MATCHING,
            RunPhase.DESIGNING,
            RunPhase.QUALIFYING,
        ),
    )
    if lease is None:
        return None
    result = workflow.process_claimed(lease)
    return {
        "run_id": result.run_id,
        "phase": result.phase.value,
        "terminal_status": result.terminal_status.value if result.terminal_status else None,
        "revision": result.revision,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 0.25 <= args.poll_seconds <= 60:
        print(json.dumps({"error": "poll-seconds must be between 0.25 and 60"}))
        return 2
    try:
        service, workflow = build_workflow(args)
        worker_id = _worker_id(args.worker_id)
        while True:
            result = process_once(service, workflow, worker_id)
            if result is not None:
                print(json.dumps(result, ensure_ascii=False), flush=True)
            if args.once:
                return 0
            time.sleep(args.poll_seconds)
    except (OSError, RuntimeError, ValueError) as exc:
        print(
            json.dumps(
                {"error_type": type(exc).__name__, "error": str(exc)[:500]},
                ensure_ascii=False,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
