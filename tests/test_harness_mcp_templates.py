from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from scripts import benchmark_prepare


CODEX_TEMPLATE = Path("harness/codex/config.toml.template")
CLAUDE_TEMPLATE = Path("harness/claude-code/mcp.json.template")
EXPECTED_ENDPOINTS = {
    "k8s_ro": "RESBENCH_K8S_MCP_URL",
    "telemetry_ro": "RESBENCH_TELEMETRY_MCP_URL",
    "source_ro": "RESBENCH_SOURCE_MCP_URL",
    "chaos_control": "RESBENCH_CHAOS_CONTROL_MCP_URL",
}


def test_codex_template_uses_official_http_mcp_server_shape():
    parsed = tomllib.loads(CODEX_TEMPLATE.read_text(encoding="utf-8"))

    assert set(parsed["mcp_servers"]) == set(EXPECTED_ENDPOINTS)
    for name, env_name in EXPECTED_ENDPOINTS.items():
        server = parsed["mcp_servers"][name]
        assert server == {
            "url": f"__{env_name}__",
            "bearer_token_env_var": "RESBENCH_MCP_TOKEN",
        }


def test_codex_template_is_safe_after_runner_url_rendering():
    rendered = CODEX_TEMPLATE.read_text(encoding="utf-8")
    for env_name in EXPECTED_ENDPOINTS.values():
        rendered = rendered.replace(f"__{env_name}__", f"https://mcp.example.invalid/{env_name.lower()}")

    parsed = tomllib.loads(rendered)

    for name, env_name in EXPECTED_ENDPOINTS.items():
        assert parsed["mcp_servers"][name]["url"] == f"https://mcp.example.invalid/{env_name.lower()}"
        assert parsed["mcp_servers"][name]["bearer_token_env_var"] == "RESBENCH_MCP_TOKEN"


def test_claude_code_template_uses_http_mcp_with_environment_expansion():
    parsed = json.loads(CLAUDE_TEMPLATE.read_text(encoding="utf-8"))

    assert set(parsed["mcpServers"]) == set(EXPECTED_ENDPOINTS)
    for name, env_name in EXPECTED_ENDPOINTS.items():
        server = parsed["mcpServers"][name]
        assert server["type"] == "http"
        assert server["url"] == f"${{{env_name}}}"
        assert server["headers"] == {"Authorization": "Bearer ${RESBENCH_MCP_TOKEN}"}


def test_claude_code_template_is_valid_after_safe_environment_substitution():
    rendered = CLAUDE_TEMPLATE.read_text(encoding="utf-8")
    for env_name in EXPECTED_ENDPOINTS.values():
        rendered = rendered.replace(f"${{{env_name}}}", f"https://mcp.example.invalid/{env_name.lower()}")
    rendered = rendered.replace("${RESBENCH_MCP_TOKEN}", "example-runtime-token")

    parsed = json.loads(rendered)

    for name in EXPECTED_ENDPOINTS:
        assert parsed["mcpServers"][name]["url"].startswith("https://mcp.example.invalid/")
        assert parsed["mcpServers"][name]["headers"]["Authorization"] == "Bearer example-runtime-token"


def test_templates_and_readmes_contain_only_secret_references():
    paths = [
        CODEX_TEMPLATE,
        CLAUDE_TEMPLATE,
        Path("harness/codex/README.md"),
        Path("harness/claude-code/README.md"),
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in paths)

    assert "RESBENCH_MCP_TOKEN" in combined
    assert not benchmark_prepare.contains_secret_material(combined)
    assert not re.search(r"sk-[A-Za-z0-9_-]{20,}", combined)
    assert "Bearer ${RESBENCH_MCP_TOKEN}" in combined
    assert "example-runtime-token" not in combined


def test_readmes_document_trial_isolation_and_launch_boundaries():
    codex = Path("harness/codex/README.md").read_text(encoding="utf-8")
    claude = Path("harness/claude-code/README.md").read_text(encoding="utf-8")

    assert "fresh" in codex and "`CODEX_HOME`" in codex
    assert "read-only, ephemeral mode" in codex
    assert "TOML `url` values are plain strings" in codex
    assert "fresh" in claude and "`CLAUDE_CONFIG_DIR`" in claude
    assert "claude --print --strict-mcp-config" in claude
    assert "Run qualification should verify" in codex
    assert "Run qualification should verify" in claude
