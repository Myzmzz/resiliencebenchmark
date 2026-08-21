"""Official MCP Python SDK v2 server for restricted Kubernetes read-only access."""

from __future__ import annotations

from typing import Any

from mcp import types
from mcp.server import MCPServer
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings

from mcp_servers.http_runtime import run_mcp_server
from .service import K8sROError, K8sROService, RuntimeConfig, error_envelope


_SERVICE: K8sROService | None = None


def _service() -> K8sROService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = K8sROService(RuntimeConfig.from_env())
    return _SERVICE


def set_service_for_tests(service: K8sROService) -> None:
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


async def _call(operation) -> dict[str, Any]:
    try:
        return await operation
    except K8sROError as exc:
        return error_envelope(exc)


def create_server(
    *,
    service: K8sROService | None = None,
    auth: AuthSettings | None = None,
    token_verifier: TokenVerifier | None = None,
) -> MCPServer:
    server = MCPServer(
        "k8s_ro_mcp",
        description=(
            "Read-only, allowlisted Kubernetes tools for benchmark namespaces. "
            "The kubeconfig path is server runtime configuration and is never accepted as a tool parameter."
        ),
        version="0.1.0",
        auth=auth,
        token_verifier=token_verifier,
    )

    def svc() -> K8sROService:
        return service if service is not None else _service()

    @server.tool(name="k8s_get_resource", title="Get Kubernetes Resource", annotations=_read_annotations("Get Kubernetes Resource"))
    async def k8s_get_resource(namespace: str, resource: str, name: str) -> dict[str, Any]:
        """Get one allowlisted namespaced Kubernetes resource as sanitized JSON."""

        return await _call(svc().get_resource(namespace=namespace, resource=resource, name=name))

    @server.tool(name="k8s_list_resources", title="List Kubernetes Resources", annotations=_read_annotations("List Kubernetes Resources"))
    async def k8s_list_resources(
        namespace: str,
        resource: str,
        label_selector: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List allowlisted namespaced Kubernetes resources with bounded pagination."""

        return await _call(svc().list_resources(namespace=namespace, resource=resource, label_selector=label_selector, limit=limit, offset=offset))

    @server.tool(name="k8s_list_events", title="List Kubernetes Events", annotations=_read_annotations("List Kubernetes Events"))
    async def k8s_list_events(
        namespace: str,
        involved_object_kind: str | None = None,
        involved_object_name: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List sanitized namespace events, optionally scoped to one involved object."""

        return await _call(svc().list_events(namespace=namespace, involved_object_kind=involved_object_kind, involved_object_name=involved_object_name, limit=limit, offset=offset))

    @server.tool(name="k8s_pod_logs", title="Read Pod Logs", annotations=_read_annotations("Read Pod Logs"))
    async def k8s_pod_logs(
        namespace: str,
        pod: str,
        container: str | None = None,
        since_seconds: int = 3600,
        tail_lines: int = 200,
    ) -> dict[str, Any]:
        """Read bounded current Pod logs. Previous logs, exec, and port-forward are intentionally not exposed."""

        return await _call(svc().pod_logs(namespace=namespace, pod=pod, container=container, since_seconds=since_seconds, tail_lines=tail_lines))

    @server.tool(name="k8s_cluster_inventory", title="Kubernetes Cluster Inventory", annotations=_read_annotations("Kubernetes Cluster Inventory"))
    async def k8s_cluster_inventory(resource: str, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """List a small allowlist of cluster-scope inventory resources such as nodes, namespaces, and CRD names."""

        return await _call(svc().cluster_inventory(resource=resource, limit=limit, offset=offset))

    return server


mcp = create_server()


def main() -> None:
    run_mcp_server(create_server)


if __name__ == "__main__":
    main()
