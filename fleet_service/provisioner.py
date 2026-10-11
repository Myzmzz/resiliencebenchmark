"""Bring slots to the desired state, and take them away again safely.

Provisioning is idempotent: it applies the slot's manifests, installs the
trimmed system under test with the existing deployment script, and waits.
Every destructive path first passes the prefix gate and an explicit confirm,
and every call is written to the audit log whether it was a dry run or not.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from .contracts import FleetConfig, SlotPhase
from .controller_client import ControllerClient, ControllerError
from .guard import (
    FleetGuardError,
    assert_destroyable,
    assert_operable_namespace,
    replica_namespace,
    slot_id,
)
from .kube import KubeClient, KubeError
from .manifests import controller_url, owned_object_summary, slot_manifests
from .store import FleetStore


class ProvisionError(RuntimeError):
    pass


class Provisioner:
    def __init__(
        self,
        store: FleetStore,
        *,
        kube: KubeClient,
        repo_root: Path,
        kubeconfig: str | None = None,
        runtime_env_file: str | None = None,
        client_factory: Callable[[str], ControllerClient] | None = None,
        deploy_runner: Callable[[list[str]], subprocess.CompletedProcess] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        private_root: Path | None = None,
    ):
        self.private_root = Path(private_root) if private_root else Path(store.path).parent
        self.store = store
        self.kube = kube
        self.repo_root = Path(repo_root)
        self.kubeconfig = kubeconfig
        self.runtime_env_file = runtime_env_file
        self.client_factory = client_factory or (lambda url: ControllerClient(url))
        self.deploy_runner = deploy_runner or self._run_deploy
        self.sleep = sleep

    # -- deployment of the system under test -------------------------------
    def _deploy_argv(
        self, config: FleetConfig, namespace: str, *, server_dry_run: bool,
        runtime_env_file: str | None = None,
    ) -> list[str]:
        argv = [
            sys.executable,
            str(self.repo_root / "scripts/deploy_application.py"),
            "--application", config.sut_application,
            "--namespace", namespace,
            "--mode", "apply",
            "--values-profile", config.sut_values_profile,
            "--server-dry-run" if server_dry_run else "--execute",
            "--timeout", "900",
        ]
        if self.kubeconfig:
            argv.extend(["--kubeconfig", self.kubeconfig])
        env_file = runtime_env_file or self.runtime_env_file
        if env_file:
            argv.extend(["--runtime-env-file", env_file])
        return argv

    @staticmethod
    def _run_deploy(argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(argv, check=False, capture_output=True, text=True, timeout=1500)

    @contextmanager
    def _private_runtime_env(self, config: FleetConfig):
        """A mode-0600 copy of the runtime env file for one deploy call.

        The Secret is mounted 0440 because fsGroup adds group read, and
        deploy_application.py refuses a runtime env file any group can read.
        The Controller's own reset path makes the same private copy.
        """
        if not self.runtime_env_file:
            yield None
            return
        self.private_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="fleet-deploy-", dir=self.private_root) as raw:
            private = Path(raw) / f"{config.sut_application}.env"
            shutil.copyfile(self.runtime_env_file, private)
            private.chmod(0o600)
            yield str(private)

    def deploy_sut(self, config: FleetConfig, namespace: str, *, dry_run: bool) -> dict[str, Any]:
        assert_operable_namespace(config.namespace_prefix, namespace)
        if dry_run and not self.kube.namespace_exists(namespace):
            # A server-side dry run creates nothing, so every write into a
            # namespace that does not exist yet is refused as NotFound. Say so
            # instead of reporting a failure the real install would not have.
            return {
                "namespace": namespace,
                "mode": "server-dry-run",
                "skipped": True,
                "reason": "namespace does not exist yet; a server dry run cannot create it",
                "command": " ".join(self._deploy_argv(config, namespace, server_dry_run=True)),
            }
        with self._private_runtime_env(config) as env_file:
            argv = self._deploy_argv(config, namespace, server_dry_run=dry_run, runtime_env_file=env_file)
            completed = self.deploy_runner(argv)
        report = {
            "namespace": namespace,
            "mode": "server-dry-run" if dry_run else "execute",
            "exit_code": completed.returncode,
        }
        if completed.returncode:
            report["stderr_excerpt"] = (completed.stderr or completed.stdout or "")[-800:]
            raise ProvisionError(f"deploy_application.py failed for {namespace}: {report['stderr_excerpt']}")
        return report

    def _partition_for_dry_run(self, objects: list[dict[str, Any]]) -> tuple[list, list]:
        """Split objects into those a server dry run can check and those it cannot."""
        known: dict[str, bool] = {}
        simulated: list[dict[str, Any]] = []
        deferred: list[dict[str, Any]] = []
        for item in objects:
            namespace = str((item.get("metadata") or {}).get("namespace") or "")
            if not namespace:
                simulated.append(item)
                continue
            if namespace not in known:
                known[namespace] = self.kube.namespace_exists(namespace)
            (simulated if known[namespace] else deferred).append(item)
        return simulated, deferred

    # -- slots -------------------------------------------------------------
    def provision_slot(self, config: FleetConfig, index: int, *, dry_run: bool,
                       actor: str = "api") -> dict[str, Any]:
        namespace = replica_namespace(config.namespace_prefix, index)
        assert_operable_namespace(config.namespace_prefix, namespace)
        endpoints = list(config.api_server_endpoints) or self.kube.api_server_endpoints()
        objects = slot_manifests(config, index, endpoints)
        self.store.audit_event(
            action="provision_slot", namespace=namespace, dry_run=dry_run, actor=actor,
            detail={"slot_id": slot_id(index), "objects": len(objects)},
        )
        simulated, deferred = self._partition_for_dry_run(objects) if dry_run else (objects, [])
        applied = self.kube.apply(simulated, dry_run=dry_run)
        sut = self.deploy_sut(config, namespace, dry_run=dry_run)
        record = {
            "slot_id": slot_id(index),
            "index": index,
            "namespace": namespace,
            "controller_url": controller_url(config, index),
            "dry_run": dry_run,
            "objects": owned_object_summary(objects),
            "applied": applied,
            "system_under_test": sut,
        }
        if deferred:
            record["not_simulated"] = {
                "reason": "a server dry run creates no namespace, so objects inside a "
                          "namespace that does not exist yet cannot be validated",
                "objects": owned_object_summary(deferred),
            }
        if not dry_run:
            self.store.upsert_slot(
                slot_id=slot_id(index), index=index, namespace=namespace,
                controller_url=controller_url(config, index),
                phase=SlotPhase.PROVISIONING.value, detail={"applied_at": None},
            )
        return record

    def wait_ready(self, config: FleetConfig, index: int, *, timeout_seconds: int = 900) -> dict[str, Any]:
        namespace = replica_namespace(config.namespace_prefix, index)
        name = f"resbench-stage2-{slot_id(index)}"
        deadline = time.monotonic() + timeout_seconds
        status: dict[str, Any] = {}
        while time.monotonic() < deadline:
            controller = self.kube.deployment_ready(name, config.control_namespace)
            application = self.kube.namespace_deployments_ready(namespace)
            status = {"controller": controller, "system_under_test": application}
            if controller.get("ready") and application.get("ready"):
                self.store.set_slot_phase(slot_id(index), SlotPhase.READY.value, status)
                return {"ready": True, **status}
            self.sleep(10)
        self.store.set_slot_phase(slot_id(index), SlotPhase.FAILED.value, status)
        return {"ready": False, **status}

    def refresh_slot(self, config: FleetConfig, slot: Mapping[str, Any]) -> dict[str, Any]:
        """Read-only phase refresh; a Busy slot is one with a trial in flight."""
        name = f"resbench-stage2-{slot['slot_id']}"
        controller = self.kube.deployment_ready(name, config.control_namespace)
        application = self.kube.namespace_deployments_ready(str(slot["namespace"]))
        busy = slot["slot_id"] in self.store.busy_slot_ids()
        if slot["phase"] == SlotPhase.DRAINING.value:
            phase = SlotPhase.DRAINING.value
        elif not controller.get("ready") or not application.get("ready"):
            phase = SlotPhase.FAILED.value
        else:
            phase = SlotPhase.BUSY.value if busy else SlotPhase.READY.value
        detail = {"controller": controller, "system_under_test": application}
        self.store.set_slot_phase(str(slot["slot_id"]), phase, detail)
        return {**dict(slot), "phase": phase, "detail": detail}

    def drain_slot(self, slot_id_value: str) -> dict[str, Any]:
        self.store.set_slot_phase(slot_id_value, SlotPhase.DRAINING.value)
        return {"slot_id": slot_id_value, "phase": SlotPhase.DRAINING.value}

    def reset_slot(self, config: FleetConfig, slot: Mapping[str, Any]) -> dict[str, Any]:
        """Reset a replica through its own Controller's existing reset path."""
        client = self.client_factory(str(slot["controller_url"]))
        try:
            tasks = client.tasks()
        except ControllerError as exc:
            raise ProvisionError(f"slot {slot['slot_id']} is unreachable: {exc}") from exc
        rows = tasks.get("tasks") if isinstance(tasks, Mapping) else None
        latest = None
        for row in rows or []:
            if isinstance(row, Mapping) and row.get("task_id"):
                latest = row
        if latest is None:
            return {"slot_id": slot["slot_id"], "reset": False, "reason": "no task to reset through"}
        result = client.reset_environment(str(latest["task_id"]))
        self.store.audit_event(
            action="reset_slot", namespace=str(slot["namespace"]), dry_run=False, actor="api",
            detail={"slot_id": slot["slot_id"], "task_id": latest.get("task_id")},
        )
        return {"slot_id": slot["slot_id"], "reset": True, "task_id": latest.get("task_id"), "result": result}

    # -- reclaim -----------------------------------------------------------
    def delete_slot(
        self, config: FleetConfig, slot: Mapping[str, Any], *, confirm: str | None,
        dry_run: bool, actor: str = "api",
    ) -> dict[str, Any]:
        namespace = str(slot["namespace"])
        assert_destroyable(config.namespace_prefix, namespace, confirm)
        name = f"resbench-stage2-{slot['slot_id']}"
        self.store.audit_event(
            action="delete_slot", namespace=namespace, dry_run=dry_run, actor=actor,
            detail={"slot_id": slot["slot_id"], "confirm": confirm},
        )
        removed = []
        for kind, object_name in (("deployment", name), ("service", name), ("persistentvolumeclaim", f"{name}-data")):
            self.kube.delete(kind, object_name, namespace=config.control_namespace, dry_run=dry_run)
            removed.append(f"{kind}/{object_name}")
        self.kube.delete_namespace(namespace, dry_run=dry_run)
        removed.append(f"namespace/{namespace}")
        if not dry_run:
            self.store.delete_slot(str(slot["slot_id"]))
        return {"slot_id": slot["slot_id"], "namespace": namespace, "dry_run": dry_run, "removed": removed}
