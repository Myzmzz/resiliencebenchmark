from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import yaml

from scripts import benchmark_prepare
from scripts.run_harness_trial import render_claude_config, render_codex_config, render_dsh_contract


CODEX_TEMPLATE = Path("harness/codex/config.toml.template")
CLAUDE_TEMPLATE = Path("harness/claude-code/mcp.json.template")
DSH_SETTINGS_TEMPLATE = Path("harness/deepseek-harness/settings.yaml.template")
DSH_MCP_PATCH = Path("harness/deepseek-harness/mcp.cordis.patch.yml")
EXPECTED_ENDPOINTS = {
    "k8s_ro": "RESBENCH_K8S_MCP_URL",
    "telemetry_ro": "RESBENCH_TELEMETRY_MCP_URL",
    "source_ro": "RESBENCH_SOURCE_MCP_URL",
    "chaos_control": "RESBENCH_CHAOS_CONTROL_MCP_URL",
    "harness_channel": "RESBENCH_HARNESS_CHANNEL_MCP_URL",
}


def token_env(server: str) -> str:
    return "RESBENCH_HARNESS_CHANNEL_TOKEN" if server == "harness_channel" else "RESBENCH_MCP_TOKEN"


def test_codex_template_uses_official_http_mcp_server_shape():
    parsed = tomllib.loads(CODEX_TEMPLATE.read_text(encoding="utf-8"))

    assert parsed["features"]
    assert all(value is False for value in parsed["features"].values())
    assert {
        "apps",
        "browser_use",
        "computer_use",
        "goals",
        "image_generation",
        "multi_agent",
        "shell_tool",
        "unified_exec",
        "shell_snapshot",
        "workspace_dependencies",
    } <= set(parsed["features"])
    assert set(parsed["mcp_servers"]) == set(EXPECTED_ENDPOINTS)
    assert parsed["approval_policy"] == "never"
    for name, env_name in EXPECTED_ENDPOINTS.items():
        server = parsed["mcp_servers"][name]
        assert server["url"] == f"__{env_name}__"
        assert server["bearer_token_env_var"] == token_env(name)
        if name == "chaos_control":
            assert server["tools"] == {
                "chaos_create_experiment": {"approval_mode": "approve"},
                "chaos_destroy_experiment": {"approval_mode": "approve"},
            }
        else:
            assert "tools" not in server


def test_codex_template_is_safe_after_runner_url_rendering():
    rendered = CODEX_TEMPLATE.read_text(encoding="utf-8")
    for env_name in EXPECTED_ENDPOINTS.values():
        rendered = rendered.replace(f"__{env_name}__", f"https://mcp.example.invalid/{env_name.lower()}")

    parsed = tomllib.loads(rendered)

    for name, env_name in EXPECTED_ENDPOINTS.items():
        assert parsed["mcp_servers"][name]["url"] == f"https://mcp.example.invalid/{env_name.lower()}"
        assert parsed["mcp_servers"][name]["bearer_token_env_var"] == token_env(name)


def test_claude_code_template_uses_http_mcp_with_environment_expansion():
    parsed = json.loads(CLAUDE_TEMPLATE.read_text(encoding="utf-8"))

    assert set(parsed["mcpServers"]) == set(EXPECTED_ENDPOINTS)
    for name, env_name in EXPECTED_ENDPOINTS.items():
        server = parsed["mcpServers"][name]
        assert server["type"] == "http"
        assert server["url"] == f"${{{env_name}}}"
        assert server["headers"] == {"Authorization": f"Bearer ${{{token_env(name)}}}"}


def test_claude_code_template_is_valid_after_safe_environment_substitution():
    rendered = CLAUDE_TEMPLATE.read_text(encoding="utf-8")
    for env_name in EXPECTED_ENDPOINTS.values():
        rendered = rendered.replace(f"${{{env_name}}}", f"https://mcp.example.invalid/{env_name.lower()}")
    rendered = rendered.replace("${RESBENCH_MCP_TOKEN}", "example-runtime-token")
    rendered = rendered.replace("${RESBENCH_HARNESS_CHANNEL_TOKEN}", "example-runtime-token")

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
    assert "removing non-MCP tools" in codex
    assert "TOML `url` values are plain strings" in codex
    assert "fresh" in claude and "`CLAUDE_CONFIG_DIR`" in claude
    assert "claude --print --strict-mcp-config" in claude
    assert "Run qualification should verify" in codex
    assert "Run qualification should verify" in claude


def test_claude_headless_adapter_explicitly_allows_only_mcp_servers():
    harnesses = yaml.safe_load(Path("harness/harnesses.yaml").read_text(encoding="utf-8"))
    args = harnesses["harnesses"]["claude-code"]["entrypoint"]["args"]

    from stage2_service.mcp_supervisor import McpSupervisor

    assert args[args.index("--tools") + 1] == ""
    allowed = set(args[args.index("--allowedTools") + 1].split(","))
    # Every MCP server the supervisor can mount must be allowed, or Claude's own
    # permission check refuses its tools (2026-09-10: the coroot_ro and
    # code_sandbox calls were refused and the Coroot check failed). A server
    # that is not in the Trial's --strict-mcp-config file stays unavailable.
    assert allowed == {f"mcp__{name}" for name in McpSupervisor.HTTP_PORTS}


def test_deepseek_headless_templates_select_model_and_disable_builtin_tools():
    settings = yaml.safe_load(DSH_SETTINGS_TEMPLATE.read_text(encoding="utf-8"))
    patch = yaml.load(DSH_MCP_PATCH.read_text(encoding="utf-8"), Loader=benchmark_prepare.TaggedSafeLoader)

    assert settings["agent-default-model"] == {
        "provider": "benchmark-gateway",
        "model": "__RESBENCH_MODEL_ALIAS__",
    }
    provider_models = {
        value["id"]
        for value in settings["llm-pi-ai"]["providers"]["benchmark-gateway"][
            "models"
        ]
    }
    # The template carries only the alias placeholder; run_harness_trial
    # substitutes the selected model (covered by test_run_harness_trial).
    assert provider_models == {"__RESBENCH_MODEL_ALIAS__"}
    disabled = {item["id"] for item in patch if isinstance(item, dict) and item.get("disabled") is True}
    assert {"code-runtime", "tool-bash", "tool-pwsh", "tool-fs", "tool-web", "tool-subagent"} <= disabled
    inserted = next(item["insert"] for item in patch if isinstance(item, dict) and "insert" in item)
    assert len(inserted) == len(EXPECTED_ENDPOINTS)
    assert all(item["config"]["transport"] == "streamable-http" for item in inserted)
    assert all(item["config"]["failOnStartupError"] is True for item in inserted)


def test_optional_substitution_servers_are_rendered_only_when_urls_exist(tmp_path: Path):
    base_env = {
        "RESBENCH_LLM_BASE_URL": "https://gateway.example.invalid/v1",
        **{env: f"https://mcp.example.invalid/{name}" for name, env in EXPECTED_ENDPOINTS.items()},
    }
    codex = render_codex_config(Path.cwd(), tmp_path / "codex-base", base_env)
    assert set(tomllib.loads(codex.read_text())["mcp_servers"]) == set(EXPECTED_ENDPOINTS)
    extended = {**base_env, "RESBENCH_COROOT_MCP_URL": "http://127.0.0.1:18086/mcp",
                "RESBENCH_CHAOS_MESH_CONTROL_MCP_URL": "http://127.0.0.1:18087/mcp",
                "RESBENCH_CODE_SANDBOX_MCP_URL": "http://127.0.0.1:18088/mcp"}
    rendered = render_codex_config(Path.cwd(), tmp_path / "codex-extended", extended)
    assert {"coroot_ro", "chaos_mesh_control", "code_sandbox"} <= set(tomllib.loads(rendered.read_text())["mcp_servers"])
    claude = render_claude_config(Path.cwd(), tmp_path / "claude", extended)
    assert {"coroot_ro", "chaos_mesh_control", "code_sandbox"} <= set(json.loads(claude.read_text())["mcpServers"])
    render_dsh_contract(Path.cwd(), tmp_path / "dsh", extended, "gpt-5.6-sol")
    patch = (tmp_path / "dsh" / "cordis.patch.yml").read_text()
    assert "serverName: coroot_ro" in patch and "serverName: chaos_mesh_control" in patch and "serverName: code_sandbox" in patch
