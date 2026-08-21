from __future__ import annotations

from typing import Any, Callable

from mcp.server import MCPServer
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.types import ToolAnnotations

from mcp_servers.http_runtime import run_mcp_server

from .core import SourceROError, SourceROIndex, error_envelope


READ_ONLY_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


def _call(operation: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return operation()
    except SourceROError as exc:
        return error_envelope(exc)


def _index() -> SourceROIndex:
    return SourceROIndex.from_environment()


def create_server(
    *,
    auth: AuthSettings | None = None,
    token_verifier: TokenVerifier | None = None,
) -> MCPServer:
    server = MCPServer(
        name="source_ro",
        title="BenchmarkFactory read-only source server",
        version="0.1.0",
        description="Read-only access to locked microservice source snapshots.",
        instructions=(
            "Use only source_* tools. Paths are relative to a source lock id directory; "
            "absolute paths, traversal, VCS internals, hidden ground truth, and oversized or binary files are rejected."
        ),
        auth=auth,
        token_verifier=token_verifier,
    )

    @server.tool(
        name="source_list_repositories",
        title="List locked source repositories",
        description="List source repositories declared in environment/shared/source-locks.yaml.",
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def list_repositories(
        application: str | None = None,
        component: str | None = None,
        limit: int = 100,
        offset: int = 0,
        format: str = "json",
    ) -> dict[str, Any]:
        return _call(
            lambda: _index().list_repositories(
                application=application,
                component=component,
                limit=limit,
                offset=offset,
                format=format,
            )
        )

    @server.tool(
        name="source_list_files",
        title="List source files",
        description="List visible files under a locked source repository id.",
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def list_files(
        repo_id: str,
        path: str = ".",
        recursive: bool = False,
        limit: int = 100,
        offset: int = 0,
        format: str = "json",
    ) -> dict[str, Any]:
        return _call(
            lambda: _index().list_files(
                repo_id,
                path,
                recursive=recursive,
                limit=limit,
                offset=offset,
                format=format,
            )
        )

    @server.tool(
        name="source_search_text",
        title="Search source text",
        description="Search UTF-8 text files with a literal substring search; no shell or regex execution is used.",
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def search_text(
        repo_id: str,
        query: str,
        path: str = ".",
        case_sensitive: bool = False,
        limit: int = 100,
        offset: int = 0,
        format: str = "json",
    ) -> dict[str, Any]:
        return _call(
            lambda: _index().search_text(
                repo_id,
                query,
                path,
                case_sensitive=case_sensitive,
                limit=limit,
                offset=offset,
                format=format,
            )
        )

    @server.tool(
        name="source_read_file",
        title="Read source file",
        description="Read a UTF-8 source file with a hard 25k character response cap.",
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def read_file(
        repo_id: str,
        path: str,
        start_line: int | None = None,
        max_chars: int = 25000,
        format: str = "json",
    ) -> dict[str, Any]:
        return _call(
            lambda: _index().read_file(
                repo_id,
                path,
                start_line=start_line,
                max_chars=max_chars,
                format=format,
            )
        )

    @server.tool(
        name="source_show_commit",
        title="Show source lock commit",
        description="Show locked commit, tag, archive digest, and runtime mapping metadata for one source repository.",
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def show_commit(repo_id: str, format: str = "json") -> dict[str, Any]:
        return _call(lambda: _index().show_commit(repo_id, format=format))

    return server


def main() -> None:
    run_mcp_server(create_server)


if __name__ == "__main__":
    main()
