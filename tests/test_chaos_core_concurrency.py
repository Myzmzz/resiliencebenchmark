"""Process/thread contention tests for the Controller-private chaos ledger lock."""

from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcp_servers.chaos_core.backends.chaosblade import _manifest, _matcher_value
from mcp_servers.chaos_core.contracts import (
    FAULT_TYPE_LABEL, NAMESPACE_LABEL, OWNER_LABEL, OWNER_VALUE, RUN_ID_LABEL,
    TARGET_UID_LABEL, ChaosControlError, ExperimentRecord, RuntimeConfig,
)
from mcp_servers.chaos_core.ledger import _ledger_file_lock
from mcp_servers.chaos_core.service import ControlledExecutionService


RUN_ID = "trial-concurrency-001"
HANDLE = "cleanup-concurrency-001"
NAMESPACE = "otel-demo"
TARGET = "cart-a"
UID = "uid-actual"
TOKEN = "baseline-concurrency-token"
KUBECONFIG = "/tmp/concurrency.kubeconfig"
CLEANUP_KUBECONFIG = "/tmp/concurrency-finalizer.kubeconfig"


def test_core_layout_keeps_backends_and_ledger_out_of_service_imports() -> None:
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import mcp_servers.chaos_core.backends.chaos_mesh; "
                "raise SystemExit('mcp_servers.chaos_core.service' in sys.modules)"
            ),
        ],
        check=False,
    )
    assert probe.returncode == 0

    import mcp_servers.chaos_core.backends.chaos_mesh as chaos_mesh_backend
    import mcp_servers.chaos_core.backends.chaosblade as chaosblade_backend
    import mcp_servers.chaos_core.contracts as contracts
    import mcp_servers.chaos_core.ledger as ledger
    import mcp_servers.chaos_core.service as service

    assert service.RuntimeConfig is contracts.RuntimeConfig
    assert service.KubectlChaosBackend is chaosblade_backend.KubectlChaosBackend
    assert service._ledger_file_lock is ledger._ledger_file_lock
    assert not hasattr(service, "_LEDGER_LOCK_FILE")
    assert chaos_mesh_backend.ChaosControlError is contracts.ChaosControlError


class SharedBackend:
    """Pickle-safe Manager-backed fake; no Kubernetes is contacted."""

    def __init__(self, records, creates, deletes, *, delay_seconds: float = 0.04) -> None:
        self.records = records
        self.creates = creates
        self.deletes = deletes
        self.delay_seconds = delay_seconds

    async def list_experiments(self, _kubeconfig: str, namespace: str | None = None):
        values = list(self.records.values())
        return values if namespace is None else [item for item in values if item.namespace == namespace]

    async def get_experiment(self, namespace: str, name: str, _kubeconfig: str):
        return self.records.get((namespace, name))

    async def get_pod_uid(self, namespace: str, name: str, _kubeconfig: str):
        return UID if (namespace, name) == (NAMESPACE, TARGET) else None

    async def create_experiment(self, manifest, _kubeconfig: str):
        await asyncio.sleep(self.delay_seconds)
        self.creates.append(manifest)
        labels = manifest["metadata"]["labels"]
        experiment = manifest["spec"]["experiments"][0]
        record = ExperimentRecord(
            name=manifest["metadata"]["name"], namespace=labels[NAMESPACE_LABEL],
            run_id=labels[RUN_ID_LABEL], target_name=_matcher_value(experiment, "names"),
            target_uid=labels[TARGET_UID_LABEL], fault_type=labels[FAULT_TYPE_LABEL],
            phase="Running", owner=labels[OWNER_LABEL], labels=dict(labels), raw=dict(manifest),
        )
        self.records[(record.namespace, record.name)] = record
        return record

    async def delete_experiment(self, namespace: str, name: str, _kubeconfig: str):
        self.deletes.append((namespace, name))
        self.records.pop((namespace, name), None)

    def render_manifest(self, name, action):
        return _manifest(name, action)

    async def prepare_target_fence(self, *_args):
        return None

    async def clear_target_fence(self, *_args):
        return None


def _config(root: Path) -> RuntimeConfig:
    return RuntimeConfig(
        execute_enabled=True, kubeconfig=KUBECONFIG, cleanup_kubeconfig=CLEANUP_KUBECONFIG, namespace_allowlist=frozenset({NAMESPACE}),
        controller_token_ref="controller-token", controller_pod_uid="controller-uid",
        allowed_fault_types=frozenset({"network-delay"}), decision_policy="agent_delegated",
        ledger_dir=root / "ledger", baseline_ledger_dir=root / "baseline",
    )


def _write_baseline(root: Path) -> None:
    baseline = root / "baseline"
    baseline.mkdir(mode=0o700, exist_ok=True)
    payload = {
        "passed": True, "run_id": RUN_ID, "namespace": NAMESPACE,
        "target_name": TARGET, "target_uid": UID, "controller_pod_uid": "controller-uid",
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    }
    path = baseline / f"{hashlib.sha256(TOKEN.encode()).hexdigest()}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def _create_kwargs() -> dict[str, object]:
    return {
        "run_id": RUN_ID, "namespace": NAMESPACE, "target_name": TARGET,
        "target_uid": UID, "fault_type": "network-delay", "duration_seconds": 60,
        "intensity": {"delay_ms": 250}, "kubeconfig": KUBECONFIG,
        "controller_token_ref": "controller-token", "expected_controller_pod_uid": "controller-uid",
        "baseline_gate_token": TOKEN, "cleanup_handle": HANDLE,
    }


def _process_create(root_text: str, records, creates, deletes, executor_id: str, output) -> None:
    root = Path(root_text)
    service = ControlledExecutionService(_config(root), SharedBackend(records, creates, deletes), executor_id=executor_id)
    try:
        result = asyncio.run(service.create_experiment(**_create_kwargs()))
        output.put({"ok": result.get("ok") is True})
    except ChaosControlError as exc:
        output.put({"ok": False, "code": exc.code})


def test_two_process_executors_create_once_with_shared_private_ledger(tmp_path: Path) -> None:
    _write_baseline(tmp_path)
    context = multiprocessing.get_context("spawn")
    with context.Manager() as manager:
        records, creates, deletes = manager.dict(), manager.list(), manager.list()
        output = context.Queue()
        workers = [
            context.Process(target=_process_create, args=(str(tmp_path), records, creates, deletes, executor, output))
            for executor in ("chaosblade", "chaos_mesh")
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=15)
            assert worker.exitcode == 0
        results = [output.get(timeout=3) for _ in workers]
        assert sum(item["ok"] for item in results) == 1
        assert len(creates) == 1
        assert any(item.get("code") in {"CLEANUP_HANDLE_ALREADY_EXISTS", "CLEANUP_HANDLE_ALREADY_USED", "EXECUTOR_CONFLICT"} for item in results if not item["ok"]), results


def test_cross_thread_asyncio_runs_preserve_first_cleanup_principal_and_fault_window(tmp_path: Path) -> None:
    _write_baseline(tmp_path)
    context = multiprocessing.get_context("spawn")
    with context.Manager() as manager:
        records, creates, deletes = manager.dict(), manager.list(), manager.list()
        first = ControlledExecutionService(_config(tmp_path), SharedBackend(records, creates, deletes))
        second = ControlledExecutionService(_config(tmp_path), SharedBackend(records, creates, deletes))
        assert asyncio.run(first.create_experiment(**_create_kwargs()))["ok"] is True
        errors: list[BaseException] = []

        def run_destroy(service: ControlledExecutionService, principal: str) -> None:
            try:
                asyncio.run(service.destroy_experiment(cleanup_handle=HANDLE, kubeconfig=KUBECONFIG, principal=principal))
            except BaseException as exc:  # test collects failures from independent loops
                errors.append(exc)

        threads = [
            threading.Thread(target=run_destroy, args=(first, "AGENT_MCP")),
            threading.Thread(target=run_destroy, args=(second, "CONTROLLER_FALLBACK")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            assert not thread.is_alive()
        assert not errors

        ledger_path = tmp_path / "ledger" / f"{HANDLE}.json"
        ledger = json.loads(ledger_path.read_text())
        first_principal = ledger["cleanup_principal"]
        assert first_principal in {"AGENT_MCP", "CONTROLLER_FALLBACK"}
        # A later TTL sweep and read-status paths cannot overwrite the actor
        # that actually removed the resource, and they retain fault timestamps.
        asyncio.run(second.cleanup_expired_leases(now=datetime.now(timezone.utc) + timedelta(hours=1)))
        asyncio.run(first.operation_status(cleanup_handle=HANDLE, kubeconfig=KUBECONFIG))
        asyncio.run(second.recovery_status(cleanup_handle=HANDLE, kubeconfig=KUBECONFIG))
        final = json.loads(ledger_path.read_text())
        assert final["cleanup_principal"] == first_principal
        assert final.get("started_at") and final.get("ended_at")


def test_cancelled_async_waiter_releases_its_private_file_lease(tmp_path: Path) -> None:
    directory = tmp_path / "ledger"

    async def scenario() -> None:
        entered = asyncio.Event()

        async def holder() -> None:
            async with _ledger_file_lock(directory):
                entered.set()
                await asyncio.sleep(30)

        task = asyncio.create_task(holder())
        await entered.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        async with _ledger_file_lock(directory):
            assert (directory / ".resbench-chaos-mutation.lock").is_file()

    asyncio.run(scenario())


def test_cancelled_waiter_then_event_loop_close_cannot_strand_file_lock(tmp_path: Path) -> None:
    directory = tmp_path / "ledger"

    async def cancelled_waiter_loop() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def holder() -> None:
            async with _ledger_file_lock(directory):
                entered.set()
                await release.wait()

        async def waiter() -> None:
            async with _ledger_file_lock(directory):
                raise AssertionError("cancelled waiter unexpectedly acquired the lock")

        holder_task = asyncio.create_task(holder())
        await entered.wait()
        waiter_task = asyncio.create_task(waiter())
        await asyncio.sleep(0.02)
        waiter_task.cancel()
        try:
            await waiter_task
        except asyncio.CancelledError:
            pass
        release.set()
        await holder_task

    # asyncio.run closes this loop immediately after the cancelled waiter.
    asyncio.run(cancelled_waiter_loop())

    async def third_party() -> None:
        async with _ledger_file_lock(directory):
            assert True

    asyncio.run(third_party())
