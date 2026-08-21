"""Official MCP Python SDK v2 server for safety-gated ChaosBlade control."""

from __future__ import annotations

from typing import Any

from mcp import types
from mcp.server import MCPServer
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings

from mcp_servers.http_runtime import run_mcp_server

from .service import ChaosControlError, ChaosControlService, RuntimeConfig


_SERVICE = ChaosControlService(RuntimeConfig.from_env())


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
        destructive_hint=False,
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


def create_server(
    *,
    service: ChaosControlService | None = None,
    auth: AuthSettings | None = None,
    token_verifier: TokenVerifier | None = None,
) -> MCPServer:
    """Create a ChaosBlade MCP server for stdio or authenticated streamable HTTP."""

    chaos = service if service is not None else _SERVICE
    server = MCPServer(
        "chaos_control_mcp",
        description="Safety-gated ChaosBlade control tools for resilience benchmark runs.",
        version="0.1.0",
        auth=auth,
        token_verifier=token_verifier,
    )

    @server.tool(
        name="chaos_validate_plan",
        title="Validate ChaosBlade Plan",
        annotations=_read_annotations("Validate ChaosBlade Plan"),
    )
    async def chaos_validate_plan(
        run_id: str,
        namespace: str,
        target_name: str,
        target_uid: str,
        fault_type: str,
        duration_seconds: int,
        intensity: dict[str, Any],
        selector: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Read-only validation of a proposed single-Pod ChaosBlade plan."""

        return await _call(
            chaos.validate_plan(
                run_id=run_id,
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
    async def chaos_inventory_run(namespace: str, kubeconfig: str | None = None) -> dict[str, Any]:
        """Read-only inventory of cluster-scoped ChaosBlade CRs for one logical namespace."""

        return await _call(chaos.inventory_run(namespace=namespace, kubeconfig=kubeconfig))

    @server.tool(
        name="chaos_create_experiment",
        title="Create Gated ChaosBlade Experiment",
        annotations=_write_annotations("Create Gated ChaosBlade Experiment"),
    )
    async def chaos_create_experiment(
        run_id: str,
        namespace: str,
        target_name: str,
        target_uid: str,
        fault_type: str,
        duration_seconds: int,
        intensity: dict[str, Any],
        kubeconfig: str,
        controller_token_ref: str,
        expected_controller_pod_uid: str,
        baseline_gate_token: str,
        cleanup_handle: str,
        selector: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Create a ChaosBlade CR only after all controller safety gates pass."""

        return await _call(
            chaos.create_experiment(
                run_id=run_id,
                namespace=namespace,
                target_name=target_name,
                target_uid=target_uid,
                fault_type=fault_type,
                duration_seconds=duration_seconds,
                intensity=intensity,
                kubeconfig=kubeconfig,
                controller_token_ref=controller_token_ref,
                expected_controller_pod_uid=expected_controller_pod_uid,
                baseline_gate_token=baseline_gate_token,
                cleanup_handle=cleanup_handle,
                selector=selector,
            )
        )

    @server.tool(
        name="chaos_get_experiment",
        title="Get ChaosBlade Experiment",
        annotations=_read_annotations("Get ChaosBlade Experiment"),
    )
    async def chaos_get_experiment(namespace: str, name: str, kubeconfig: str | None = None) -> dict[str, Any]:
        """Read-only lookup of one cluster-scoped ChaosBlade CR by logical namespace and name."""

        return await _call(chaos.get_experiment(namespace=namespace, name=name, kubeconfig=kubeconfig))

    @server.tool(
        name="chaos_destroy_experiment",
        title="Destroy Ledger-Owned ChaosBlade Experiment",
        annotations=_destroy_annotations("Destroy Ledger-Owned ChaosBlade Experiment"),
    )
    async def chaos_destroy_experiment(cleanup_handle: str, kubeconfig: str) -> dict[str, Any]:
        """Destroy one ChaosBlade CR only through this server's private ledger handle."""

        return await _call(chaos.destroy_experiment(cleanup_handle=cleanup_handle, kubeconfig=kubeconfig))

    @server.tool(
        name="chaos_recovery_status",
        title="Read ChaosBlade Recovery Status",
        annotations=_read_annotations("Read ChaosBlade Recovery Status"),
    )
    async def chaos_recovery_status(cleanup_handle: str, kubeconfig: str | None = None) -> dict[str, Any]:
        """Read-only recovery status for a ledger-owned ChaosBlade experiment."""

        return await _call(chaos.recovery_status(cleanup_handle=cleanup_handle, kubeconfig=kubeconfig))

    return server


mcp = create_server()


def main() -> None:
    run_mcp_server(create_server)


if __name__ == "__main__":
    main()
