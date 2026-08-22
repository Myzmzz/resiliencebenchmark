import json
from pathlib import Path

from scripts import probe_models


PRODUCTION_MODELS = Path("harness/models.yaml")


def test_production_registry_uses_requested_glm_and_minimax_aliases():
    models = probe_models.load_models_config(PRODUCTION_MODELS)["models"]

    assert "glm-5.3" in models
    assert "MiniMax-M3" in models
    assert "glm-4.5" not in models
    assert "MiniMax-M1" not in models


def write_models_config(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "version: test/v1",
                "defaults:",
                "  capability_probe:",
                "    timeout_seconds: 5",
                "models:",
                "  gpt-5.6:",
                "    upstream_model: gpt-5.6",
                "    display_name: GPT-5.6",
                "    protocol_candidates: [openai_chat_completions]",
                "  claude-opus-5:",
                "    upstream_model: claude-opus-5",
                "    display_name: Claude Opus 5",
                "    protocol_candidates: [anthropic_messages, openai_chat_completions]",
            ]
        ),
        encoding="utf-8",
    )


class FakeTransport:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body, "timeout": timeout})
        if method == "GET" and url.endswith("/models"):
            return response({"data": [{"id": "gpt-5.6"}, {"id": "claude-opus-5"}]})
        payload = json.loads(body.decode("utf-8")) if body else {}
        if url.endswith("/messages"):
            return response({"id": "msg_1", "model": payload["model"], "content": [{"type": "text", "text": "ok"}]})
        if payload.get("stream"):
            return probe_models.HttpResponse(
                status=200,
                headers={"content-type": "text/event-stream"},
                body=b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n',
                elapsed_ms=7,
            )
        if "tools" in payload:
            count = len(payload["tools"])
            tool_calls = [
                {
                    "id": f"call_{idx}",
                    "type": "function",
                    "function": {"name": tool["function"]["name"], "arguments": '{"signal":"ok"}'},
                }
                for idx, tool in enumerate(payload["tools"])
            ]
            return response({"model": payload["model"], "choices": [{"message": {"tool_calls": tool_calls[:count]}}]})
        if payload.get("response_format"):
            return response(
                {
                    "model": payload["model"],
                    "choices": [{"message": {"content": '{"probe":"ok","supported":true}'}}],
                }
            )
        return response({"model": payload["model"], "choices": [{"message": {"content": "ok"}}]})


def response(payload, status=200):
    return probe_models.HttpResponse(
        status=status,
        headers={"content-type": "application/json"},
        body=json.dumps(payload).encode("utf-8"),
        elapsed_ms=3,
    )


def test_dry_run_reads_aliases_without_network(tmp_path):
    config = tmp_path / "models.yaml"
    write_models_config(config)
    fake = FakeTransport()

    report = probe_models.run_probe(config, {}, dry_run=True, transport=fake)

    assert report["dryRun"] is True
    assert [model["alias"] for model in report["models"]] == ["gpt-5.6", "claude-opus-5"]
    assert all(probe["status"] == "planned" for model in report["models"] for probe in model["probes"])
    assert fake.calls == []


def test_probe_uses_env_credentials_without_reporting_values(tmp_path):
    config = tmp_path / "models.yaml"
    write_models_config(config)
    fake = FakeTransport()
    env = {
        probe_models.BASE_URL_ENV: "https://gateway.example/v1",
        probe_models.API_KEY_ENV: "sk-test-secret-value-that-must-not-leak",
    }

    report = probe_models.run_probe(config, env, aliases=["gpt-5.6"], transport=fake)
    encoded = json.dumps(report)

    assert report["issues"] == []
    assert report["credentialSources"]["apiKey"] == {"source": "env:RESBENCH_LLM_API_KEY", "present": True}
    assert env[probe_models.API_KEY_ENV] not in encoded
    assert env[probe_models.BASE_URL_ENV] not in encoded
    assert fake.calls
    assert all(call["headers"]["authorization"] == f"Bearer {env[probe_models.API_KEY_ENV]}" for call in fake.calls)


def test_probe_records_core_capabilities_with_fake_transport(tmp_path):
    config = tmp_path / "models.yaml"
    write_models_config(config)

    report = probe_models.run_probe(
        config,
        {probe_models.BASE_URL_ENV: "https://gateway.example/v1", probe_models.API_KEY_ENV: "secret"},
        aliases=["gpt-5.6"],
        transport=FakeTransport(),
    )

    model = report["models"][0]
    assert model["overallStatus"] == "supported"
    assert model["capabilities"]["aliasResolved"] is True
    assert model["capabilities"]["openaiChatCompletions"] is True
    assert model["capabilities"]["streaming"] is True
    assert model["capabilities"]["singleToolCall"] is True
    assert model["capabilities"]["parallelToolCalls"] is True
    assert model["capabilities"]["structuredJsonOutput"] is True


def test_anthropic_failure_is_protocol_only_not_model_failure(tmp_path):
    config = tmp_path / "models.yaml"
    write_models_config(config)

    class AnthropicUnsupportedTransport(FakeTransport):
        def __call__(self, method, url, headers, body, timeout):
            if url.endswith("/messages"):
                self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
                return response({"error": {"message": "not found"}}, status=404)
            return super().__call__(method, url, headers, body, timeout)

    report = probe_models.run_probe(
        config,
        {probe_models.BASE_URL_ENV: "https://gateway.example/v1", probe_models.API_KEY_ENV: "secret"},
        aliases=["claude-opus-5"],
        transport=AnthropicUnsupportedTransport(),
    )

    model = report["models"][0]
    anthropic = [probe for probe in model["probes"] if probe["protocol"] == "anthropic_messages"][0]
    assert anthropic["status"] == "unsupported"
    assert anthropic["modelFailureImpact"] == "protocol_only"
    assert model["overallStatus"] == "supported"


def test_missing_env_is_structured_error_and_no_probe(tmp_path):
    config = tmp_path / "models.yaml"
    write_models_config(config)
    fake = FakeTransport()

    report = probe_models.run_probe(config, {}, transport=fake)

    assert report["issues"] == [{"severity": "ERROR", "message": "RESBENCH_LLM_BASE_URL is required"}]
    assert report["models"] == []
    assert fake.calls == []


def test_probe_rejects_remote_plain_http_before_sending_key(tmp_path):
    config = tmp_path / "models.yaml"
    write_models_config(config)
    fake = FakeTransport()

    report = probe_models.run_probe(
        config,
        {probe_models.BASE_URL_ENV: "http://gateway.example/v1", probe_models.API_KEY_ENV: "runtime-key"},
        transport=fake,
    )

    assert report["issues"] == [
        {"severity": "ERROR", "message": "RESBENCH_LLM_BASE_URL must use HTTPS unless it targets loopback"}
    ]
    assert fake.calls == []


def test_transport_errors_do_not_echo_gateway_or_key(tmp_path):
    config = tmp_path / "models.yaml"
    write_models_config(config)
    gateway = "https://private-gateway.example/v1"
    key = "runtime-key-that-must-not-leak"

    def failing_transport(method, url, headers, body, timeout):
        raise RuntimeError(f"failed at {url} using {headers.get('authorization')}")

    report = probe_models.run_probe(
        config,
        {probe_models.BASE_URL_ENV: gateway, probe_models.API_KEY_ENV: key},
        aliases=["gpt-5.6"],
        transport=failing_transport,
    )
    encoded = json.dumps(report)

    assert gateway not in encoded
    assert key not in encoded
    assert "Bearer" not in encoded


def test_cli_outputs_redacted_json(tmp_path, monkeypatch, capsys):
    config = tmp_path / "models.yaml"
    write_models_config(config)
    monkeypatch.setenv(probe_models.BASE_URL_ENV, "https://gateway.example/v1")
    monkeypatch.setenv(probe_models.API_KEY_ENV, "sk-cli-secret-value")

    rc = probe_models.main(["--models-config", str(config), "--model", "gpt-5.6"], transport=FakeTransport())

    captured = capsys.readouterr()
    assert rc == 0
    assert "sk-cli-secret-value" not in captured.out
    assert "https://gateway.example/v1" not in captured.out
    report = json.loads(captured.out)
    assert report["models"][0]["alias"] == "gpt-5.6"
