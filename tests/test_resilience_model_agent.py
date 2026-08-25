from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from resilience_agent.agent import ResilienceAnalysisAgent
from resilience_agent.agent_loop import AgentLoopError, run_agent_loop
from resilience_agent.analysis_tools import ProjectAnalysisTools, ToolInputError
from resilience_agent.common import load_document, stable_id
from resilience_agent.defect_identification import identify_defects
from resilience_agent.episode_design import design_episodes
from resilience_agent.model_client import (
    ModelClientError,
    ModelConfig,
    ModelTransportError,
    ModelTurn,
    ResponsesModelClient,
    ToolCall,
    _default_http_post,
    load_model_config,
)
from resilience_agent.model_reasoning import (
    EvidenceValidationError,
    analyze_defects_with_model,
    review_episode_designs_with_model,
)
from resilience_agent.pipeline import PACKAGE_ROOT, REPO_ROOT, TEMPLATE_ROOT


CATALOG = REPO_ROOT / "tasks/catalog/resilience-defect-classes.v0.1.yaml"
RULES = TEMPLATE_ROOT / "defect-matchers.v0.1.yaml"
MODEL_DEFECT_SCHEMA = PACKAGE_ROOT / "schemas/model-defect-assessment.schema.json"
MODEL_EPISODE_SCHEMA = PACKAGE_ROOT / "schemas/model-episode-review.schema.json"
TRAIN_TICKET = REPO_ROOT.parent / "benchmark-sources/materialized/train-ticket-upstream"
TRAIN_CONTEXT = REPO_ROOT / "artifacts/resilience-agent/train-ticket-static-20260823/system-context.yaml"


def model_config(*, max_rounds: int = 12) -> ModelConfig:
    return ModelConfig(
        provider="openai-compatible",
        protocol="responses",
        base_url="https://model.invalid/v1",
        credential="test-only",
        model="gpt-5.5",
        reasoning_effort="xhigh",
        store=False,
        max_tool_rounds=max_rounds,
    )


class ScriptedModel:
    def __init__(self, turns: list[ModelTurn | Exception], *, max_rounds: int = 12):
        self.config = model_config(max_rounds=max_rounds)
        self.turns = list(turns)
        self.requests: list[dict[str, Any]] = []

    def create_turn(self, **kwargs: Any) -> ModelTurn:
        self.requests.append(kwargs)
        if not self.turns:
            raise AssertionError("scripted model has no remaining turns")
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return turn


def tool_turn(name: str, arguments: dict[str, Any], call_id: str = "call-1") -> ModelTurn:
    item = {
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": json.dumps(arguments),
    }
    return ModelTurn(
        response_id=f"resp-{call_id}",
        resolved_model="gpt-5.5",
        status="completed",
        output_items=[item],
        tool_calls=[ToolCall(call_id=call_id, name=name, arguments=arguments)],
        final_text=None,
        usage={"input_tokens": 10, "output_tokens": 3, "total_tokens": 13},
    )


def final_turn(value: dict[str, Any], response_id: str = "resp-final") -> ModelTurn:
    text = json.dumps(value, ensure_ascii=False)
    return ModelTurn(
        response_id=response_id,
        resolved_model="gpt-5.5",
        status="completed",
        output_items=[
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        tool_calls=[],
        final_text=text,
        usage={"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
    )


def empty_assessment() -> dict[str, Any]:
    return {
        "schema_version": "model-defect-assessment.v0.1",
        "analysis_summary": "No evidence-backed findings.",
        "findings": [],
        "rejected_seed_candidates": [],
        "coverage_notes": ["Synthetic model test."],
    }


def test_model_config_is_separate_and_requires_runtime_credentials() -> None:
    path = PACKAGE_ROOT / "config/model.yaml"
    with pytest.raises(ModelClientError, match="RESILIENCE_AGENT_LLM_API_KEY"):
        load_model_config(path, environ={}, keychain_reader=lambda service, account: None)
    config = load_model_config(
        path,
        environ={
            "RESILIENCE_AGENT_LLM_BASE_URL": "https://gateway.example/v1",
            "RESILIENCE_AGENT_LLM_API_KEY": "secret-test-value",
        },
    )
    assert config.model == "gpt-5.5"
    assert config.reasoning_effort == "xhigh"
    assert config.store is False
    assert "secret-test-value" not in repr(config)
    assert config.base_url_source == "env:RESILIENCE_AGENT_LLM_BASE_URL"
    assert config.credential_source == "env:RESILIENCE_AGENT_LLM_API_KEY"


def test_model_config_uses_default_gateway_and_dedicated_keychain() -> None:
    calls: list[tuple[str, str]] = []

    def keychain(service: str, account: str) -> str:
        calls.append((service, account))
        return "keychain-test-secret"

    config = load_model_config(
        PACKAGE_ROOT / "config/model.yaml",
        environ={"USER": "tester"},
        keychain_reader=keychain,
    )
    assert config.base_url == "https://api.nexustokenai.com"
    assert config.base_url_source == "config:base_url_default"
    assert config.credential_source == "keychain:resilience-agent-llm"
    assert calls == [("resilience-agent-llm", "tester")]
    assert "keychain-test-secret" not in json.dumps(config.public_dict())


def test_responses_client_sends_structured_output_and_parses_final_text() -> None:
    captured: dict[str, Any] = {}

    def fake_post(url: str, headers: dict[str, str], body: bytes, timeout: int) -> dict[str, Any]:
        captured.update(
            {"url": url, "headers": headers, "body": json.loads(body), "timeout": timeout}
        )
        return {
            "id": "resp-1",
            "model": "gpt-5.5-2026-08-01",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": '{"ok":true}'}],
                }
            ],
            "usage": {"input_tokens": 7, "output_tokens": 2, "total_tokens": 9},
        }

    client = ResponsesModelClient(model_config(), http_post=fake_post)
    turn = client.create_turn(
        instructions="test",
        input_items=[{"role": "user", "content": [{"type": "input_text", "text": "go"}]}],
        tools=[],
        output_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False},
        schema_name="test_schema",
    )
    assert turn.final_text == '{"ok":true}'
    assert turn.resolved_model == "gpt-5.5-2026-08-01"
    assert captured["url"] == "https://model.invalid/v1/responses"
    assert captured["body"]["store"] is False
    assert captured["body"]["reasoning"] == {"effort": "xhigh"}
    assert captured["body"]["text"]["format"]["type"] == "json_schema"
    assert captured["body"]["parallel_tool_calls"] is True
    assert captured["headers"]["User-Agent"].startswith("openai-python/")


def test_responses_client_real_loopback_http_transport() -> None:
    captured: dict[str, Any] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            length = int(self.headers.get("Content-Length", "0"))
            captured["path"] = self.path
            captured["authorization"] = self.headers.get("Authorization")
            captured["body"] = json.loads(self.rfile.read(length))
            payload = json.dumps(
                {
                    "id": "resp-loopback",
                    "model": "gpt-5.5-loopback",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": '{"ok":true}'}],
                        }
                    ],
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = model_config()
        config = ModelConfig(**{**config.__dict__, "base_url": f"http://127.0.0.1:{server.server_port}/v1"})
        turn = ResponsesModelClient(config).create_turn(
            instructions="test",
            input_items=[{"role": "user", "content": [{"type": "input_text", "text": "go"}]}],
            tools=[],
            output_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False},
            schema_name="loopback_schema",
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
    assert turn.resolved_model == "gpt-5.5-loopback"
    assert captured["path"] == "/v1/responses"
    assert captured["authorization"] == "Bearer test-only"
    assert captured["body"]["text"]["format"]["name"] == "loopback_schema"


def test_responses_client_parses_function_call_items() -> None:
    def fake_post(url: str, headers: dict[str, str], body: bytes, timeout: int) -> dict[str, Any]:
        return {
            "id": "resp-tool",
            "model": "gpt-5.5",
            "status": "completed",
            "output": [
                {"type": "reasoning", "id": "reasoning-1", "summary": []},
                {
                    "type": "function_call",
                    "call_id": "call-search",
                    "name": "search_project",
                    "arguments": '{"query":"RestTemplateBuilder"}',
                },
            ],
            "usage": {"input_tokens": 2, "output_tokens": 2, "total_tokens": 4},
        }

    turn = ResponsesModelClient(model_config(), http_post=fake_post).create_turn(
        instructions="test",
        input_items=[],
        tools=[],
        output_schema={"type": "object"},
        schema_name="tool_schema",
    )
    assert turn.final_text is None
    assert turn.tool_calls == [
        ToolCall(
            call_id="call-search",
            name="search_project",
            arguments={"query": "RestTemplateBuilder"},
        )
    ]
    assert turn.output_items[0]["type"] == "reasoning"


def test_complete_agent_runs_over_real_loopback_responses_transport(tmp_path: Path) -> None:
    project = tmp_path / "project"
    output = tmp_path / "output"
    project.mkdir()
    (project / "client.py").write_text(
        "import requests\nresponse = requests.get(url)\n",
        encoding="utf-8",
    )
    context = {"application": "demo", "namespace": "demo"}
    seed = identify_defects(project, CATALOG, RULES, context)
    candidate_id = seed["candidates"][0]["candidate_id"]
    assessment = {
        "schema_version": "model-defect-assessment.v0.1",
        "analysis_summary": "The deterministic timeout candidate is supported by the cited call.",
        "findings": [
            {
                "defect_ref": "RBD-001",
                "target_component": "client",
                "confidence_score": 0.82,
                "evidence_refs": [
                    {
                        "path": "client.py",
                        "line_start": 2,
                        "line_count": 1,
                        "signal_summary": "Downstream requests call has no local timeout argument.",
                    }
                ],
                "mechanism_reasoning": "A stalled dependency can retain the caller.",
                "matched_conditions": ["requests.get without local timeout"],
                "missing_safeguards": ["No timeout argument in the cited call."],
                "alternative_explanations": ["A configured Session may add a timeout."],
                "validation_requirements": ["Inspect effective Session configuration."],
            }
        ],
        "rejected_seed_candidates": [],
        "coverage_notes": ["Loopback integration fixture."],
    }
    episode_review = {
        "schema_version": "model-episode-review.v0.1",
        "reviews": [
            {
                "candidate_id": candidate_id,
                "episode_title": "Demo client timeout Episode",
                "hypothesis": "A bounded downstream delay can exceed the client SLO without an effective timeout.",
                "critical_path_rationale": "The cited client call is the fixture's downstream operation.",
                "baseline_objective": "Capture a healthy no-fault client baseline.",
                "validation_objective": "Apply one bounded delay and correlate client lifetime and recovery.",
                "additional_evidence_requirements": ["Effective client timeout at runtime."],
                "alternative_experiments": [],
                "risk_notes": ["Keep the target synthetic."],
                "public_leakage_notes": ["Do not label the missing timeout in public input."],
                "readiness_notes": ["Runtime identity remains unresolved."],
            }
        ],
        "overall_notes": ["No live fault was executed."],
    }
    requests_seen: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            request_body = json.loads(self.rfile.read(length))
            requests_seen.append(request_body)
            index = len(requests_seen)
            if index == 1:
                output_items = [
                    {
                        "type": "function_call",
                        "call_id": "call-search",
                        "name": "search_project",
                        "arguments": json.dumps(
                            {
                                "query": "requests.get",
                                "file_globs": ["**/*.py"],
                                "case_sensitive": True,
                                "max_results": 20,
                            }
                        ),
                    }
                ]
            else:
                value = assessment if index == 2 else episode_review
                output_items = [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": json.dumps(value)}
                        ],
                    }
                ]
            payload = json.dumps(
                {
                    "id": f"resp-{index}",
                    "model": "gpt-5.5-loopback",
                    "status": "completed",
                    "output": output_items,
                    "usage": {"input_tokens": 5, "output_tokens": 5, "total_tokens": 10},
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = model_config()
        config = ModelConfig(**{**config.__dict__, "base_url": f"http://127.0.0.1:{server.server_port}/v1"})
        result = ResilienceAnalysisAgent(ResponsesModelClient(config)).run(
            project,
            system_context=context,
            output_dir=output,
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
    assert len(requests_seen) == 3
    assert any(item.get("type") == "function_call_output" for item in requests_seen[1]["input"])
    assert all(item["store"] is False for item in requests_seen)
    assert result.run_manifest["stages"][1]["tool_call_count"] == 1
    assert result.candidates["analysis_mode"] == "hybrid_model_assisted"
    assert len(result.candidates["candidates"]) == 1
    assert set(result.candidates["candidates"][0]["match_rule_ids"]) == {
        "rbd-001-python-requests-without-timeout",
        "model-semantic-analysis",
    }
    assert result.episode_designs["generation_mode"] == "model_assisted_episode_design"
    assert result.episode_designs["episodes"][0]["title"] == "Demo client timeout Episode"


def test_tool_loop_replays_function_output_and_records_trace(tmp_path: Path) -> None:
    (tmp_path / "client.py").write_text("import requests\nrequests.get(url)\n", encoding="utf-8")
    tools = ProjectAnalysisTools(tmp_path, CATALOG)
    model = ScriptedModel(
        [
            tool_turn(
                "search_project",
                {
                    "query": "requests.get",
                    "file_globs": ["**/*.py"],
                    "case_sensitive": True,
                    "max_results": 20,
                },
            ),
            final_turn(empty_assessment()),
        ]
    )
    result = run_agent_loop(
        stage="test",
        model=model,
        tools=tools,
        instructions="test",
        prompt="inspect",
        output_schema=load_document(MODEL_DEFECT_SCHEMA),
        schema_name="model_defect_assessment",
    )
    assert result.trace["tool_call_count"] == 1
    assert result.trace["status"] == "completed"
    second_input = model.requests[1]["input_items"]
    replay = next(item for item in second_input if item.get("type") == "function_call_output")
    assert "client.py" in replay["output"]
    assert result.trace["usage"]["total_tokens"] == 43


def test_analysis_tool_catalog_is_read_only_and_strict(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
    definitions = ProjectAnalysisTools(tmp_path, CATALOG).definitions
    assert {item["name"] for item in definitions} == {
        "list_project_files",
        "search_project",
        "read_project_file",
        "inspect_kubernetes_resources",
        "get_defect_templates",
        "get_system_context",
    }
    assert all(item["type"] == "function" and item["strict"] is True for item in definitions)
    assert all(item["parameters"]["additionalProperties"] is False for item in definitions)
    assert all(not item["name"].startswith(("write", "execute", "delete", "apply")) for item in definitions)


def test_tool_loop_stops_after_max_rounds(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
    tools = ProjectAnalysisTools(tmp_path, CATALOG)
    args = {"glob": "**/*", "limit": 10}
    model = ScriptedModel(
        [tool_turn("list_project_files", args, "c1"), tool_turn("list_project_files", args, "c2")],
        max_rounds=2,
    )
    with pytest.raises(AgentLoopError, match="exceeded 2"):
        run_agent_loop(
            stage="test",
            model=model,
            tools=tools,
            instructions="test",
            prompt="inspect",
            output_schema=load_document(MODEL_DEFECT_SCHEMA),
            schema_name="model_defect_assessment",
        )


def test_tool_loop_retries_only_transport_failure(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
    model = ScriptedModel(
        [ModelTransportError("temporary timeout"), final_turn(empty_assessment())]
    )
    result = run_agent_loop(
        stage="test",
        model=model,
        tools=ProjectAnalysisTools(tmp_path, CATALOG),
        instructions="test",
        prompt="inspect",
        output_schema=load_document(MODEL_DEFECT_SCHEMA),
        schema_name="model_defect_assessment",
    )
    assert result.trace["status"] == "completed"
    assert result.trace["transport_retries"] == [
        {"round": 1, "attempt": 1, "error": "temporary timeout"}
    ]


@pytest.mark.parametrize("status", [429, 500, 503, 504, 524])
def test_http_gateway_transient_status_is_retryable(monkeypatch, status: int) -> None:
    def fail(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "https://model.invalid/v1/responses",
            status,
            "transient",
            hdrs=None,
            fp=io.BytesIO(b"temporary gateway failure"),
        )

    monkeypatch.setattr("urllib.request.urlopen", fail)
    with pytest.raises(ModelTransportError, match=f"HTTP {status}"):
        _default_http_post("https://model.invalid", {}, b"{}", 1)


def test_http_client_error_is_not_retried(monkeypatch) -> None:
    def fail(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "https://model.invalid/v1/responses",
            400,
            "bad request",
            hdrs=None,
            fp=io.BytesIO(b"invalid schema"),
        )

    monkeypatch.setattr("urllib.request.urlopen", fail)
    with pytest.raises(ModelClientError, match="HTTP 400"):
        _default_http_post("https://model.invalid", {}, b"{}", 1)


def test_tool_loop_rejects_invalid_final_schema(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
    with pytest.raises(AgentLoopError, match="schema validation"):
        run_agent_loop(
            stage="test",
            model=ScriptedModel([final_turn({"schema_version": "wrong"})]),
            tools=ProjectAnalysisTools(tmp_path, CATALOG),
            instructions="test",
            prompt="inspect",
            output_schema=load_document(MODEL_DEFECT_SCHEMA),
            schema_name="model_defect_assessment",
        )


def test_read_tool_blocks_path_traversal_and_credential_files(tmp_path: Path) -> None:
    (tmp_path / "safe.txt").write_text("safe\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN=not-real\n", encoding="utf-8")
    tools = ProjectAnalysisTools(tmp_path, CATALOG)
    with pytest.raises(ToolInputError, match="escapes"):
        tools.read_project_file("../outside.txt", 1, 1)
    with pytest.raises(ToolInputError, match="credential-like"):
        tools.read_project_file(".env", 1, 1)


def test_kubernetes_tool_enumerates_resources_in_list_bundle(tmp_path: Path) -> None:
    (tmp_path / "bundle.yaml").write_text(
        """- apiVersion: apps/v1
  kind: Deployment
  metadata: {name: ts-payment-service}
  spec: {replicas: 1}
- apiVersion: apps/v1
  kind: Deployment
  metadata: {name: ts-order-service}
  spec: {replicas: 2}
""",
        encoding="utf-8",
    )
    tools = ProjectAnalysisTools(tmp_path, CATALOG)
    result = tools.inspect_kubernetes_resources("**/*.yaml", ["Deployment"], 20)
    assert [(item["name"], item["replicas"]) for item in result["resources"]] == [
        ("ts-payment-service", 1),
        ("ts-order-service", 2),
    ]


def test_tools_support_explicit_aliased_evidence_root(tmp_path: Path) -> None:
    project = tmp_path / "project"
    extra = tmp_path / "benchmark-config"
    project.mkdir()
    extra.mkdir()
    (project / "client.py").write_text("print('ok')\n", encoding="utf-8")
    (extra / "deployment.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: ts-travel-service}\nspec: {replicas: 1}\n",
        encoding="utf-8",
    )
    tools = ProjectAnalysisTools(
        project,
        CATALOG,
        evidence_roots={"benchmark-config": extra},
    )
    listing = tools.list_project_files("benchmark-config/**/*.yaml", 20)
    assert listing["files"][0]["path"] == "benchmark-config/deployment.yaml"
    read = tools.read_project_file("benchmark-config/deployment.yaml", 1, 4)
    assert read["lines"][2]["text"] == "metadata: {name: ts-travel-service}"
    resources = tools.inspect_kubernetes_resources(
        "benchmark-config/**/*.yaml", ["Deployment"], 20
    )
    assert resources["resources"][0]["name"] == "ts-travel-service"


def test_model_cannot_cite_nonexistent_evidence(tmp_path: Path) -> None:
    (tmp_path / "client.py").write_text("import requests\n", encoding="utf-8")
    seed = identify_defects(tmp_path, CATALOG, RULES)
    assessment = {
        "schema_version": "model-defect-assessment.v0.1",
        "analysis_summary": "hallucinated",
        "findings": [
            {
                "defect_ref": "RBD-001",
                "target_component": "client",
                "confidence_score": 0.9,
                "evidence_refs": [
                    {"path": "missing.py", "line_start": 1, "line_count": 1, "signal_summary": "missing"}
                ],
                "mechanism_reasoning": "invented",
                "matched_conditions": ["invented"],
                "missing_safeguards": [],
                "alternative_explanations": [],
                "validation_requirements": [],
            }
        ],
        "rejected_seed_candidates": [],
        "coverage_notes": [],
    }
    result, _, _ = analyze_defects_with_model(
        seed_document=seed,
        model=ScriptedModel([final_turn(assessment)]),
        tools=ProjectAnalysisTools(tmp_path, CATALOG),
        assessment_schema_path=MODEL_DEFECT_SCHEMA,
    )
    assert result["candidates"] == []
    assert result["model_review"]["invalid_findings"][0]["defect_ref"] == "RBD-001"
    assert "unverifiable" in result["model_review"]["invalid_findings"][0]["reason"]


def test_context_secrets_are_redacted_before_prompt_or_artifact(tmp_path: Path) -> None:
    (tmp_path / "client.py").write_text(
        "import requests\nAPI_KEY=sk-abcdefghijklmnop1234\nrequests.get(url)\n",
        encoding="utf-8",
    )
    context = {
        "application": "demo",
        "api_key": "must-not-appear",
        "nested": {"password": "must-not-appear-either"},
    }
    seed = identify_defects(tmp_path, CATALOG, RULES, context)
    serialized = json.dumps(seed)
    assert "must-not-appear" not in serialized
    assert seed["project"]["context"]["api_key"] == "[REDACTED]"
    tools = ProjectAnalysisTools(tmp_path, CATALOG, context)
    assert tools.system_context["nested"]["password"] == "[REDACTED]"
    read_result = tools.read_project_file("client.py", 1, 3)
    assert "sk-abcdefghijklmnop1234" not in json.dumps(read_result)
    assert "[REDACTED_SECRET]" in json.dumps(read_result)


def test_episode_model_must_review_every_candidate(tmp_path: Path) -> None:
    (tmp_path / "client.py").write_text("import requests\nrequests.get(url)\n", encoding="utf-8")
    candidates = identify_defects(tmp_path, CATALOG, RULES, {"application": "demo"})
    base = design_episodes(
        candidates,
        TEMPLATE_ROOT / "episode-design-templates.v0.1.yaml",
    )
    incomplete_review = {
        "schema_version": "model-episode-review.v0.1",
        "reviews": [],
        "overall_notes": [],
    }
    with pytest.raises(EvidenceValidationError, match="must contain exactly candidate"):
        review_episode_designs_with_model(
            candidate_document=candidates,
            base_designs=base,
            model=ScriptedModel([final_turn(incomplete_review)]),
            tools=ProjectAnalysisTools(tmp_path, CATALOG, {"application": "demo"}),
            review_schema_path=MODEL_EPISODE_SCHEMA,
        )


def test_cli_model_mode_fails_closed_without_credentials(tmp_path: Path) -> None:
    env = os.environ.copy()
    env.pop("RESILIENCE_AGENT_LLM_BASE_URL", None)
    env.pop("RESILIENCE_AGENT_LLM_API_KEY", None)
    output = tmp_path / "out"
    config_path = tmp_path / "model.yaml"
    config_path.write_text(
        """schema_version: resilience-agent-model.v1
provider: openai-compatible
protocol: responses
base_url_env: TEST_AGENT_BASE_URL
base_url_default: https://gateway.example/v1
api_key_env: TEST_AGENT_API_KEY
api_key_keychain_service: ''
model_env: TEST_AGENT_MODEL
model_default: gpt-5.5
reasoning_effort: xhigh
store: false
""",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "resilience_agent",
            "run",
            "--project",
            str(PACKAGE_ROOT / "examples/minimal"),
            "--output-dir",
            str(output),
            "--model-config",
            str(config_path),
        ],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert proc.returncode == 2
    assert "TEST_AGENT_API_KEY" in proc.stderr
    assert not (output / "candidate-defects.json").exists()


def test_model_contract_failure_writes_failed_run_manifest(tmp_path: Path) -> None:
    project = tmp_path / "project"
    output = tmp_path / "output"
    project.mkdir()
    (project / "client.py").write_text("import requests\nrequests.get(url)\n", encoding="utf-8")
    with pytest.raises(AgentLoopError):
        ResilienceAnalysisAgent(
            ScriptedModel([final_turn({"schema_version": "invalid"})])
        ).run(project, output_dir=output)
    manifest = load_document(output / "agent-run.json")
    assert manifest["status"] == "failed"
    assert manifest["reasoning_mode"] == "model"
    assert manifest["artifacts"]["candidate_defects"] is None
    assert not (output / "candidate-defects.json").exists()


def test_episode_stage_failure_preserves_validated_candidate_artifacts(tmp_path: Path) -> None:
    project = tmp_path / "project"
    output = tmp_path / "output"
    project.mkdir()
    (project / "client.py").write_text("import requests\nrequests.get(url)\n", encoding="utf-8")
    model = ScriptedModel(
        [final_turn(empty_assessment()), final_turn({"schema_version": "invalid"})]
    )
    with pytest.raises(AgentLoopError):
        ResilienceAnalysisAgent(model).run(project, output_dir=output)
    manifest = load_document(output / "agent-run.json")
    assert manifest["status"] == "failed"
    assert manifest["artifacts"]["candidate_defects"] == "candidate-defects.json"
    assert manifest["artifacts"]["model_defect_assessment"] == "model-defect-assessment.json"
    assert (output / "candidate-defects.json").is_file()
    assert (output / "model-defect-assessment.json").is_file()
    assert not (output / "episode-designs.json").exists()


def test_train_ticket_model_agent_rejects_bad_seed_and_finds_timeout_candidate(tmp_path: Path) -> None:
    assert TRAIN_TICKET.is_dir()
    context = load_document(TRAIN_CONTEXT)
    seed = identify_defects(TRAIN_TICKET, CATALOG, RULES, context)
    seed_ids = [item["candidate_id"] for item in seed["candidates"]]
    evidence_refs = [
        {
            "path": "ts-travel-service/src/main/java/travel/TravelApplication.java",
            "line_start": 32,
            "line_count": 2,
            "signal_summary": "RestTemplateBuilder builds the client without local timeout configuration.",
        },
        {
            "path": "ts-travel-service/src/main/java/travel/service/TravelServiceImpl.java",
            "line_start": 345,
            "line_count": 7,
            "signal_summary": "Search-path code synchronously calls ts-basic-service through RestTemplate.exchange.",
        },
    ]
    assessment = {
        "schema_version": "model-defect-assessment.v0.1",
        "analysis_summary": "The seed availability candidate is a fault-injection helper; the search path has a stronger timeout candidate.",
        "findings": [
            {
                "defect_ref": "RBD-001",
                "target_component": "ts-travel-service",
                "confidence_score": 0.88,
                "evidence_refs": evidence_refs,
                "mechanism_reasoning": "A synchronous search dependency can retain request capacity during downstream latency.",
                "matched_conditions": ["RestTemplateBuilder.build without visible timeout", "synchronous critical-path exchange"],
                "missing_safeguards": ["No explicit connect/read timeout was found in the cited construction path."],
                "alternative_explanations": ["Runtime external configuration may apply a timeout."],
                "validation_requirements": ["Inspect effective runtime client timeout configuration."],
            }
        ],
        "rejected_seed_candidates": [
            {"candidate_id": candidate_id, "reason": "Target is an unproven fault-injection helper, not a registered critical service."}
            for candidate_id in seed_ids
        ],
        "coverage_notes": ["Messaging and consistency families need additional targeted review."],
    }
    candidate_id = stable_id(
        "CAND-RBD-001",
        [
            "RBD-001",
            "ts-travel-service",
            "model-semantic-analysis",
            "ts-travel-service/src/main/java/travel/TravelApplication.java:32",
            "ts-travel-service/src/main/java/travel/service/TravelServiceImpl.java:345",
        ],
    )
    episode_review = {
        "schema_version": "model-episode-review.v0.1",
        "reviews": [
            {
                "candidate_id": candidate_id,
                "episode_title": "Train-Ticket search-path outbound timeout Episode",
                "hypothesis": "A bounded delay on ts-basic-service causes ts-travel-service search latency and in-flight work to exceed the registered entry SLO because the client has no effective timeout.",
                "critical_path_rationale": "The cited travel search method synchronously calls ts-basic-service.",
                "baseline_objective": "Establish two attributable healthy search workload windows.",
                "validation_objective": "Delay one ts-basic-service target and correlate client lifetime, search SLO, and recovery.",
                "additional_evidence_requirements": ["Effective RestTemplate timeout configuration at runtime."],
                "alternative_experiments": [
                    {
                        "trigger_class": "latency",
                        "target_component": "ts-basic-service",
                        "rationale": "This is the directly cited downstream dependency.",
                        "evidence_refs": [
                            {
                                "path": "ts-travel-service/src/main/java/travel/service/TravelServiceImpl.java",
                                "line_start": 345,
                                "line_count": 7,
                            }
                        ],
                    }
                ],
                "risk_notes": ["Do not delay every ts-basic-service replica."],
                "public_leakage_notes": ["Do not label ts-basic-service as the answer in public material."],
                "readiness_notes": ["Runtime target UID and cleanup handle remain mandatory."],
            }
        ],
        "overall_notes": ["Static candidate only; no runtime experiment was performed."],
    }
    model = ScriptedModel([final_turn(assessment, "defect-final"), final_turn(episode_review, "episode-final")])
    result = ResilienceAnalysisAgent(model).run(
        TRAIN_TICKET,
        system_context=context,
        output_dir=tmp_path,
    )
    assert result.candidates["analysis_mode"] == "hybrid_model_assisted"
    assert [item["defect_ref"] for item in result.candidates["candidates"]] == ["RBD-001"]
    assert result.candidates["candidates"][0]["target"]["component"] == "ts-travel-service"
    assert result.candidates["model_review"]["rejected_seed_candidates"]
    episode = result.episode_designs["episodes"][0]
    assert result.episode_designs["generation_mode"] == "model_assisted_episode_design"
    assert episode["title"] == "Train-Ticket search-path outbound timeout Episode"
    assert episode["model_reasoning"]["alternative_experiments"][0]["target_component"] == "ts-basic-service"
    assert episode["readiness"]["ready_for_lock"] is False
    assert result.run_manifest["model"]["model"] == "gpt-5.5"
    assert "test-only" not in json.dumps(result.run_manifest)
    assert (tmp_path / "agent-run.json").is_file()
    assert (tmp_path / "model-defect-assessment.json").is_file()
    assert (tmp_path / "model-episode-review.json").is_file()
