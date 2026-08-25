#!/usr/bin/env python3
"""Execute approved benchmark Runs with live per-trial safety and Oracle gates."""

from __future__ import annotations

import argparse
import atexit
import json
import os
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from controller.disturbance_runtime import RuntimeDisturbanceInjectorFactory
from controller.execution_workflow import ExecutionWorkflow
from controller.residual_cleanup import cleanup_run_workloads
from controller.run_contracts import RunPhase
from controller.run_service import RunControlService
from controller.runtime_multi_level import RuntimeMultiLevelRunner
from controller.runtime_secrets import (
    BaselineCapabilityIssuer,
    PrivateRuntimeSecretStore,
)
from controller.system_snapshot import (
    KubectlObservationAdapter,
    KubectlReadOnlyAdapter,
    SnapshotStatus,
    SystemScanner,
)
from controller.trial_finalization import McpMainFaultControl, PerTrialFinalizer
from controller.trial_preparation import (
    EngineeringOtelBaselineMeasurer,
    FormalOtelBaselineMeasurer,
    LiveResetVerifier,
    LiveTargetResolver,
    OtelExperimentWorkloadSession,
    PerTrialPreparer,
    TrialRuntimeContextStore,
)
from disturbances.file_telemetry_interceptor import FileTelemetryRuleClient
from disturbances.kubernetes_runtime import KubernetesDisturbanceClient
from disturbances.types import DisturbanceType
from evaluator.runtime_oracle import (
    HarnessArtifactAgentResultLoader,
    KubectlPrometheusFaultEffectObserver,
    RuntimeLevelEvaluator,
    RuntimeRunOracle,
)
from harness.live_runner import LiveHarnessTrialRunner
from scripts.reset_episode import SubprocessCommandRunner

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = REPO_ROOT.parent

REQUIRED_ENV = (
    "RESBENCH_KUBECONFIG",
    "RESBENCH_LOCUST_WORKLOAD_IMAGE",
    "RESBENCH_CHAOS_BASELINE_LEDGER_DIR",
    "RESBENCH_PRIVATE_RUNTIME_ROOT",
    "RESBENCH_TELEMETRY_DISTURBANCE_DIR",
    "RESBENCH_CHAOS_CONTROLLER_TOKEN_REF",
    "RESBENCH_CHAOS_CONTROLLER_POD_UID",
    "RESBENCH_CHAOS_CONTROL_MCP_URL",
    "RESBENCH_MCP_TOKEN",
    "RESBENCH_K8S_MCP_URL",
    "RESBENCH_TELEMETRY_MCP_URL",
    "RESBENCH_SOURCE_MCP_URL",
)
MODEL_GATEWAY_ENV = ("RESBENCH_LLM_BASE_URL", "RESBENCH_LLM_API_KEY")


@dataclass(frozen=True)
class ExecutionTimingPolicy:
    mode: str
    experiment_seconds: int
    formal_baseline_report: Path | None = None

    @property
    def formal_run_eligible(self) -> bool:
        return self.mode == "formal"


def execution_timing_policy(profile_id: str) -> ExecutionTimingPolicy:
    if profile_id.startswith("standard-"):
        return ExecutionTimingPolicy(mode="formal", experiment_seconds=900)
    if profile_id.startswith("engineering-"):
        report = Path(
            os.environ.get(
                "RESBENCH_FORMAL_BASELINE_REPORT",
                REPO_ROOT / "runs/local-control-stack/formal-baseline-preflight.json",
            )
        ).expanduser().resolve()
        return ExecutionTimingPolicy(
            mode="engineering",
            experiment_seconds=360,
            formal_baseline_report=report,
        )
    raise RuntimeError(f"unsupported execution timing profile: {profile_id}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--worker-id", default="execution-worker-local")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--artifacts-root", type=Path)
    return parser


def _required_environment() -> dict[str, str]:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    codex_auth = os.environ.get("RESBENCH_CODEX_AUTH_FILE", "")
    if not codex_auth:
        missing.extend(name for name in MODEL_GATEWAY_ENV if not os.environ.get(name))
    if missing:
        raise RuntimeError(
            "execution worker is disabled; missing runtime settings: "
            + ", ".join(missing)
        )
    if codex_auth:
        auth_path = Path(codex_auth).expanduser().resolve()
        if not auth_path.is_file() or auth_path.is_symlink() or auth_path.stat().st_mode & 0o077:
            raise RuntimeError("RESBENCH_CODEX_AUTH_FILE must be a private regular file")
    keys = [*REQUIRED_ENV, *MODEL_GATEWAY_ENV, "RESBENCH_CODEX_AUTH_FILE"]
    return {name: os.environ[name] for name in keys if os.environ.get(name)}


def _paths(args: argparse.Namespace) -> tuple[Path, Path]:
    database = (
        args.database
        or Path(os.environ.get("BENCHMARK_CONTROL_DB_PATH", REPO_ROOT / "runs/control-plane.sqlite3"))
    ).expanduser().resolve()
    artifacts = (
        args.artifacts_root
        or Path(os.environ.get("BENCHMARK_RUN_ARTIFACTS_PATH", REPO_ROOT / "artifacts/runs"))
    ).expanduser().resolve()
    return database, artifacts


def process_once(args: argparse.Namespace) -> dict[str, Any] | None:
    runtime = _required_environment()
    database, artifacts_root = _paths(args)
    service = RunControlService.create(
        database_path=database,
        artifacts_root=artifacts_root,
    )
    lease = service.claim_next_run(
        args.worker_id,
        phases=(
            RunPhase.BASELINING,
            RunPhase.EXECUTING,
            RunPhase.RECOVERING,
            RunPhase.EVALUATING,
            RunPhase.SCORING,
            RunPhase.CLEANING_UP,
        ),
        ttl_seconds=3600,
    )
    if lease is None:
        return None
    atexit.register(service.release_worker_lease, lease.run_id, lease.worker_id)
    heartbeat_stop = threading.Event()

    def heartbeat() -> None:
        while not heartbeat_stop.wait(60):
            try:
                service.renew_worker_lease(
                    lease.run_id,
                    lease.worker_id,
                    ttl_seconds=3600,
                )
                mutation_lease = service.store.get_mutation_lease()
                if mutation_lease is not None and mutation_lease.run_id == lease.run_id:
                    service.acquire_mutation_lease(
                        lease.run_id,
                        ttl_seconds=600,
                    )
            except (RuntimeError, ValueError):
                return

    heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
    heartbeat_thread.start()
    record = service.get_run(lease.run_id)
    if record.spec.harness.harness_id != "codex" and "RESBENCH_CODEX_AUTH_FILE" in runtime:
        service.release_worker_lease(lease.run_id, lease.worker_id)
        raise RuntimeError("ChatGPT OAuth runtime currently supports only the Codex harness")
    kubeconfig = Path(runtime["RESBENCH_KUBECONFIG"]).expanduser().resolve()
    private_root = Path(runtime["RESBENCH_PRIVATE_RUNTIME_ROOT"]).expanduser().resolve()
    source_root = Path(
        os.environ.get(
            "RESBENCH_SOURCE_ROOT",
            WORKSPACE_ROOT / "benchmark-sources/materialized",
        )
    ).expanduser().resolve()
    runtime_adapter = KubectlReadOnlyAdapter(kubeconfig)
    observation_adapter = KubectlObservationAdapter(kubeconfig)
    scanner = SystemScanner(REPO_ROOT, source_root)
    if record.phase is RunPhase.CLEANING_UP:
        cleanup = service.read_json_artifact(
            record.run_id, ExecutionWorkflow.CLEANUP_REF
        )
        if not isinstance(cleanup, Mapping):
            workload_cleanup = cleanup_run_workloads(
                kubeconfig=kubeconfig,
                run_id=record.run_id,
                runner=SubprocessCommandRunner(),
            )
            runtime_snapshot = runtime_adapter.scan(record.spec.scan.namespace)
            observer_snapshot = observation_adapter.scan(record.spec.scan.namespace)
            cleanup = {
                "verified": (
                    runtime_snapshot.status is SnapshotStatus.QUALIFIED
                    and runtime_snapshot.chaosblade_global_count == 0
                    and observer_snapshot.status is SnapshotStatus.QUALIFIED
                    and workload_cleanup["verified"] is True
                ),
                "workload_cleanup": workload_cleanup,
                "evidence_refs": [
                    f"runtime://{record.run_id}/post-run-snapshot",
                    *workload_cleanup["evidence_refs"],
                ],
            }
            service.record_json_artifact(
                record.run_id,
                artifact_ref=ExecutionWorkflow.CLEANUP_REF,
                payload=cleanup,
                event_type="CLEANUP_VERIFICATION_RECORDED",
            )
        finished = service.finish_cleanup(
            record.run_id,
            verified=cleanup.get("verified") is True,
            detail={"evidence_refs": cleanup.get("evidence_refs", [])},
        )
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=2)
        service.release_worker_lease(lease.run_id, lease.worker_id)
        return {
            "run_id": finished.run_id,
            "phase": finished.phase.value,
            "terminal_status": (
                finished.terminal_status.value if finished.terminal_status else None
            ),
            "revision": finished.revision,
        }
    contexts = TrialRuntimeContextStore(private_root / "trial-contexts")
    secret_store = PrivateRuntimeSecretStore(private_root / "secrets")
    multi_episode = service.read_json_artifact(
        record.run_id, ExecutionWorkflow.MULTI_LEVEL_REF
    )
    internal_plan = service.read_json_artifact(
        record.run_id, "internal/locked-episode-plan.json"
    )
    if not isinstance(multi_episode, Mapping) or not isinstance(internal_plan, Mapping):
        service.release_worker_lease(lease.run_id, lease.worker_id)
        raise TypeError("approved Run has no locked internal and multi-level Episode")
    base_task = multi_episode["base_task"]
    target = base_task["target"]
    main_fault = base_task["main_fault"]
    timing = execution_timing_policy(record.spec.progression.profile_id)
    if timing.formal_run_eligible:
        baseline_measurer = FormalOtelBaselineMeasurer(
            kubeconfig=kubeconfig,
            workload_image=runtime["RESBENCH_LOCUST_WORKLOAD_IMAGE"],
            environment=os.environ,
        )
    else:
        assert timing.formal_baseline_report is not None
        baseline_measurer = EngineeringOtelBaselineMeasurer(
            kubeconfig=kubeconfig,
            workload_image=runtime["RESBENCH_LOCUST_WORKLOAD_IMAGE"],
            formal_baseline_report=timing.formal_baseline_report,
            smoke_duration_seconds=60,
            environment=os.environ,
        )
    experiment_workload = OtelExperimentWorkloadSession(
        kubeconfig=kubeconfig,
        workload_image=runtime["RESBENCH_LOCUST_WORKLOAD_IMAGE"],
        duration_seconds=timing.experiment_seconds,
        environment=os.environ,
    )
    preparer = PerTrialPreparer(
        reset_verifier=LiveResetVerifier(runtime_adapter, str(target["namespace"])),
        target_resolver=LiveTargetResolver(
            runtime_adapter,
            namespace=str(target["namespace"]),
            component=str(target["component"]),
        ),
        baseline_measurer=baseline_measurer,
        capability_issuer=BaselineCapabilityIssuer(
            baseline_ledger_dir=Path(runtime["RESBENCH_CHAOS_BASELINE_LEDGER_DIR"]),
            secret_store=secret_store,
            controller_pod_uid=runtime["RESBENCH_CHAOS_CONTROLLER_POD_UID"],
        ),
        context_store=contexts,
        experiment_workload_session=experiment_workload,
    )

    def verify_business_recovery(ticket, level, trial_report):
        context = contexts.load(ticket.trial_id)
        experiment_result = experiment_workload.finish(ticket)
        result = baseline_measurer(ticket, level, context["target"])
        return {
            "verified": (
                experiment_result.get("summary", {}).get("qualified") is True
                and result.get("qualified") is True
            ),
            "measurement_mode": timing.mode,
            "formal_run_eligible": timing.formal_run_eligible,
            "experiment_summary": experiment_result.get("summary"),
            "summary": result.get("summary"),
            "fresh_smoke_summary": result.get("fresh_smoke_summary"),
            "evidence_refs": [
                *experiment_result.get("evidence_refs", []),
                *result.get("evidence_refs", []),
            ],
        }

    finalizer = PerTrialFinalizer(
        context_store=contexts,
        main_fault_control=McpMainFaultControl(
            url=runtime["RESBENCH_CHAOS_CONTROL_MCP_URL"],
            token=runtime["RESBENCH_MCP_TOKEN"],
        ),
        business_recovery_verifier=verify_business_recovery,
    )
    harness_root = artifacts_root / record.run_id / "harness"
    trial_runner = LiveHarnessTrialRunner(
        repo_root=REPO_ROOT,
        public_episode_file=artifacts_root
        / record.run_id
        / "locked/public-episode.json",
        harness_name=record.spec.harness.harness_id,
        model_alias=record.spec.harness.model_alias,
        artifact_root=harness_root,
        timeout_seconds=1800,
        parent_env=os.environ,
        trial_context_store=contexts,
        secret_store=secret_store,
        controller_token_ref=runtime["RESBENCH_CHAOS_CONTROLLER_TOKEN_REF"],
        controller_pod_uid=runtime["RESBENCH_CHAOS_CONTROLLER_POD_UID"],
        main_fault=main_fault,
    )
    injector_factory = RuntimeDisturbanceInjectorFactory(
        context_store=contexts,
        kubernetes_client=KubernetesDisturbanceClient.from_kubeconfig(kubeconfig),
        telemetry_rule_client=FileTelemetryRuleClient(
            Path(runtime["RESBENCH_TELEMETRY_DISTURBANCE_DIR"])
        ),
        controller_record_root=artifacts_root / record.run_id / "controller-records",
        namespace_allowlist={str(target["namespace"])},
        allowed_types={DisturbanceType.TARGET_DRIFT, DisturbanceType.METRIC_DATA_GAP},
    )
    source_refs = [
        f"source://{item.get('path')}:{item.get('line')}"
        for item in internal_plan.get("design_basis", {}).get("evidence_basis", [])
        if isinstance(item, Mapping) and item.get("path")
    ]
    raw_level_evaluator = RuntimeLevelEvaluator(
        episode_id=str(multi_episode["episode_id"]),
        expected_diagnosis_terms=["timeout", "deadline", "abortsignal", "abort signal"],
        source_evidence_refs=source_refs,
        effect_observer=KubectlPrometheusFaultEffectObserver(kubeconfig),
        agent_result_loader=HarnessArtifactAgentResultLoader(harness_root),
    )

    def level_evaluator(ticket, level, trial_report, controller_records):
        result = dict(
            raw_level_evaluator(ticket, level, trial_report, controller_records)
        )
        ref = f"evaluation/levels/{ticket.trial_id.lower()}.json"
        result["result_ref"] = ref
        service.record_json_artifact(
            record.run_id,
            artifact_ref=ref,
            payload=result,
            event_type="LEVEL_RESULT_RECORDED",
        )
        return result

    multi_runner = RuntimeMultiLevelRunner(
        repo_root=REPO_ROOT,
        run_artifacts_root=artifacts_root,
        private_state_root=private_root,
        trial_runner=trial_runner,
        level_evaluator=level_evaluator,
        injector_factory=injector_factory,
        trial_preparer=preparer,
        trial_finalizer=finalizer,
        continue_after_failure=not timing.formal_run_eligible,
    )

    def entry_gate(current, episode):
        snapshot = scanner.scan(
            current.run_id,
            current.spec,
            runtime_adapter=runtime_adapter,
            observation_adapter=observation_adapter,
        )
        qualified = (
            snapshot.runtime.status is SnapshotStatus.QUALIFIED
            and snapshot.observers.status is SnapshotStatus.QUALIFIED
        )
        return {
            "qualified": qualified,
            "snapshot_id": snapshot.snapshot_id,
            "execution_profile": current.spec.progression.profile_id,
            "measurement_mode": timing.mode,
            "formal_run_eligible": timing.formal_run_eligible,
            "evidence_refs": ["stages/system-snapshot.json"],
        }

    def verify_post_run(current, execution):
        workload_cleanup = cleanup_run_workloads(
            kubeconfig=kubeconfig,
            run_id=current.run_id,
            runner=SubprocessCommandRunner(),
        )
        snapshot = scanner.scan(
            current.run_id,
            current.spec,
            runtime_adapter=runtime_adapter,
            observation_adapter=observation_adapter,
        )
        verified = (
            snapshot.runtime.status is SnapshotStatus.QUALIFIED
            and snapshot.runtime.chaosblade_global_count == 0
            and snapshot.observers.status is SnapshotStatus.QUALIFIED
            and workload_cleanup["verified"] is True
        )
        return {
            "verified": verified,
            "workload_cleanup": workload_cleanup,
            "evidence_refs": [
                f"runtime://{current.run_id}/post-run-snapshot",
                *workload_cleanup["evidence_refs"],
            ],
        }

    workflow = ExecutionWorkflow(
        service,
        baseline_runner=entry_gate,
        multi_level_runner=multi_runner,
        recovery_verifier=verify_post_run,
        oracle=RuntimeRunOracle(),
        cleanup_verifier=lambda current: verify_post_run(current, {}),
    )
    finished = workflow.process_claimed(lease)
    heartbeat_stop.set()
    heartbeat_thread.join(timeout=2)
    return {
        "run_id": finished.run_id,
        "phase": finished.phase.value,
        "terminal_status": (
            finished.terminal_status.value if finished.terminal_status else None
        ),
        "revision": finished.revision,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 0.25 <= args.poll_seconds <= 60:
        print(json.dumps({"error": "poll-seconds must be between 0.25 and 60"}))
        return 2
    while True:
        try:
            result = process_once(args)
        except (OSError, RuntimeError, ValueError) as exc:
            print(
                json.dumps(
                    {"error_type": type(exc).__name__, "error": str(exc)[:1000]},
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return 2
        if result is not None:
            print(json.dumps(result, ensure_ascii=False), flush=True)
        if args.once:
            return 0
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
