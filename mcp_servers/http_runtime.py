from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol
from urllib.parse import urlparse

from mcp.server import MCPServer
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings


DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8000
DEFAULT_HTTP_PATH = "/mcp"
DEFAULT_CLIENT_ID = "resiliencebenchmark-agent"
MIN_TOKEN_CHARS = 32
TOKEN_ENV = "RESBENCH_MCP_TOKEN"
TRANSPORT_ENV = "RESBENCH_MCP_TRANSPORT"
HTTP_HOST_ENV = "RESBENCH_MCP_HTTP_HOST"
HTTP_PORT_ENV = "RESBENCH_MCP_HTTP_PORT"
HTTP_PATH_ENV = "RESBENCH_MCP_HTTP_PATH"
HTTP_ALLOW_NON_LOOPBACK_ENV = "RESBENCH_MCP_HTTP_ALLOW_NON_LOOPBACK"
ISSUER_URL_ENV = "RESBENCH_MCP_ISSUER_URL"
RESOURCE_URL_ENV = "RESBENCH_MCP_RESOURCE_URL"
SCOPE_ENV = "RESBENCH_MCP_SCOPE"
CLIENT_ID_ENV = "RESBENCH_MCP_CLIENT_ID"
TransportName = Literal["stdio", "streamable-http"]


class MCPRuntimeConfigError(ValueError):
    """Raised when MCP runtime configuration is missing or unsafe."""


class MCPServerFactory(Protocol):
    def __call__(
        self,
        *,
        auth: AuthSettings | None = None,
        token_verifier: TokenVerifier | None = None,
    ) -> MCPServer: ...


@dataclass(frozen=True)
class MCPHttpRuntimeConfig:
    host: str
    port: int
    path: str
    auth: AuthSettings
    token_verifier: TokenVerifier

    def run_kwargs(self) -> dict[str, object]:
        return {
            "host": self.host,
            "port": self.port,
            "streamable_http_path": self.path,
        }


class StaticBearerTokenVerifier:
    """Constant-time verifier for one static bearer token supplied at runtime."""

    def __init__(self, *, token: str, scopes: list[str], resource: str, client_id: str = DEFAULT_CLIENT_ID) -> None:
        if not token:
            raise MCPRuntimeConfigError(f"{TOKEN_ENV} is required for HTTP MCP mode")
        if token != token.strip() or any(char.isspace() for char in token) or len(token) < MIN_TOKEN_CHARS:
            raise MCPRuntimeConfigError(
                f"{TOKEN_ENV} must be at least {MIN_TOKEN_CHARS} non-whitespace characters with no surrounding whitespace"
            )
        if not scopes:
            raise MCPRuntimeConfigError(f"{SCOPE_ENV} is required for HTTP MCP mode")
        self._token_sha256 = hashlib.sha256(token.encode("utf-8")).digest()
        self._scopes = list(scopes)
        self._resource = resource
        self._client_id = client_id or DEFAULT_CLIENT_ID

    async def verify_token(self, token: str) -> AccessToken | None:
        candidate_sha256 = hashlib.sha256(token.encode("utf-8")).digest()
        if not hmac.compare_digest(candidate_sha256, self._token_sha256):
            return None
        return AccessToken(
            token="<redacted>",
            client_id=self._client_id,
            scopes=self._scopes,
            resource=self._resource,
        )


def read_transport(env: Mapping[str, str] | None = None) -> TransportName:
    values = os.environ if env is None else env
    raw = values.get(TRANSPORT_ENV, "stdio").strip().lower()
    if raw in {"stdio", ""}:
        return "stdio"
    if raw in {"http", "streamable-http", "streamable_http"}:
        return "streamable-http"
    raise MCPRuntimeConfigError(f"{TRANSPORT_ENV} must be stdio or streamable-http")


def build_http_runtime_config(env: Mapping[str, str] | None = None) -> MCPHttpRuntimeConfig:
    values = os.environ if env is None else env
    token = _required(values, TOKEN_ENV)
    issuer_url = _required_url(values, ISSUER_URL_ENV)
    resource_url = _required_url(values, RESOURCE_URL_ENV)
    scope = _required(values, SCOPE_ENV)
    host = _parse_host(values.get(HTTP_HOST_ENV, DEFAULT_HTTP_HOST), values)
    port = _parse_port(values.get(HTTP_PORT_ENV, str(DEFAULT_HTTP_PORT)))
    path = _parse_path(values.get(HTTP_PATH_ENV, DEFAULT_HTTP_PATH))
    scopes = [part for part in re.split(r"[\s,]+", scope.strip()) if part]
    if not scopes:
        raise MCPRuntimeConfigError(f"{SCOPE_ENV} is required for HTTP MCP mode")
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._/-]{0,127}", item) for item in scopes):
        raise MCPRuntimeConfigError(f"{SCOPE_ENV} contains an invalid scope")
    auth = AuthSettings(
        issuer_url=issuer_url,
        resource_server_url=resource_url,
        required_scopes=scopes,
    )
    verifier = StaticBearerTokenVerifier(
        token=token,
        scopes=scopes,
        resource=resource_url,
        client_id=values.get(CLIENT_ID_ENV, DEFAULT_CLIENT_ID),
    )
    return MCPHttpRuntimeConfig(host=host, port=port, path=path, auth=auth, token_verifier=verifier)


def create_server_for_transport(
    factory: MCPServerFactory,
    *,
    transport: TransportName,
    env: Mapping[str, str] | None = None,
) -> tuple[MCPServer, MCPHttpRuntimeConfig | None]:
    if transport == "stdio":
        return factory(), None
    config = build_http_runtime_config(env)
    return factory(auth=config.auth, token_verifier=config.token_verifier), config


def run_mcp_server(factory: MCPServerFactory, *, env: Mapping[str, str] | None = None) -> None:
    transport = read_transport(env)
    server, config = create_server_for_transport(factory, transport=transport, env=env)
    if transport == "stdio":
        server.run("stdio")
        return
    assert config is not None
    server.run("streamable-http", **config.run_kwargs())


def verify_static_token(verifier: TokenVerifier, token: str) -> AccessToken | None:
    return asyncio.run(verifier.verify_token(token))


def _required(values: Mapping[str, str], name: str) -> str:
    value = values.get(name)
    if value is None or not value.strip():
        raise MCPRuntimeConfigError(f"{name} is required for HTTP MCP mode")
    return value


def _required_url(values: Mapping[str, str], name: str) -> str:
    value = _required(values, name)
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise MCPRuntimeConfigError(f"{name} must be an explicit http(s) URL")
    if parsed.username or parsed.password:
        raise MCPRuntimeConfigError(f"{name} must not contain credentials")
    if parsed.query or parsed.fragment:
        raise MCPRuntimeConfigError(f"{name} must not contain a query or fragment")
    return value


def _parse_host(host: str, values: Mapping[str, str]) -> str:
    normalized = host.strip()
    if not normalized:
        raise MCPRuntimeConfigError(f"{HTTP_HOST_ENV} must not be empty")
    if _is_loopback_host(normalized):
        return normalized
    allowed = values.get(HTTP_ALLOW_NON_LOOPBACK_ENV, "").strip().lower() in {"1", "true", "yes"}
    if not allowed:
        raise MCPRuntimeConfigError(
            f"{HTTP_HOST_ENV} must be loopback unless {HTTP_ALLOW_NON_LOOPBACK_ENV}=true is set"
        )
    return normalized


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _parse_port(port: str) -> int:
    try:
        value = int(port)
    except ValueError as exc:
        raise MCPRuntimeConfigError(f"{HTTP_PORT_ENV} must be an integer") from exc
    if value < 1024 or value > 65535:
        raise MCPRuntimeConfigError(f"{HTTP_PORT_ENV} must be between 1024 and 65535")
    return value


def _parse_path(path: str) -> str:
    value = path.strip()
    if not value.startswith("/"):
        raise MCPRuntimeConfigError(f"{HTTP_PATH_ENV} must start with /")
    if "?" in value or "#" in value or "\\" in value or ".." in value or "//" in value:
        raise MCPRuntimeConfigError(f"{HTTP_PATH_ENV} must be a clean URL path")
    if not re.fullmatch(r"/[A-Za-z0-9._~/-]*", value):
        raise MCPRuntimeConfigError(f"{HTTP_PATH_ENV} contains unsupported characters")
    return value
