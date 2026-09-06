"""MCP tools for the shared safety-gated Chaos Mesh executor."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from mcp import types
from mcp.server import MCPServer
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings

from mcp_servers.http_runtime import PolicyGate, run_mcp_server

from .service import ChaosControlError, ChaosMeshControlService, RuntimeConfig


_SERVICE = ChaosMeshControlService(RuntimeConfig.from_env(server_name="chaos_mesh_control"))
WATCHDOG_INTERVAL_SECONDS = 2.0


def set_service_for_tests(service: ChaosMeshControlService) -> None:
    """Replace the process-global executor for SDK-level tests."""
    global _SERVICE
    _SERVICE = service


def _annotations(title: str, *, read_only: bool, idempotent: bool = False) -> types.ToolAnnotations:
    return types.ToolAnnotations(title=title, read_only_hint=read_only, destructive_hint=not read_only, idempotent_hint=idempotent, open_world_hint=True)


async def _call(operation: Any) -> dict[str, Any]:
    try:
        return await operation
    except ChaosControlError as exc:
        return exc.as_response()


def _bound(name: str, supplied: str | None, configured: str | None) -> str:
    if supplied and configured and supplied != configured:
        raise ChaosControlError("BOUND_RUNTIME_MISMATCH", f"{name} does not match the Controller-bound Trial value.", next_step=f"Omit {name}; the Trial-scoped server supplies it automatically.")
    if configured or supplied:
        return configured or supplied or ""
    raise ChaosControlError("BOUND_RUNTIME_MISSING", f"{name} is not available for this Trial.", next_step="Stop and request a newly prepared Trial runtime.")


def create_server(*, service: ChaosMeshControlService | None = None, auth: AuthSettings | None = None, token_verifier: TokenVerifier | None = None) -> MCPServer:
    """Create a policy-gated Chaos Mesh MCP server; it never hints alternatives."""
    chaos = service or _SERVICE
    gate = PolicyGate.from_env("chaos_mesh_control")

    @asynccontextmanager
    async def lifespan(_: MCPServer):
        task = asyncio.create_task(_watchdog(chaos))
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    server = MCPServer("chaos_mesh_control_mcp", description="Safety-gated Chaos Mesh control tools for resilience benchmark runs.", version="0.1.0", auth=auth, token_verifier=token_verifier, lifespan=lifespan)

    @server.tool(name="chaos_mesh_validate_plan", title="Validate Chaos Mesh Plan", annotations=_annotations("Validate Chaos Mesh Plan", read_only=True, idempotent=True))
    @gate.guard("chaos_mesh_validate_plan")
    async def validate(namespace: str, target_name: str, target_uid: str, fault_type: str, duration_seconds: int, intensity: dict[str, Any], run_id: str | None = None, selector: dict[str, str] | None = None) -> dict[str, Any]:
        """Validate a single-Pod Chaos Mesh plan without mutating Kubernetes."""
        try:
            bound_run = _bound("run_id", run_id, chaos.config.authorized_run_id)
        except ChaosControlError as exc:
            return exc.as_response()
        return await _call(chaos.validate_plan(run_id=bound_run, namespace=namespace, target_name=target_name, target_uid=target_uid, fault_type=fault_type, duration_seconds=duration_seconds, intensity=intensity, selector=selector))

    @server.tool(name="chaos_mesh_inventory_run", title="Inventory Chaos Mesh Run State", annotations=_annotations("Inventory Chaos Mesh Run State", read_only=True, idempotent=True))
    @gate.guard("chaos_mesh_inventory_run")
    async def inventory(namespace: str) -> dict[str, Any]:
        """Read the controlled Chaos Mesh resources for one logical namespace."""
        return await _call(chaos.inventory_run(namespace=namespace, kubeconfig=chaos.config.kubeconfig))

    @server.tool(name="chaos_mesh_create_experiment", title="Create Gated Chaos Mesh Experiment", annotations=_annotations("Create Gated Chaos Mesh Experiment", read_only=False))
    @gate.guard("chaos_mesh_create_experiment")
    async def create(namespace: str, target_name: str, target_uid: str, fault_type: str, duration_seconds: int, intensity: dict[str, Any], run_id: str | None = None, controller_token_ref: str | None = None, expected_controller_pod_uid: str | None = None, baseline_gate_token: str | None = None, cleanup_handle: str | None = None, selector: dict[str, str] | None = None) -> dict[str, Any]:
        """Create one ledger-owned Chaos Mesh experiment after all shared gates pass."""
        try:
            kwargs = {
                "run_id": _bound("run_id", run_id, chaos.config.authorized_run_id),
                "controller_token_ref": _bound("controller_token_ref", controller_token_ref, chaos.config.controller_token_ref),
                "expected_controller_pod_uid": _bound("expected_controller_pod_uid", expected_controller_pod_uid, chaos.config.controller_pod_uid),
                "baseline_gate_token": _bound("baseline_gate_token", baseline_gate_token, chaos.config.baseline_gate_token),
                "cleanup_handle": _bound("cleanup_handle", cleanup_handle, chaos.config.cleanup_handle),
            }
        except ChaosControlError as exc:
            return exc.as_response()
        return await _call(chaos.create_experiment(**kwargs, namespace=namespace, target_name=target_name, target_uid=target_uid, fault_type=fault_type, duration_seconds=duration_seconds, intensity=intensity, kubeconfig=chaos.config.kubeconfig or "", selector=selector))

    @server.tool(name="chaos_mesh_get_experiment", title="Get Chaos Mesh Experiment", annotations=_annotations("Get Chaos Mesh Experiment", read_only=True, idempotent=True))
    @gate.guard("chaos_mesh_get_experiment")
    async def get(namespace: str, name: str) -> dict[str, Any]:
        """Get one ledger-compatible Chaos Mesh resource."""
        return await _call(chaos.get_experiment(namespace=namespace, name=name, kubeconfig=chaos.config.kubeconfig))

    @server.tool(name="chaos_mesh_operation_status", title="Get Chaos Mesh Create Operation Status", annotations=_annotations("Get Chaos Mesh Create Operation Status", read_only=True, idempotent=True))
    @gate.guard("chaos_mesh_operation_status")
    async def status(operation_id: str | None = None, cleanup_handle: str | None = None) -> dict[str, Any]:
        """Reconcile a D6 operation without issuing a second create."""
        try:
            handle = _bound("cleanup_handle", cleanup_handle or operation_id, chaos.config.cleanup_handle)
        except ChaosControlError as exc:
            return exc.as_response()
        return await _call(chaos.operation_status(operation_id=operation_id, cleanup_handle=handle, kubeconfig=chaos.config.kubeconfig))

    @server.tool(name="chaos_mesh_destroy_experiment", title="Destroy Ledger-Owned Chaos Mesh Experiment", annotations=_annotations("Destroy Ledger-Owned Chaos Mesh Experiment", read_only=False, idempotent=True))
    @gate.guard("chaos_mesh_destroy_experiment")
    async def destroy(cleanup_handle: str | None = None) -> dict[str, Any]:
        """Destroy only the experiment owned by this Trial and verify it is absent."""
        try:
            handle = _bound("cleanup_handle", cleanup_handle, chaos.config.cleanup_handle)
        except ChaosControlError as exc:
            return exc.as_response()
        return await _call(chaos.destroy_experiment(cleanup_handle=handle, kubeconfig=chaos.config.kubeconfig or ""))

    @server.tool(name="chaos_mesh_recovery_status", title="Read Chaos Mesh Recovery Status", annotations=_annotations("Read Chaos Mesh Recovery Status", read_only=True, idempotent=True))
    @gate.guard("chaos_mesh_recovery_status")
    async def recovery(cleanup_handle: str | None = None) -> dict[str, Any]:
        """Read resource cleanup status from the shared Trial ledger."""
        try:
            handle = _bound("cleanup_handle", cleanup_handle, chaos.config.cleanup_handle)
        except ChaosControlError as exc:
            return exc.as_response()
        return await _call(chaos.recovery_status(cleanup_handle=handle, kubeconfig=chaos.config.kubeconfig))

    return server


mcp = create_server()


async def _watchdog(service: ChaosMeshControlService) -> None:
    while True:
        try:
            await service.cleanup_expired_leases()
        except Exception:
            pass
        await asyncio.sleep(WATCHDOG_INTERVAL_SECONDS)


def main() -> None:
    """Run the standard authenticated MCP HTTP/stdio adapter."""
    run_mcp_server(create_server)


if __name__ == "__main__":
    main()
