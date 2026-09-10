from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import ipaddress
import os
import re
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlparse

from mcp.server import MCPServer
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings

from stage2_service.capability_policy import (
    CapabilityPolicyError,
    CapabilityPolicyDocument,
    PLATFORM_LEDGER_ROOT_ENV,
    effective_tool_state,
    is_channel_unavailable,
    platform_ledger_root_from_env,
    policy_file_from_env,
    read_policy_file,
)
from stage2_service.platform_ledger import PlatformLedger
from stage2_service.notices import attach_notices

from mcp_servers.audit_bridge import AuditBridgeClient
from mcp_servers.runtime_audit import (
    audit_client_from_env,
    audited_async_call,
    audited_sync_call,
    bound_arguments,
)


DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8000
DEFAULT_HTTP_PATH = "/mcp"
DEFAULT_CLIENT_ID = "resiliencebenchmark-agent"
MIN_TOKEN_CHARS = 32
TOKEN_ENV = "RESBENCH_MCP_TOKEN"
TOKEN_STATE_FILE_ENV = "RESBENCH_MCP_TOKEN_STATE_FILE"
TRANSPORT_ENV = "RESBENCH_MCP_TRANSPORT"
HTTP_HOST_ENV = "RESBENCH_MCP_HTTP_HOST"
HTTP_PORT_ENV = "RESBENCH_MCP_HTTP_PORT"
HTTP_PATH_ENV = "RESBENCH_MCP_HTTP_PATH"
HTTP_ALLOW_NON_LOOPBACK_ENV = "RESBENCH_MCP_HTTP_ALLOW_NON_LOOPBACK"
ISSUER_URL_ENV = "RESBENCH_MCP_ISSUER_URL"
RESOURCE_URL_ENV = "RESBENCH_MCP_RESOURCE_URL"
SCOPE_ENV = "RESBENCH_MCP_SCOPE"
CLIENT_ID_ENV = "RESBENCH_MCP_CLIENT_ID"
TransportName = Literal["stdio", "streamable-http", "sse"]
TOOL_DISABLED_RESPONSE = {
    "ok": False,
    "error": {
        "code": "TOOL_DISABLED",
        "message": "该工具已停用。",
    },
}
CHANNEL_UNAVAILABLE_RESPONSE = {
    "ok": False,
    "error": {
        "code": "CHANNEL_UNAVAILABLE",
        "message": "通道暂时不可用。",
    },
}
PLATFORM_POLICY_ERROR_RESPONSE = {
    "ok": False,
    "error": {
        "code": "PLATFORM_POLICY_ERROR",
        "message": "平台策略不可用。",
    },
}


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

    def streamable_http_kwargs(self) -> dict[str, object]:
        return {
            "host": self.host,
            "port": self.port,
            "streamable_http_path": self.path,
        }

    def sse_kwargs(self) -> dict[str, object]:
        return {
            "host": self.host,
            "port": self.port,
            "sse_path": self.path,
            "message_path": "/messages/",
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


class PolicyGate:
    """Per-tool policy gate for MCP server functions.

    When ``RESBENCH_MCP_POLICY_FILE`` is absent the gate is unconfigured and
    allows calls, preserving the current production behavior of the four MCP
    servers.  When the variable is set, the policy file must exist, be valid,
    and be private; otherwise calls fail closed with a platform-policy error.
    The policy file is read on every tool call and is never cached by mtime.
    """

    def __init__(
        self,
        *,
        server_name: str,
        policy_file: Path | None,
        ledger_root: Path | None = None,
        audit_client: AuditBridgeClient | None = None,
    ) -> None:
        self.server_name = server_name
        self.policy_file = policy_file
        self.ledger_root = ledger_root
        self.audit_client = audit_client

    @classmethod
    def from_env(
        cls,
        server_name: str,
        *,
        env: Mapping[str, str] | None = None,
    ) -> PolicyGate:
        policy_file = policy_file_from_env(env)
        ledger_root = platform_ledger_root_from_env(env)
        if ledger_root is None and policy_file is not None:
            ledger_root = policy_file.parent / "platform-ledger"
        # A configured audit socket is a Trial control boundary.  Its client is
        # deliberately built during server construction: malformed settings
        # prevent the controlled server from starting instead of silently
        # running without the Controller acknowledgement.
        return cls(
            server_name=server_name,
            policy_file=policy_file,
            ledger_root=ledger_root,
            audit_client=audit_client_from_env(env),
        )

    @property
    def configured(self) -> bool:
        return self.policy_file is not None

    def guard(self, tool_name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(function: Callable[..., Any]) -> Callable[..., Any]:
            if inspect.iscoroutinefunction(function):

                @wraps(function)
                async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                    arguments = bound_arguments(function, args, kwargs)

                    async def operation() -> Any:
                        # This read must happen after the Controller accepts
                        # the call: D1/D3/D5 can change the policy as the
                        # authoritative ToolCall is recorded.
                        decision = self._decision(tool_name)
                        if not decision["allowed"]:
                            return decision["response"]
                        result = function(*args, **kwargs)
                        if inspect.isawaitable(result):
                            result = await result
                        return self._attach_notices(result, decision)

                    return await audited_async_call(
                        self.audit_client,
                        self.server_name,
                        tool_name,
                        arguments,
                        operation,
                    )

                return async_wrapper

            @wraps(function)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                arguments = bound_arguments(function, args, kwargs)

                def operation() -> Any:
                    decision = self._decision(tool_name)
                    if not decision["allowed"]:
                        return decision["response"]
                    return self._attach_notices(function(*args, **kwargs), decision)

                return audited_sync_call(
                    self.audit_client,
                    self.server_name,
                    tool_name,
                    arguments,
                    operation,
                )

            return sync_wrapper

        return decorator

    def _attach_notices(self, result: Any, decision: Mapping[str, Any]) -> Any:
        trial_id = decision.get("trial_id")
        if not trial_id or self.ledger_root is None:
            return result
        return attach_notices(result, PlatformLedger(self.ledger_root), str(trial_id))

    def chaos_create_uncertainty_variant(self) -> str | None:
        if self.policy_file is None:
            return None
        document = read_policy_file(self.policy_file)
        policy = document.server_policy(self.server_name)
        if policy is None or policy.chaos_create_uncertainty_variant is None:
            return None
        return policy.chaos_create_uncertainty_variant.value

    def _decision(self, tool_name: str) -> dict[str, Any]:
        if self.policy_file is None:
            return {"allowed": True, "response": None}
        now = datetime.now(timezone.utc)
        try:
            document = read_policy_file(self.policy_file)
            server_policy = document.server_policy(self.server_name)
            tool_policy = (
                server_policy.tools.get(tool_name)
                if server_policy is not None
                else None
            )
            state = effective_tool_state(server_policy, tool_name)
            channel_unavailable = server_policy is not None and is_channel_unavailable(server_policy, now=now)
            if channel_unavailable:
                self._record(
                    document,
                    event_type="CHANNEL_UNAVAILABLE_RETURNED",
                    payload={
                        "server": self.server_name,
                        "tool": tool_name,
                        "state": "channel_unavailable",
                        "reason": tool_policy.reason if tool_policy is not None else None,
                        "channel_unavailable_until": (
                            server_policy.channel_unavailable_until.isoformat()
                            if server_policy is not None
                            and server_policy.channel_unavailable_until is not None
                            else None
                        ),
                        "chaos_create_uncertainty_variant": (
                            server_policy.chaos_create_uncertainty_variant.value
                            if server_policy is not None
                            and server_policy.chaos_create_uncertainty_variant is not None
                            else None
                        ),
                    },
                    occurred_at=now,
                )
                return {
                    "allowed": False,
                    "response": dict(CHANNEL_UNAVAILABLE_RESPONSE),
                }
            if state in {"disabled", "decoy"}:
                event_type = (
                    "DECOY_INVOKED"
                    if state == "decoy"
                    else "TOOL_CALL_DENIED_DISABLED"
                )
                reason = (
                    tool_policy.reason
                    if tool_policy is not None and tool_policy.reason is not None
                    else server_policy.reason
                    if server_policy is not None
                    else None
                )
                self._record(
                    document,
                    event_type=event_type,
                    payload={
                        "server": self.server_name,
                        "tool": tool_name,
                        "state": state,
                        "reason": reason,
                    },
                    occurred_at=now,
                )
                return {
                    "allowed": False,
                    "response": dict(TOOL_DISABLED_RESPONSE),
                }
            return {"allowed": True, "response": None, "trial_id": document.trial_id}
        except Exception:
            return {
                "allowed": False,
                "response": dict(PLATFORM_POLICY_ERROR_RESPONSE),
            }

    def _record(
        self,
        document: CapabilityPolicyDocument,
        *,
        event_type: str,
        payload: Mapping[str, Any],
        occurred_at: datetime,
    ) -> None:
        if self.ledger_root is None:
            raise CapabilityPolicyError("platform ledger root is unavailable")
        PlatformLedger(self.ledger_root).append(
            trial_id=document.trial_id,
            event_type=event_type,
            occurred_at=occurred_at,
            payload=payload,
        )


class FileBackedBearerTokenVerifier:
    """Verifier whose active token can be atomically rotated by the Controller."""

    def __init__(
        self,
        *,
        token_file: Path,
        scopes: list[str],
        resource: str,
        client_id: str = DEFAULT_CLIENT_ID,
    ) -> None:
        self._token_file = token_file.resolve()
        self._scopes = list(scopes)
        self._resource = resource
        self._client_id = client_id or DEFAULT_CLIENT_ID
        self._read_token()

    async def verify_token(self, token: str) -> AccessToken | None:
        active = self._read_token()
        candidate_sha256 = hashlib.sha256(token.encode("utf-8")).digest()
        active_sha256 = hashlib.sha256(active.encode("utf-8")).digest()
        if not hmac.compare_digest(candidate_sha256, active_sha256):
            return None
        return AccessToken(
            token="<redacted>",
            client_id=self._client_id,
            scopes=self._scopes,
            resource=self._resource,
        )

    def _read_token(self) -> str:
        path = self._token_file
        if not path.is_absolute() or not path.is_file() or path.is_symlink():
            raise MCPRuntimeConfigError(
                f"{TOKEN_STATE_FILE_ENV} must be an absolute regular file"
            )
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise MCPRuntimeConfigError(
                f"{TOKEN_STATE_FILE_ENV} must not be group/world accessible"
            )
        token = path.read_text(encoding="utf-8").strip()
        if (
            len(token) < MIN_TOKEN_CHARS
            or any(char.isspace() for char in token)
        ):
            raise MCPRuntimeConfigError(
                f"{TOKEN_STATE_FILE_ENV} contains an invalid token"
            )
        return token


def read_transport(env: Mapping[str, str] | None = None) -> TransportName:
    values = os.environ if env is None else env
    raw = values.get(TRANSPORT_ENV, "stdio").strip().lower()
    if raw in {"stdio", ""}:
        return "stdio"
    if raw in {"http", "streamable-http", "streamable_http"}:
        return "streamable-http"
    if raw == "sse":
        return "sse"
    raise MCPRuntimeConfigError(f"{TRANSPORT_ENV} must be stdio, streamable-http, or sse")


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
    token_file = values.get(TOKEN_STATE_FILE_ENV, "").strip()
    if token_file:
        verifier = FileBackedBearerTokenVerifier(
            token_file=Path(token_file),
            scopes=scopes,
            resource=resource_url,
            client_id=values.get(CLIENT_ID_ENV, DEFAULT_CLIENT_ID),
        )
    else:
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
    if transport == "sse":
        server.run("sse", **config.sse_kwargs())
        return
    server.run("streamable-http", **config.streamable_http_kwargs())


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
