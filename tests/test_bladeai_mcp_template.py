from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from scripts import benchmark_prepare


BLADEAI_TEMPLATE = Path("harness/bladeai/mcp.json.template")
BLADEAI_README = Path("harness/bladeai/README.md")
BLADEAI_ENV = Path("harness/bladeai/env.example")
BLADEAI_QUALIFICATION = Path("harness/bladeai/qualification.yaml")
HARNESSES = Path("harness/harnesses.yaml")

EXPECTED_READ_ONLY = {
    "k8s_ro": "RESBENCH_BLADEAI_K8S_MCP_SSE_URL",
    "telemetry_ro": "RESBENCH_BLADEAI_TELEMETRY_MCP_SSE_URL",
    "source_ro": "RESBENCH_BLADEAI_SOURCE_MCP_SSE_URL",
}
ALLOWED_ATTACH_TO = {"clarification", "phase1", "phase2", "verifier"}


def _load_template() -> dict:
    return json.loads(BLADEAI_TEMPLATE.read_text(encoding="utf-8"))


def test_bladeai_template_uses_v062_mcp_shape_and_sse_http_transport():
    parsed = _load_template()

    assert set(parsed) == {"mcpServers"}
    assert set(parsed["mcpServers"]) == {*EXPECTED_READ_ONLY, "chaos_control"}

    for name, env_name in EXPECTED_READ_ONLY.items():
        server = parsed["mcpServers"][name]
        assert server["transport"] == "http"
        assert server["url"] == f"${{{env_name}}}"
        assert server["headers"] == {"Authorization": "Bearer ${RESBENCH_MCP_TOKEN}"}
        assert server["attach_to"] == ["verifier"]
        assert server["timeout_seconds"] == 30
        assert set(server["attach_to"]) <= ALLOWED_ATTACH_TO
        assert "type" not in server


def test_bladeai_template_keeps_chaos_control_disabled():
    chaos = _load_template()["mcpServers"]["chaos_control"]

    assert chaos["enabled"] is False
    assert chaos["attach_to"] == []
    assert chaos["transport"] == "http"
    assert chaos["headers"] == {"Authorization": "Bearer ${RESBENCH_MCP_TOKEN}"}


def test_bladeai_template_renders_without_secret_material():
    rendered = BLADEAI_TEMPLATE.read_text(encoding="utf-8")
    loopback_urls = {
        "RESBENCH_BLADEAI_K8S_MCP_SSE_URL": "http://127.0.0.1:18181/sse",
        "RESBENCH_BLADEAI_TELEMETRY_MCP_SSE_URL": "http://127.0.0.1:18182/sse",
        "RESBENCH_BLADEAI_SOURCE_MCP_SSE_URL": "http://127.0.0.1:18183/sse",
        "RESBENCH_BLADEAI_CHAOS_CONTROL_MCP_SSE_URL": "http://127.0.0.1:18184/sse",
    }
    for env_name, url in loopback_urls.items():
        rendered = rendered.replace(f"${{{env_name}}}", url)
    rendered = rendered.replace("${RESBENCH_MCP_TOKEN}", "x")

    parsed = json.loads(rendered)
    for name in EXPECTED_READ_ONLY:
        assert parsed["mcpServers"][name]["url"].startswith("http://127.0.0.1:1818")
        assert parsed["mcpServers"][name]["headers"]["Authorization"] == "Bearer x"


def test_bladeai_docs_and_examples_do_not_store_real_secrets():
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in [BLADEAI_TEMPLATE, BLADEAI_README, BLADEAI_ENV, BLADEAI_QUALIFICATION]
    )

    assert "RESBENCH_MCP_TOKEN" in combined
    assert not benchmark_prepare.contains_secret_material(combined)
    assert not re.search(r"sk-[A-Za-z0-9_-]{20,}", combined)
    assert "Bearer ${RESBENCH_MCP_TOKEN}" in combined


def test_bladeai_qualification_records_real_version_boundary():
    qualification = yaml.safe_load(BLADEAI_QUALIFICATION.read_text(encoding="utf-8"))
    contract = qualification["bladeai"]["source_contract"]
    template = qualification["bladeai"]["benchmark_template"]

    assert qualification["bladeai"]["version"] == "0.6.2"
    assert contract["mcp_role"] == "client"
    assert contract["supported_transports"] == ["stdio", "http_sse"]
    assert "streamable_http_client" in contract["unsupported_transports"]
    assert contract["enabled_flag"] == "BLADE_AI_MCP_ENABLED"
    assert contract["config_path_flag"] == "BLADE_AI_MCP_CONFIG_PATH"
    assert contract["config_path_status"] == "declared_but_loader_uses_default_home_path"
    assert contract["runtime_transport_status"] == "native_sse_runtime_live_qualified_read_only"
    assert contract["host_native_sse_listeners"] == {
        "k8s_ro": "127.0.0.1:18181",
        "telemetry_ro": "127.0.0.1:18182",
        "source_ro": "127.0.0.1:18183",
    }
    assert set(contract["allowed_attach_to"]) == ALLOWED_ATTACH_TO
    assert set(template["read_only_servers"]) == set(EXPECTED_READ_ONLY)
    assert template["controlled_write_servers"]["chaos_control"]["enabled"] is False
    assert qualification["bladeai"]["live_evidence"]["connectedTools"] == {
        "k8s_ro": 5,
        "telemetry_ro": 10,
        "source_ro": 5,
    }
    assert qualification["bladeai"]["live_evidence"]["chaosControlConnected"] is False


def test_harness_registry_points_bladeai_to_template_and_boundary():
    registry = yaml.safe_load(HARNESSES.read_text(encoding="utf-8"))
    bladeai = registry["harnesses"]["bladeai"]

    assert bladeai["mcp"]["template"] == "bladeai/mcp.json.template"
    assert bladeai["mcp"]["qualification"] == "bladeai/qualification.yaml"
    assert bladeai["mcp"]["transport_status"] == "native_sse_runtime_live_qualified_read_only"
    assert bladeai["mcp"]["host_native_sse_listeners"] == {
        "k8s_ro": "127.0.0.1:18181",
        "telemetry_ro": "127.0.0.1:18182",
        "source_ro": "127.0.0.1:18183",
    }
    assert bladeai["mcp"]["chaos_control"] == "disabled_in_bladeai_external_controller_only"
