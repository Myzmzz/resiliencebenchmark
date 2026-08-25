from __future__ import annotations

import asyncio

import pytest
from mcp.server import MCPServer

from mcp_servers.http_runtime import (
    FileBackedBearerTokenVerifier,
    HTTP_PATH_ENV,
    HTTP_PORT_ENV,
    ISSUER_URL_ENV,
    MCPRuntimeConfigError,
    RESOURCE_URL_ENV,
    SCOPE_ENV,
    TOKEN_ENV,
    StaticBearerTokenVerifier,
    build_http_runtime_config,
    create_server_for_transport,
    read_transport,
)


def http_env(**overrides: str) -> dict[str, str]:
    env = {
        TOKEN_ENV: "t" * 40,
        ISSUER_URL_ENV: "https://issuer.example.test",
        RESOURCE_URL_ENV: "https://mcp.example.test/source",
        SCOPE_ENV: "source_ro:read",
    }
    env.update(overrides)
    return env


async def verify(verifier: StaticBearerTokenVerifier, token: str):
    return await verifier.verify_token(token)


def test_file_backed_token_verifier_rotates_without_server_restart(tmp_path):
    token_file = tmp_path / "active-token"
    first = "a" * 40
    second = "b" * 40
    token_file.write_text(first, encoding="utf-8")
    token_file.chmod(0o600)
    verifier = FileBackedBearerTokenVerifier(
        token_file=token_file,
        scopes=["resbench:episode"],
        resource="http://127.0.0.1:8000/mcp",
    )

    assert asyncio.run(verifier.verify_token(first)) is not None
    token_file.write_text(second, encoding="utf-8")
    token_file.chmod(0o600)
    assert asyncio.run(verifier.verify_token(first)) is None
    assert asyncio.run(verifier.verify_token(second)) is not None


def test_static_bearer_token_verifier_accepts_only_exact_token_without_returning_secret():
    verifier = StaticBearerTokenVerifier(
        token="t" * 40,
        scopes=["source_ro:read"],
        resource="https://mcp.example.test/source",
    )

    accepted = asyncio.run(verify(verifier, "t" * 40))
    rejected = asyncio.run(verify(verifier, "wrong-token"))

    assert accepted is not None
    assert accepted.token == "<redacted>"
    assert accepted.scopes == ["source_ro:read"]
    assert accepted.resource == "https://mcp.example.test/source"
    assert rejected is None


def test_http_runtime_requires_token_and_does_not_leak_configured_token():
    env = http_env()
    secret = env.pop(TOKEN_ENV)

    with pytest.raises(MCPRuntimeConfigError) as exc:
        build_http_runtime_config(env)

    message = str(exc.value)
    assert TOKEN_ENV in message
    assert secret not in message


def test_http_runtime_requires_scope():
    env = http_env()
    env.pop(SCOPE_ENV)

    with pytest.raises(MCPRuntimeConfigError) as exc:
        build_http_runtime_config(env)

    assert SCOPE_ENV in str(exc.value)


@pytest.mark.parametrize("token", ["short", " " + "t" * 40, "t" * 20 + "\n" + "t" * 20])
def test_http_runtime_rejects_weak_or_whitespace_tokens(token: str):
    with pytest.raises(MCPRuntimeConfigError, match="at least"):
        build_http_runtime_config(http_env(**{TOKEN_ENV: token}))


def test_stdio_transport_does_not_require_token():
    def factory():
        return MCPServer(name="test_stdio")

    server, config = create_server_for_transport(factory, transport="stdio", env={})

    assert isinstance(server, MCPServer)
    assert config is None
    assert read_transport({}) == "stdio"


def test_sse_transport_uses_authenticated_http_runtime():
    server, config = create_server_for_transport(
        lambda **kwargs: MCPServer(name="test_sse", **kwargs),
        transport="sse",
        env=http_env(**{HTTP_PATH_ENV: "/sse", HTTP_PORT_ENV: "18183"}),
    )

    assert server.name == "test_sse"
    assert config is not None
    assert config.sse_kwargs() == {
        "host": "127.0.0.1",
        "port": 18183,
        "sse_path": "/sse",
        "message_path": "/messages/",
    }
    assert read_transport({"RESBENCH_MCP_TRANSPORT": "sse"}) == "sse"


def test_http_runtime_builds_auth_settings_and_safe_defaults():
    config = build_http_runtime_config(http_env())

    assert config.host == "127.0.0.1"
    assert config.port == 8000
    assert config.path == "/mcp"
    assert [str(scope) for scope in config.auth.required_scopes] == ["source_ro:read"]
    assert str(config.auth.issuer_url).rstrip("/") == "https://issuer.example.test"
    assert str(config.auth.resource_server_url).rstrip("/") == "https://mcp.example.test/source"


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        (HTTP_PORT_ENV, "not-a-port", "integer"),
        (HTTP_PORT_ENV, "80", "between 1024 and 65535"),
        (HTTP_PATH_ENV, "mcp", "start with /"),
        (HTTP_PATH_ENV, "/../mcp", "clean URL path"),
        (HTTP_PATH_ENV, "/mcp?token=bad", "clean URL path"),
        (ISSUER_URL_ENV, "not-a-url", "explicit"),
        (RESOURCE_URL_ENV, "https://user:pass@example.test/mcp", "must not contain credentials"),
        (RESOURCE_URL_ENV, "https://example.test/mcp?token=bad", "must not contain a query"),
        (SCOPE_ENV, "source_ro:read bad?scope", "invalid scope"),
    ],
)
def test_http_runtime_rejects_unsafe_parameters(key: str, value: str, expected: str):
    with pytest.raises(MCPRuntimeConfigError, match=expected):
        build_http_runtime_config(http_env(**{key: value}))


def test_http_runtime_rejects_non_loopback_host_by_default():
    with pytest.raises(MCPRuntimeConfigError, match="loopback"):
        build_http_runtime_config(http_env(RESBENCH_MCP_HTTP_HOST="0.0.0.0"))


def test_http_runtime_allows_non_loopback_only_with_explicit_flag():
    config = build_http_runtime_config(
        http_env(
            RESBENCH_MCP_HTTP_HOST="0.0.0.0",
            RESBENCH_MCP_HTTP_ALLOW_NON_LOOPBACK="true",
        )
    )

    assert config.host == "0.0.0.0"
