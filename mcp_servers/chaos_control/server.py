"""Official MCP Python SDK v2 server for safety-gated ChaosBlade control."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from mcp import types
from mcp.server import MCPServer
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings

from mcp_servers.http_runtime import run_mcp_server

from .service import ChaosControlError, ChaosControlService, RuntimeConfig


_SERVICE = ChaosControlService(RuntimeConfig.from_env())
WATCHDOG_INTERVAL_SECONDS = 2.0


def set_service_for_tests(service: ChaosControlService) -> None:
    """Replace the process-global service for SDK-level tests."""

    global _SERVICE
    _SERVICE = service


def _read_annotations(title: str) -> types.ToolAnnotations:
    return types.ToolAnnotations(
        title=title,
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    )


def _write_annotations(title: str) -> types.ToolAnnotations:
    return types.ToolAnnotations(
        title=title,
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=True,
    )


def _destroy_annotations(title: str) -> types.ToolAnnotations:
    return types.ToolAnnotations(
        title=title,
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=True,
        open_world_hint=True,
    )


async def _call(operation):
    try:
        return await operation
    except ChaosControlError as exc:
        return exc.as_response()


def _server_kubeconfig(service: ChaosControlService) -> str | None:
    return service.config.kubeconfig


def _runtime_value(name: str, supplied: str | None, bound: str | None) -> str:
    if supplied and bound and supplied != bound:
        raise ChaosControlError(
            "BOUND_RUNTIME_MISMATCH",
            f"{name} does not match the Controller-bound Trial value.",
            next_step=f"Omit {name}; the Trial-scoped server supplies it automatically.",
        )
    value = bound or supplied
    if not value:
        raise ChaosControlError(
            "BOUND_RUNTIME_MISSING",
            f"{name} is not available for this Trial.",
            next_step="Stop and request a newly prepared Trial runtime.",
        )
    return value


def create_server(
    *,
    service: ChaosControlService | None = None,
    auth: AuthSettings | None = None,
    token_verifier: TokenVerifier | None = None,
) -> MCPServer:
    """Create a ChaosBlade MCP server for stdio or authenticated streamable HTTP."""

    chaos = service if service is not None else _SERVICE

    @asynccontextmanager
    async def lifespan(server: MCPServer):
        task = asyncio.create_task(_deadline_watchdog(chaos), name="chaos-control-deadline-watchdog")
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    server = MCPServer(
        "chaos_control_mcp",
        description="Safety-gated ChaosBlade control tools for resilience benchmark runs.",
        version="0.1.0",
        auth=auth,
        token_verifier=token_verifier,
        lifespan=lifespan,
    )

    @server.tool(
        name="chaos_validate_plan",
        title="Validate ChaosBlade Plan",
        annotations=_read_annotations("Validate ChaosBlade Plan"),
    )
    async def chaos_validate_plan(
        namespace: str,
        target_name: str,
        target_uid: str,
        fault_type: str,
        duration_seconds: int,
        intensity: dict[str, Any],
        run_id: str | None = None,
        selector: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Read-only validation of a proposed single-Pod ChaosBlade plan."""

        try:
            bound_run_id = _runtime_value(
                "run_id", run_id, chaos.config.authorized_run_id
            )
        except ChaosControlError as exc:
            return exc.as_response()

        return await _call(
            chaos.validate_plan(
                run_id=bound_run_id,
                namespace=namespace,
                target_name=target_name,
                target_uid=target_uid,
                fault_type=fault_type,
                duration_seconds=duration_seconds,
                intensity=intensity,
                selector=selector,
            )
        )

    @server.tool(
        name="chaos_inventory_run",
        title="Inventory ChaosBlade Run State",
        annotations=_read_annotations("Inventory ChaosBlade Run State"),
    )
    async def chaos_inventory_run(namespace: str) -> dict[str, Any]:
        """Read-only inventory of cluster-scoped ChaosBlade CRs for one logical namespace."""

        return await _call(chaos.inventory_run(namespace=namespace, kubeconfig=_server_kubeconfig(chaos)))

    @server.tool(
        name="chaos_create_experiment",
        title="Create Gated ChaosBlade Experiment",
        annotations=_write_annotations("Create Gated ChaosBlade Experiment"),
    )
    async def chaos_create_experiment(
        namespace: str,
        target_name: str,
        target_uid: str,
        fault_type: str,
        duration_seconds: int,
        intensity: dict[str, Any],
        run_id: str | None = None,
        controller_token_ref: str | None = None,
        expected_controller_pod_uid: str | None = None,
        baseline_gate_token: str | None = None,
        cleanup_handle: str | None = None,
        selector: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Create one gated experiment; Trial identity and gates are server-bound."""

        try:
            bound_run_id = _runtime_value(
                "run_id", run_id, chaos.config.authorized_run_id
            )
            bound_controller_ref = _runtime_value(
                "controller_token_ref",
                controller_token_ref,
                chaos.config.controller_token_ref,
            )
            bound_controller_uid = _runtime_value(
                "expected_controller_pod_uid",
                expected_controller_pod_uid,
                chaos.config.controller_pod_uid,
            )
            bound_baseline_token = _runtime_value(
                "baseline_gate_token",
                baseline_gate_token,
                chaos.config.baseline_gate_token,
            )
            bound_cleanup_handle = _runtime_value(
                "cleanup_handle", cleanup_handle, chaos.config.cleanup_handle
            )
        except ChaosControlError as exc:
            return exc.as_response()

        return await _call(
            chaos.create_experiment(
                run_id=bound_run_id,
                namespace=namespace,
                target_name=target_name,
                target_uid=target_uid,
                fault_type=fault_type,
                duration_seconds=duration_seconds,
                intensity=intensity,
                kubeconfig=_server_kubeconfig(chaos),
                controller_token_ref=bound_controller_ref,
                expected_controller_pod_uid=bound_controller_uid,
                baseline_gate_token=bound_baseline_token,
                cleanup_handle=bound_cleanup_handle,
                selector=selector,
            )
        )

    @server.tool(
        name="chaos_get_experiment",
        title="Get ChaosBlade Experiment",
        annotations=_read_annotations("Get ChaosBlade Experiment"),
    )
    async def chaos_get_experiment(namespace: str, name: str) -> dict[str, Any]:
        """Read-only lookup of one cluster-scoped ChaosBlade CR by logical namespace and name."""

        return await _call(chaos.get_experiment(namespace=namespace, name=name, kubeconfig=_server_kubeconfig(chaos)))

    @server.tool(
        name="chaos_operation_status",
        title="Get Chaos Create Operation Status",
        annotations=_read_annotations("Get Chaos Create Operation Status"),
    )
    async def chaos_operation_status(
        operation_id: str | None = None,
        cleanup_handle: str | None = None,
    ) -> dict[str, Any]:
        """Read-only reconciliation of a D6 create operation by Trial-scoped operation_id."""

        try:
            bound_cleanup_handle = _runtime_value(
                "cleanup_handle", cleanup_handle or operation_id, chaos.config.cleanup_handle
            )
        except ChaosControlError as exc:
            return exc.as_response()
        return await _call(
            chaos.operation_status(
                operation_id=operation_id,
                cleanup_handle=bound_cleanup_handle,
                kubeconfig=_server_kubeconfig(chaos),
            )
        )

    @server.tool(
        name="chaos_destroy_experiment",
        title="Destroy Ledger-Owned ChaosBlade Experiment",
        annotations=_destroy_annotations("Destroy Ledger-Owned ChaosBlade Experiment"),
    )
    async def chaos_destroy_experiment(
        cleanup_handle: str | None = None,
    ) -> dict[str, Any]:
        """Destroy the experiment owned by this Trial-scoped server."""

        try:
            bound_cleanup_handle = _runtime_value(
                "cleanup_handle", cleanup_handle, chaos.config.cleanup_handle
            )
        except ChaosControlError as exc:
            return exc.as_response()

        return await _call(chaos.destroy_experiment(cleanup_handle=bound_cleanup_handle, kubeconfig=_server_kubeconfig(chaos)))

    @server.tool(
        name="chaos_recovery_status",
        title="Read ChaosBlade Recovery Status",
        annotations=_read_annotations("Read ChaosBlade Recovery Status"),
    )
    async def chaos_recovery_status(
        cleanup_handle: str | None = None,
    ) -> dict[str, Any]:
        """Read recovery status for the experiment owned by this Trial."""

        try:
            bound_cleanup_handle = _runtime_value(
                "cleanup_handle", cleanup_handle, chaos.config.cleanup_handle
            )
        except ChaosControlError as exc:
            return exc.as_response()

        return await _call(chaos.recovery_status(cleanup_handle=bound_cleanup_handle, kubeconfig=_server_kubeconfig(chaos)))

    return server


mcp = create_server()


def main() -> None:
    run_mcp_server(create_server)


async def _deadline_watchdog(service: ChaosControlService) -> None:
    while True:
        try:
            await service.cleanup_expired_leases()
        except Exception:
            pass
        await asyncio.sleep(WATCHDOG_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
