"""Official MCP SDK v2 server for the controlled code sandbox."""

from __future__ import annotations

from typing import Annotated, Any

from mcp import types
from mcp.server import MCPServer
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from pydantic import Field

from mcp_servers.http_runtime import PolicyGate, run_mcp_server

from .service import CodeSandboxError, CodeSandboxService


_SERVICE: CodeSandboxService | None = None


def set_service_for_tests(service: CodeSandboxService) -> None:
    global _SERVICE
    _SERVICE = service


def create_server(
    *,
    service: CodeSandboxService | None = None,
    auth: AuthSettings | None = None,
    token_verifier: TokenVerifier | None = None,
) -> MCPServer:
    """Create a PolicyGate-protected MCP server with one non-network tool."""
    server = MCPServer(
        "code_sandbox_mcp",
        description="Run bounded Python in the per-Trial isolated Agent sandbox.",
        version="0.1.0",
        auth=auth,
        token_verifier=token_verifier,
    )
    gate = PolicyGate.from_env("code_sandbox")

    def svc() -> CodeSandboxService:
        chosen = service if service is not None else _SERVICE
        if chosen is None:
            raise CodeSandboxError("sandbox service is not configured")
        return chosen

    @server.tool(
        name="run_python",
        title="Run Python in controlled sandbox",
        annotations=types.ToolAnnotations(
            title="Run Python in controlled sandbox",
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    @gate.guard("run_python")
    async def run_python(
        code: str,
        timeout_seconds: Annotated[int, Field(ge=1, le=60)] = 60,
    ) -> dict[str, Any]:
        """Run source with a maximum 60-second budget and a 64 KiB output cap."""
        try:
            return svc().run_python(code, timeout_seconds)
        except CodeSandboxError as exc:
            return {"ok": False, "error": {"code": "CODE_SANDBOX_ERROR", "message": str(exc)}}

    return server


def main() -> None:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = CodeSandboxService.from_env()
    run_mcp_server(create_server)


if __name__ == "__main__":
    main()
