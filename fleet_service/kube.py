"""kubectl wrapper for the Fleet's own cluster writes.

Every mutating call has a server-side dry-run form, and every namespace that
reaches a delete goes through ``fleet_service.guard`` first.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

import yaml


class KubeError(RuntimeError):
    """A kubectl invocation failed; the message carries its diagnostic tail."""


class CommandRunner(Protocol):
    def run(self, argv: Sequence[str], *, stdin: str | None = None, timeout: int = 120): ...


class SubprocessRunner:
    def run(self, argv: Sequence[str], *, stdin: str | None = None, timeout: int = 120):
        return subprocess.run(
            list(argv), input=stdin, check=False, capture_output=True, text=True,
            shell=False, timeout=timeout,
        )


class KubeClient:
    def __init__(self, *, kubeconfig: str | None = None, runner: CommandRunner | None = None):
        self.kubeconfig = kubeconfig
        self.runner = runner or SubprocessRunner()

    def _base(self) -> list[str]:
        argv = ["kubectl", "--request-timeout=60s"]
        if self.kubeconfig:
            argv.extend(["--kubeconfig", self.kubeconfig])
        return argv

    def _run(self, argv: Sequence[str], *, stdin: str | None = None, timeout: int = 120) -> str:
        completed = self.runner.run(self._base() + list(argv), stdin=stdin, timeout=timeout)
        if completed.returncode:
            detail = (completed.stderr or completed.stdout or "").strip().replace("\n", " ")[-600:]
            raise KubeError(f"kubectl {' '.join(argv[:3])} failed: {detail}")
        return completed.stdout

    def apply(self, objects: Sequence[Mapping[str, Any]], *, dry_run: bool = False) -> list[str]:
        """Apply a list of objects; with ``dry_run`` nothing is persisted."""
        if not objects:
            return []
        document = yaml.safe_dump_all([dict(item) for item in objects], sort_keys=False)
        argv = ["apply", "-f", "-", "-o", "name"]
        if dry_run:
            argv.append("--dry-run=server")
        output = self._run(argv, stdin=document, timeout=300)
        return [line.strip() for line in output.splitlines() if line.strip()]

    def delete_namespace(self, namespace: str, *, dry_run: bool = False, timeout: int = 300) -> str:
        argv = ["delete", "namespace", namespace, "--ignore-not-found=true", "--wait=true", f"--timeout={timeout}s"]
        if dry_run:
            argv.append("--dry-run=server")
        return self._run(argv, timeout=timeout + 60)

    def delete(self, kind: str, name: str, *, namespace: str | None = None, dry_run: bool = False) -> str:
        argv = ["delete", kind, name, "--ignore-not-found=true"]
        if namespace:
            argv.extend(["-n", namespace])
        if dry_run:
            argv.append("--dry-run=server")
        return self._run(argv)

    def get_json(self, argv: Sequence[str]) -> Any:
        output = self._run([*argv, "-o", "json"])
        try:
            return json.loads(output or "{}")
        except json.JSONDecodeError as exc:
            raise KubeError("kubectl response is not JSON") from exc

    def api_server_endpoints(self) -> list[str]:
        """Addresses behind the ``kubernetes`` Service, for an egress rule.

        A NetworkPolicy cannot name a Service, and kube-proxy rewrites the
        destination before the policy is applied, so the replica's egress rule
        has to name the real endpoints.
        """
        try:
            payload = self.get_json(["get", "endpoints", "kubernetes", "-n", "default"])
        except KubeError:
            return []
        addresses: list[str] = []
        for subset in payload.get("subsets") or []:
            for entry in subset.get("addresses") or []:
                address = str(entry.get("ip") or "").strip()
                if address and address not in addresses:
                    addresses.append(address)
        return addresses

    def namespace_exists(self, namespace: str) -> bool:
        completed = self.runner.run(
            self._base() + ["get", "namespace", namespace, "-o", "name"], timeout=60
        )
        return completed.returncode == 0

    def deployment_ready(self, name: str, namespace: str) -> dict[str, Any]:
        try:
            payload = self.get_json(["get", "deployment", name, "-n", namespace])
        except KubeError as exc:
            return {"exists": False, "ready": False, "reason": str(exc)}
        status = payload.get("status") or {}
        spec = payload.get("spec") or {}
        desired = int(spec.get("replicas") or 0)
        ready = int(status.get("readyReplicas") or 0)
        return {"exists": True, "ready": desired >= 1 and ready >= desired,
                "desired": desired, "readyReplicas": ready}

    def namespace_deployments_ready(self, namespace: str) -> dict[str, Any]:
        try:
            payload = self.get_json(["get", "deployments", "-n", namespace])
        except KubeError as exc:
            return {"ready": False, "reason": str(exc), "deployments": 0}
        items = payload.get("items") or []
        desired = sum(int((item.get("spec") or {}).get("replicas") or 0) for item in items)
        ready = sum(int((item.get("status") or {}).get("readyReplicas") or 0) for item in items)
        load = next(
            (item for item in items if ((item.get("metadata") or {}).get("name")) == "load-generator"),
            None,
        )
        load_ready = int(((load or {}).get("status") or {}).get("readyReplicas") or 0)
        return {
            "ready": bool(items) and desired == ready and load_ready >= 1,
            "deployments": len(items),
            "desired_replicas": desired,
            "ready_replicas": ready,
            "load_generator_ready": load_ready,
        }
