from __future__ import annotations

import asyncio
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
