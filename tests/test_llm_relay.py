from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import replace

import httpx
import pytest
from starlette.testclient import TestClient

from stage2_service.llm_relay import (
    TrialRelay,
    TrialRelayConfig,
    create_trial_relay_app,
    relay_uvicorn_config,
)


def relay(handler, **overrides):
    config = TrialRelayConfig.issue(
        trial_id="trial-one",
        model_alias="gpt-5.5",
        upstream_base_url="http://private-gateway:4000/v1",
        upstream_api_key="upstream-master-secret",
        relay_token="trial-only-token",
        **overrides,
    )
    transport = httpx.MockTransport(handler)
    return config, TestClient(
        create_trial_relay_app(
            config,
            client_factory=lambda: httpx.AsyncClient(
                transport=transport, base_url="http://private-gateway", follow_redirects=False
            ),
        )
    )


def test_trial_relay_forwards_only_authorized_inference_with_private_upstream_key():
    captured = {}

    def upstream(request: httpx.Request):
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        captured["x_api_key"] = request.headers.get("x-api-key")
        captured["body"] = request.content
        return httpx.Response(
            200,
            headers={
                "content-type": "application/json",
                "authorization": "Bearer reflected-secret",
                "x-api-key": "reflected-key",
            },
            stream=_Stream(_one_chunk(b'{"id":"resp-1","model":"gpt-5.5"}')),
        )

    config, client = relay(upstream)
    response = client.post(
        "/v1/responses",
        headers={"Authorization": "Bearer trial-only-token", "X-API-Key": "agent-key"},
        json={"model": "gpt-5.5", "input": "hello"},
    )

    assert response.status_code == 200
    assert captured["url"] == "http://private-gateway:4000/v1/responses"
    assert captured["authorization"] == "Bearer upstream-master-secret"
    assert captured["x_api_key"] is None
    assert b"trial-only-token" not in captured["body"]
    assert "authorization" not in response.headers
    assert "x-api-key" not in response.headers
    assert config.agent_environment() == {
        "RESBENCH_LLM_BASE_URL": "http://127.0.0.1:18090/v1",
        "RESBENCH_LLM_API_KEY": "trial-only-token",
    }


def test_relay_rejects_cross_trial_token_model_and_admin_or_history_paths():
    _config, client = relay(lambda request: httpx.Response(200, json={}))
    payload = {"model": "gpt-5.5", "messages": []}

    assert client.post("/v1/chat/completions", json=payload).status_code == 401
    assert client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer other-trial-token"}, json=payload,
    ).status_code == 401
    assert client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer trial-only-token"},
        json={**payload, "model": "gateway-admin"},
    ).status_code == 403
    assert client.get("/v1/models", headers={"Authorization": "Bearer trial-only-token"}).status_code == 404
    assert client.get("/v1/responses/resp-1", headers={"Authorization": "Bearer trial-only-token"}).status_code == 404
    assert client.post("/key/generate", headers={"Authorization": "Bearer trial-only-token"}, json={}).status_code == 404
    assert client.post("/v1/responses/../keys", headers={"Authorization": "Bearer trial-only-token"}, json=payload).status_code == 404
    assert client.post("/v1/responses%2F..%2Fkeys", headers={"Authorization": "Bearer trial-only-token"}, json=payload).status_code == 404
    assert client.post("/v1/responses?model=gateway-admin", headers={"Authorization": "Bearer trial-only-token"}, json=payload).status_code == 400


def test_relay_preserves_claude_messages_beta_query_and_protocol_headers():
    captured = []
    _config, client = relay(lambda request: captured.append(request) or httpx.Response(200, stream=_Stream(_one_chunk(b"{}"))))
    response = client.post(
        "/v1/messages?beta=true",
        headers={"Authorization": "Bearer trial-only-token", "X-API-Key": "agent-key", "anthropic-version": "2023-06-01",
                 "anthropic-beta": "fixture-beta", "x-private-agent-header": "must-not-forward"},
        json={"model": "gpt-5.5", "messages": []},
    )
    assert response.status_code == 200
    assert str(captured[0].url) == "http://private-gateway:4000/v1/messages?beta=true"
    assert captured[0].headers["anthropic-version"] == "2023-06-01"
    assert captured[0].headers["anthropic-beta"] == "fixture-beta"
    assert captured[0].headers["authorization"] == "Bearer upstream-master-secret"
    assert "x-api-key" not in captured[0].headers
    assert "x-private-agent-header" not in captured[0].headers


@pytest.mark.parametrize("path", ["/v1/messages?beta=false", "/v1/messages?beta=true&beta=true",
    "/v1/messages?beta=true&model=other", "/v1/messages?api_key=stolen", "/v1/responses?beta=true",
    "/v1/chat/completions?beta=true"])
def test_relay_beta_support_does_not_open_other_queries(path):
    calls = []
    config, client = relay(lambda request: calls.append(request) or httpx.Response(200))
    response = client.post(path, headers={"Authorization": "Bearer trial-only-token"},
                           json={"model": "gpt-5.5", "messages": []})
    assert response.status_code == 400
    assert calls == [] and config.request_ids == []


@pytest.mark.parametrize("harness", ["codex", "claude-code", "deepseek-harness", "bladeai"])
def test_relay_owns_audit_identity_and_never_forwards_forged_agent_headers(harness):
    received = []

    def upstream(request):
        received.append(dict(request.headers))
        return httpx.Response(200, stream=_Stream(_one_chunk(b"{}")))

    config, client = relay(upstream, harness_name=harness, gateway_config_sha256="a" * 64)
    for _ in range(2):
        response = client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer trial-only-token",
                     "x-resbench-trial-id": "forged-trial", "x-resbench-harness": "forged-agent",
                     "x-resbench-model-alias": "forged-model", "x-resbench-request-id": "forged-id",
                     "x-resbench-gateway-config-sha256": "forged-version"},
            json={"model": "gpt-5.5", "input": "ordinary task"},
        )
        assert response.status_code == 200
    assert len(config.request_ids) == len(set(config.request_ids)) == 2
    for headers, request_id in zip(received, config.request_ids):
        assert headers["x-resbench-trial-id"] == "trial-one"
        assert headers["x-resbench-harness"] == harness
        assert headers["x-resbench-model-alias"] == "gpt-5.5"
        assert headers["x-resbench-request-id"] == request_id != "forged-id"
        assert headers["x-resbench-gateway-config-sha256"] == "a" * 64


@pytest.mark.parametrize("field", ["api_base", "base_url", "api_key", "custom_llm_provider", "litellm_params", "extra_headers", "fallbacks", "proxy_server_request"])
def test_relay_rejects_top_level_route_and_credential_overrides(field):
    calls = []
    _config, client = relay(lambda request: calls.append(request) or httpx.Response(200))
    response = client.post(
        "/v1/chat/completions", headers={"Authorization": "Bearer trial-only-token"},
        json={"model": "gpt-5.5", "messages": [], field: {"override": "x"}},
    )
    assert response.status_code == 403
    assert response.json()["error"]["message"] == "request_routing_parameter_forbidden"
    assert calls == []


def test_relay_does_not_scan_user_text_or_source_code_for_route_words():
    captured = {}
    def upstream(request: httpx.Request):
        captured["body"] = request.content
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=_Stream(_one_chunk(b'{"id":"ok"}')))
    _config, client = relay(upstream)
    text = "api_key='ordinary source example'; base_url='documentation'"
    response = client.post("/v1/responses", headers={"Authorization": "Bearer trial-only-token"}, json={"model": "gpt-5.5", "input": text})
    assert response.status_code == 200
    assert text.encode() in captured["body"]


def test_relay_preserves_sse_streaming_without_forwarding_client_authorization():
    async def stream():
        yield b"data: first\n\n"
        yield b"data: [DONE]\n\n"

    def upstream(request: httpx.Request):
        assert request.headers["authorization"] == "Bearer upstream-master-secret"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream", "x-request-id": "upstream-1"},
            stream=_Stream(stream()),
        )

    _config, client = relay(upstream)
    response = client.post(
        "/v1/messages",
        headers={"Authorization": "Bearer trial-only-token", "Accept": "text/event-stream"},
        json={"model": "gpt-5.5", "stream": True, "messages": []},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-request-id"] == "upstream-1"
    assert response.content == b"data: first\n\ndata: [DONE]\n\n"


def test_relay_bounds_body_and_does_not_follow_upstream_redirects():
    _config, client = relay(
        lambda request: httpx.Response(302, headers={"location": "http://elsewhere/v1/responses"}),
        max_request_bytes=32,
    )
    headers = {"Authorization": "Bearer trial-only-token"}
    assert client.post("/v1/responses", headers=headers, content=b"x" * 33).status_code == 413
    redirected = client.post("/v1/responses", headers=headers, json={"model": "gpt-5.5"})
    assert redirected.status_code == 502
    assert redirected.json()["error"]["message"] == "upstream_redirect_rejected"


def test_chunked_body_over_limit_is_rejected_before_the_upstream_is_contacted():
    calls = []
    _config, client = relay(
        lambda request: calls.append(request) or httpx.Response(200),
        max_request_bytes=32,
    )

    def chunks():
        yield b"a" * 20
        yield b"b" * 20

    response = client.post(
        "/v1/responses",
        headers={"Authorization": "Bearer trial-only-token", "Transfer-Encoding": "chunked"},
        content=chunks(),
    )

    assert response.status_code == 413
    assert calls == []


def test_relay_server_entrypoint_is_fixed_to_loopback_port():
    config, _client = relay(lambda request: httpx.Response(200))
    server_config = relay_uvicorn_config(config)
    assert server_config.host == "127.0.0.1"
    assert server_config.port == 18090
    try:
        create_trial_relay_app(replace(config, host="0.0.0.0"))
    except ValueError as exc:
        assert "127.0.0.1:18090" in str(exc)
    else:
        raise AssertionError("non-loopback binding must be rejected")


def test_managed_trial_relay_prebinds_an_ephemeral_test_socket_and_stops_its_server():
    async def one_chunk():
        yield b'{"id":"ok"}'

    def upstream(_request: httpx.Request):
        return httpx.Response(
            200, headers={"content-type": "application/json"}, stream=_Stream(one_chunk())
        )

    config, _client = relay(upstream)
    config = replace(config, port=0, allow_ephemeral_port_for_tests=True)
    transport = httpx.MockTransport(upstream)
    managed = TrialRelay(
        config,
        client_factory=lambda: httpx.AsyncClient(transport=transport, base_url="http://private-gateway"),
    )
    with managed as running:
        environment = running.agent_environment()
        response = httpx.post(
            f"{environment['RESBENCH_LLM_BASE_URL']}/responses",
            headers={"Authorization": f"Bearer {environment['RESBENCH_LLM_API_KEY']}"},
            json={"model": "gpt-5.5", "input": "local"},
            timeout=2,
        )
        assert response.status_code == 200
        port = running.port
    assert managed._thread is None
    assert port != 0


def test_managed_relay_releases_prebound_socket_when_app_creation_fails(monkeypatch):
    config, _client = relay(lambda request: httpx.Response(200))
    config = replace(config, port=0, allow_ephemeral_port_for_tests=True)
    managed = TrialRelay(config)
    monkeypatch.setattr(
        "stage2_service.llm_relay.create_trial_relay_app",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("invalid relay config")),
    )

    with pytest.raises(RuntimeError, match="invalid relay config"):
        managed.start()

    assert managed._socket is None
    assert managed._thread is None


class _HangingServer:
    """A uvicorn stand-in whose graceful shutdown waits on an open request."""

    def __init__(self):
        self.should_exit = False
        self.force_exit = False


def test_relay_close_forces_exit_when_graceful_shutdown_hangs():
    # 2026-09-10 L2xC0: a cancelled Agent left a request open, graceful
    # shutdown never finished, and the campaign failed before scoring.
    config, _client = relay(lambda request: httpx.Response(200))
    managed = TrialRelay(config, shutdown_timeout_seconds=0.05)
    server = _HangingServer()

    def serve():
        while not server.force_exit:
            time.sleep(0.01)

    managed._server = server
    managed._thread = threading.Thread(target=serve, daemon=True)
    managed._thread.start()

    managed.close()

    assert server.should_exit is True
    assert server.force_exit is True
    assert managed._thread is None


def test_relay_close_still_reports_a_server_that_never_stops():
    config, _client = relay(lambda request: httpx.Response(200))
    managed = TrialRelay(config, shutdown_timeout_seconds=0.05)
    released = threading.Event()
    managed._server = _HangingServer()
    managed._thread = threading.Thread(target=released.wait, daemon=True)
    managed._thread.start()
    try:
        with pytest.raises(RuntimeError, match="did not terminate"):
            managed.close()
    finally:
        released.set()


class _Stream(httpx.AsyncByteStream):
    def __init__(self, generator):
        self.generator = generator

    async def __aiter__(self):
        async for item in self.generator:
            yield item

    async def aclose(self):
        await asyncio.sleep(0)


async def _one_chunk(value: bytes):
    yield value


# --- 被服务化的 Harness 用预置令牌鉴权 ------------------------------------
#
# 2026-09-13 真实 L0 的实测：BladeAI 0.7.0 是先于试验启动、活得比试验长的
# 服务，其公开 API 明确拒绝写 llm_api_key（code 1002，"not writable via the
# HTTP API"），平台因此没法把每轮现发的令牌交给它。不解决就每场判
# CASE_INVALID / GATEWAY_EVIDENCE_MISSING，每个节点 BLOCKED_BY_PLATFORM 零分。
#
# 口径：本轮 relay 额外接受它已持有的那把凭据。request id 照发、审计行照写、
# 模型别名照校验——取证链一条不少；代价是它自带凭据，这一点写进评估报告。


def _ok_upstream(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "application/json"},
        stream=_Stream(_one_chunk(b'{"id":"resp-1","model":"gpt-5.5"}')),
    )


def _post(client, token: str):
    return client.post(
        "/v1/responses",
        headers={"authorization": f"Bearer {token}", "content-type": "application/json"},
        content=b'{"model": "gpt-5.5", "input": "hi"}',
    )


def test_served_harness_token_is_accepted_alongside_the_trial_token():
    config, client = relay(_ok_upstream, served_harness_token="preshared-served-key")
    assert _post(client, "preshared-served-key").status_code == 200
    assert _post(client, config.relay_token).status_code == 200


def test_a_wrong_token_is_still_rejected_when_a_served_token_is_configured():
    _, client = relay(_ok_upstream, served_harness_token="preshared-served-key")
    assert _post(client, "some-other-token").status_code == 401


def test_the_served_token_still_mints_request_ids_and_enforces_the_model_alias():
    """取证链不能因为换了把钥匙就断——这正是加这条的理由。"""
    config, client = relay(_ok_upstream, served_harness_token="preshared-served-key")
    assert _post(client, "preshared-served-key").status_code == 200
    assert len(config.request_ids) == 1 and config.request_ids[0]

    wrong_model = client.post(
        "/v1/responses",
        headers={"authorization": "Bearer preshared-served-key",
                 "content-type": "application/json"},
        content=b'{"model": "some-other-model", "input": "hi"}',
    )
    assert wrong_model.status_code == 403


def test_the_other_three_harnesses_are_unchanged_because_the_field_stays_empty():
    """子进程 Harness 不配这个字段，行为与加它之前逐字节相同。"""
    config, client = relay(_ok_upstream)
    assert config.served_harness_token == ""
    assert _post(client, config.relay_token).status_code == 200
    assert _post(client, "anything-else").status_code == 401
    # 空的预置令牌不能授权任何东西，空 Bearer 头也不行
    assert _post(client, "").status_code == 401


def _post_messages_with_api_key(client, token: str, path: str = "/v1/messages"):
    """An Anthropic-SDK client: the API key travels in x-api-key, never as Bearer."""
    return client.post(
        path,
        headers={"x-api-key": token, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": "gpt-5.5", "messages": []},
    )


def test_messages_accept_the_trial_token_in_x_api_key_and_never_forward_it():
    """DSH's pi-ai anthropic-messages client sends only x-api-key (2026-09-21).

    Before this, every DSH Trial on claude-opus-5 got 401 from the relay and
    never reached the gateway, so the platform saw no gateway evidence at all.
    """
    captured = []
    config, client = relay(lambda request: captured.append(request) or httpx.Response(200, stream=_Stream(_one_chunk(b"{}"))))

    response = _post_messages_with_api_key(client, config.relay_token)

    assert response.status_code == 200
    assert captured[0].headers["authorization"] == "Bearer upstream-master-secret"
    assert "x-api-key" not in captured[0].headers


def test_messages_reject_a_wrong_token_in_x_api_key():
    _config, client = relay(_ok_upstream)

    assert _post_messages_with_api_key(client, "some-other-token").status_code == 401
    assert _post_messages_with_api_key(client, "").status_code == 401


@pytest.mark.parametrize("path", ["/v1/responses", "/v1/chat/completions"])
def test_openai_protocol_paths_still_require_bearer(path):
    """x-api-key is the Anthropic protocol's header; OpenAI-protocol paths keep Bearer only."""
    config, client = relay(_ok_upstream)

    assert _post_messages_with_api_key(client, config.relay_token, path=path).status_code == 401


def test_messages_accept_the_served_token_in_x_api_key_only_when_one_is_configured():
    _config, client = relay(_ok_upstream, served_harness_token="preshared-served-key")
    assert _post_messages_with_api_key(client, "preshared-served-key").status_code == 200

    _config, client = relay(_ok_upstream)
    assert _post_messages_with_api_key(client, "preshared-served-key").status_code == 401


# --- Codex tool namespaces across the Anthropic bridge (2026-09-21) ---------
#
# Codex 0.155 sends each MCP server's tools as one Responses "namespace" tool
# and routes a call by (namespace, name).  LiteLLM 1.92.0's Responses ->
# Anthropic bridge drops the namespace, so every Codex x claude-opus-5 MCP call
# failed with "unsupported call: <name>".  For such routes the relay expands the
# namespaces on the way up and restores them on the way down.

from stage2_service.llm_relay import flatten_responses_tool_namespaces  # noqa: E402

_NAMESPACED_REQUEST = {
    "model": "gpt-5.5",
    "stream": True,
    "input": [
        {"type": "message", "role": "user", "content": "check the pod"},
        {"type": "function_call", "call_id": "c0", "namespace": "mcp__k8s_ro", "name": "k8s_list_resources", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c0", "output": "[]"},
    ],
    "tools": [
        {"type": "function", "name": "list_mcp_resources", "parameters": {"type": "object"}},
        {"type": "namespace", "name": "mcp__k8s_ro", "description": "k8s", "tools": [
            {"type": "function", "name": "k8s_list_resources", "parameters": {"type": "object"}},
            {"type": "function", "name": "k8s_get_resource", "parameters": {"type": "object"}},
        ]},
        {"type": "namespace", "name": "mcp__harness_channel", "description": "channel", "tools": [
            {"type": "function", "name": "harness_poll_notices", "parameters": {"type": "object"}},
        ]},
    ],
}


def _sse(*events) -> bytes:
    return b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events)


def test_namespace_tools_are_expanded_with_their_names_unchanged():
    captured = []
    config, client = relay(
        lambda request: captured.append(request) or httpx.Response(200, headers={"content-type": "application/json"}, stream=_Stream(_one_chunk(b'{"output": []}'))),
        flatten_tool_namespaces=True,
    )

    response = client.post("/v1/responses", headers={"authorization": f"Bearer {config.relay_token}"}, json=_NAMESPACED_REQUEST)

    assert response.status_code == 200
    sent = json.loads(captured[0].content)
    assert [t.get("type") for t in sent["tools"]] == ["function"] * 4
    assert [t["name"] for t in sent["tools"]] == ["list_mcp_resources", "k8s_list_resources", "k8s_get_resource", "harness_poll_notices"]
    assert "namespace" not in sent["input"][1]


def test_streamed_function_calls_get_their_namespace_back_even_across_chunk_boundaries():
    done = {"type": "response.output_item.done", "item": {"type": "function_call", "call_id": "c1", "name": "harness_poll_notices", "arguments": "{}"}}
    completed = {"type": "response.completed", "response": {"output": [
        {"type": "message", "content": []},
        {"type": "function_call", "call_id": "c1", "name": "harness_poll_notices", "arguments": "{}"},
    ]}}
    text_delta = {"type": "response.output_text.delta", "delta": "thinking"}
    body = _sse(text_delta, done, completed)
    cut = body.index(b"harness_poll") + 4  # split one data line across two network chunks

    async def two_chunks():
        yield body[:cut]
        yield body[cut:]

    config, client = relay(
        lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_Stream(two_chunks())),
        flatten_tool_namespaces=True,
    )
    response = client.post("/v1/responses", headers={"authorization": f"Bearer {config.relay_token}"}, json=_NAMESPACED_REQUEST)

    events = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]
    assert events[0] == text_delta  # unrelated events pass through unchanged
    assert events[1]["item"]["namespace"] == "mcp__harness_channel"
    assert events[2]["response"]["output"][1]["namespace"] == "mcp__harness_channel"
    assert "namespace" not in events[2]["response"]["output"][0]


def test_a_non_streamed_response_gets_its_namespace_back():
    body = {"output": [{"type": "function_call", "call_id": "c1", "name": "k8s_get_resource", "arguments": "{}"}]}
    config, client = relay(
        lambda request: httpx.Response(200, headers={"content-type": "application/json"}, stream=_Stream(_one_chunk(json.dumps(body).encode()))),
        flatten_tool_namespaces=True,
    )
    response = client.post("/v1/responses", headers={"authorization": f"Bearer {config.relay_token}"}, json=dict(_NAMESPACED_REQUEST, stream=False))

    assert response.json()["output"][0]["namespace"] == "mcp__k8s_ro"


def test_native_responses_routes_are_forwarded_byte_for_byte():
    """qwen / deepseek / gpt-5.6-sol keep the namespace natively; their traffic must not change."""
    captured = []
    upstream_body = _sse({"type": "response.output_item.done", "item": {"type": "function_call", "name": "harness_poll_notices"}})
    config, client = relay(lambda request: captured.append(request) or httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_Stream(_one_chunk(upstream_body))))
    raw = json.dumps(_NAMESPACED_REQUEST).encode()

    response = client.post("/v1/responses", headers={"authorization": f"Bearer {config.relay_token}", "content-type": "application/json"}, content=raw)

    assert captured[0].content == raw
    assert response.content == upstream_body


def test_the_messages_path_is_never_rewritten_even_on_a_bridged_route():
    captured = []
    config, client = relay(lambda request: captured.append(request) or httpx.Response(200, stream=_Stream(_one_chunk(b"{}"))), flatten_tool_namespaces=True)
    raw = json.dumps(dict(_NAMESPACED_REQUEST, messages=[])).encode()

    client.post("/v1/messages", headers={"authorization": f"Bearer {config.relay_token}", "content-type": "application/json"}, content=raw)

    assert captured[0].content == raw


def test_a_name_that_would_be_ambiguous_after_expansion_leaves_the_request_unchanged():
    payload = json.loads(json.dumps(_NAMESPACED_REQUEST))
    payload["tools"].append({"type": "namespace", "name": "mcp__other", "tools": [{"type": "function", "name": "k8s_get_resource"}]})
    before = json.dumps(payload)

    assert flatten_responses_tool_namespaces(payload) is None
    assert json.dumps(payload) == before


def test_a_request_without_namespaces_is_left_alone():
    payload = {"model": "gpt-5.5", "tools": [{"type": "function", "name": "f"}]}
    assert flatten_responses_tool_namespaces(payload) is None
