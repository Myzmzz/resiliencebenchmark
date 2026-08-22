import json
from pathlib import Path

import pytest
import yaml

from scripts import run_harness_trial as trial


REPO_ROOT = Path(__file__).resolve().parents[1]


def runtime_env():
    return {
        "RESBENCH_LLM_BASE_URL": "https://gateway.example/v1",
        "RESBENCH_LLM_API_KEY": "sk-test-secret-value-that-must-not-leak",
        "RESBENCH_K8S_MCP_URL": "http://127.0.0.1:18181/mcp",
        "RESBENCH_TELEMETRY_MCP_URL": "http://127.0.0.1:18182/mcp",
        "RESBENCH_SOURCE_MCP_URL": "http://127.0.0.1:18183/mcp",
        "RESBENCH_CHAOS_CONTROL_MCP_URL": "http://127.0.0.1:18184/mcp",
        "RESBENCH_MCP_TOKEN": "mcp-token-that-must-not-leak-000000",
        "KUBECONFIG": "/tmp/should-not-pass",
        "HARBOR_REGISTRY": "registry.example",
        "RESBENCH_SSH_BOOTSTRAP_IDENTITY": "/tmp/key",
    }


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def artifact_text(path: Path) -> str:
    return "\n".join(item.read_text(encoding="utf-8") for item in path.rglob("*") if item.is_file())


def artifact_dir(report, root: Path) -> Path:
    return root / report["artifactRef"]


def artifact_ref_path(report, root: Path, key: str) -> Path:
    return root / report[key]


def valid_agent_result(defect: str = "missing outbound timeout"):
    return {
        "status": "completed",
        "suspected_defect": defect,
        "evidence": [
            {
                "source": "k8s_ro",
                "summary": "frontend was ready before the experiment",
                "observed_at": "2026-08-21T00:00:00Z",
                "artifact_ref": "k8s://pods/frontend",
            }
        ],
        "actions_taken": ["checked public episode scope"],
        "recovery_check": "no mutation was performed",
        "remaining_risk": "public smoke only",
    }


def test_runner_tool_allowlist_matches_declared_mcp_contract():
    contract = yaml.safe_load((REPO_ROOT / "harness/mcp-tools.yaml").read_text(encoding="utf-8"))

    assert set(trial.ALLOWED_MCP_TOOLS) == set(contract["tools"])
    for server, tools in trial.ALLOWED_MCP_TOOLS.items():
        assert tools == set(contract["tools"][server]["allowed_operations"])


def test_codex_registry_uses_literal_stdin_marker():
    harnesses = yaml.safe_load((REPO_ROOT / "harness/harnesses.yaml").read_text(encoding="utf-8"))
    args = harnesses["harnesses"]["codex"]["entrypoint"]["args"]

    assert args[-1] == "-"
    assert all(isinstance(item, str) for item in args)
    assert "--strict-config" not in args
    assert "--skip-git-repo-check" in args


def test_dry_run_records_template_hashes_without_resolved_urls_or_homes(tmp_path):
    report = trial.run_trial(
        REPO_ROOT,
        "codex",
        "gpt-5.6",
        execute=False,
        artifact_root=tmp_path,
        parent_env=runtime_env(),
        trial_id="dry-codex",
    )

    output_dir = artifact_dir(report, tmp_path)
    planned = load_json(output_dir / "planned.json")
    trace = load_json(artifact_ref_path(report, tmp_path, "runTraceRef"))
    encoded = artifact_text(output_dir)

    assert report["dryRun"] is True
    assert trace["final_output"]["status"] == "aborted_by_controller"
    assert "mcp-token-that-must-not-leak-000000" not in encoded
    assert "http://127.0.0.1:18181/mcp" not in encoded
    assert str(REPO_ROOT) not in encoded
    assert str(tmp_path) not in encoded
    assert str(tmp_path) not in json.dumps(report)
    assert report["artifactRef"] == "dry-codex"
    assert planned["templates"]["codexConfigTemplateSha256"]
    assert planned["homes"] == {"isolated": True, "retainedInArtifacts": False}
    assert not any(path.is_dir() for path in output_dir.iterdir())
    assert "KUBECONFIG" not in planned["envKeys"]
    assert "HARBOR_REGISTRY" not in planned["envKeys"]
    assert "RESBENCH_SSH_BOOTSTRAP_IDENTITY" not in planned["envKeys"]
    assert planned["runtimeEnv"]["RESBENCH_MCP_TOKEN"] == {"present": True}


def test_codex_config_renders_custom_responses_provider_without_api_key(tmp_path):
    config_path = trial.render_codex_config(REPO_ROOT, tmp_path / "codex-home", runtime_env())
    rendered = config_path.read_text(encoding="utf-8")

    assert 'model_provider = "resbench_gateway"' in rendered
    assert 'base_url = "https://gateway.example/v1"' in rendered
    assert 'env_key = "OPENAI_API_KEY"' in rendered
    assert 'wire_api = "responses"' in rendered
    assert "supports_websockets = false" in rendered
    assert "sk-test-secret-value-that-must-not-leak" not in rendered
    assert "mcp-token-that-must-not-leak-000000" not in rendered


def test_dry_run_for_claude_does_not_persist_rendered_mcp_config(tmp_path):
    report = trial.run_trial(
        REPO_ROOT,
        "claude-code",
        "claude-opus-5",
        execute=False,
        artifact_root=tmp_path,
        parent_env=runtime_env(),
        trial_id="dry-claude",
    )

    output_dir = artifact_dir(report, tmp_path)
    planned = load_json(output_dir / "planned.json")
    text = artifact_text(output_dir)

    assert planned["templates"]["claudeMcpTemplateSha256"]
    assert "${RESBENCH_K8S_MCP_URL}" not in text
    assert "mcp-token-that-must-not-leak-000000" not in text


def test_execute_codex_uses_fixed_argv_stdin_and_allowlisted_env(tmp_path):
    calls = []
    final = valid_agent_result()

    def fake_runner(argv, stdin, env, timeout_seconds):
        calls.append({"argv": argv, "stdin": stdin, "env": dict(env), "timeout": timeout_seconds})
        stdout = (
            json.dumps({"type": "tool_call", "tool": "k8s_ro.k8s_cluster_inventory", "args": {"namespace": "otel-demo"}})
            + "\n"
            + json.dumps(final)
            + "\n"
        ).encode("utf-8")
        return trial.CommandResult(returncode=0, stdout=stdout, stderr=b"")

    report = trial.run_trial(
        REPO_ROOT,
        "codex",
        "gpt-5.6",
        execute=True,
        artifact_root=tmp_path,
        parent_env=runtime_env(),
        runner=fake_runner,
        trial_id="exec-codex",
        timeout_seconds=12,
    )

    assert report["status"] == "completed"
    assert calls and calls[0]["argv"][:3] == ["codex", "exec", "--model"]
    assert "--strict-config" not in calls[0]["argv"]
    assert "--output-last-message" in calls[0]["argv"]
    assert isinstance(calls[0]["argv"], list)
    assert b"Public episode contract follows" in calls[0]["stdin"]
    assert b"You may use these Agent-visible MCP capabilities" in calls[0]["stdin"]
    assert calls[0]["timeout"] == 12
    assert calls[0]["env"]["OPENAI_BASE_URL"] == runtime_env()["RESBENCH_LLM_BASE_URL"]
    assert calls[0]["env"]["OPENAI_API_KEY"] == runtime_env()["RESBENCH_LLM_API_KEY"]
    assert "RESBENCH_LLM_API_KEY" not in calls[0]["env"]
    assert "RESBENCH_LLM_BASE_URL" not in calls[0]["env"]
    assert "RESBENCH_K8S_MCP_URL" not in calls[0]["env"]
    assert calls[0]["env"]["PATH"] == trial.SAFE_PATH
    assert "KUBECONFIG" not in calls[0]["env"]
    assert "HARBOR_REGISTRY" not in calls[0]["env"]
    trace = load_json(artifact_ref_path(report, tmp_path, "runTraceRef"))
    assert trace["events"][1]["kind"] == "agent_message"
    assert any(event["kind"] == "tool_call" and event["tool"] == "k8s_ro.k8s_cluster_inventory" for event in trace["events"])
    assert load_json(artifact_ref_path(report, tmp_path, "agentResultRef"))["suspected_defect"] == "missing outbound timeout"
    assert not any(path.is_dir() for path in artifact_dir(report, tmp_path).iterdir())


def test_execute_codex_prefers_output_last_message_file(tmp_path):
    final = valid_agent_result("from codex last message")
    calls = []

    def fake_runner(argv, stdin, env, timeout_seconds):
        calls.append(argv)
        output_path = Path(argv[argv.index("--output-last-message") + 1])
        output_path.write_text(json.dumps({"structured_output": final}), encoding="utf-8")
        return trial.CommandResult(returncode=0, stdout=b'{"status":"completed"}\n', stderr=b"")

    report = trial.run_trial(
        REPO_ROOT,
        "codex",
        "gpt-5.6",
        execute=True,
        artifact_root=tmp_path,
        parent_env=runtime_env(),
        runner=fake_runner,
        trial_id="exec-codex-last-message",
    )

    assert report["status"] == "completed"
    assert load_json(artifact_ref_path(report, tmp_path, "agentResultRef"))["suspected_defect"] == "from codex last message"


def test_codex_mcp_jsonl_shape_records_call_and_result(tmp_path):
    final = valid_agent_result("codex mcp shape")

    def fake_runner(argv, stdin, env, timeout_seconds):
        events = [
            {"type": "mcp_tool_call", "server": "k8s_ro", "tool": "k8s_cluster_inventory", "status": "in_progress"},
            {
                "type": "mcp_tool_call",
                "server": "k8s_ro",
                "tool": "k8s_cluster_inventory",
                "status": "completed",
                "result": {"content": []},
            },
            final,
        ]
        return trial.CommandResult(
            returncode=0,
            stdout=("\n".join(json.dumps(item) for item in events) + "\n").encode(),
            stderr=b"",
        )

    report = trial.run_trial(
        REPO_ROOT,
        "codex",
        "gpt-5.6",
        execute=True,
        artifact_root=tmp_path,
        parent_env=runtime_env(),
        runner=fake_runner,
        trial_id="codex-mcp-shape",
    )
    trace = load_json(artifact_ref_path(report, tmp_path, "runTraceRef"))

    assert report["status"] == "completed"
    assert any(event["kind"] == "tool_call" and event.get("tool") == "k8s_ro.k8s_cluster_inventory" for event in trace["events"])
    assert any(event["kind"] == "tool_result" and event.get("tool") == "k8s_ro.k8s_cluster_inventory" for event in trace["events"])


@pytest.mark.parametrize(
    "tool_event",
    [
        {"type": "command_execution", "command": "cat /etc/passwd", "status": "completed"},
        {"type": "tool_call", "tool": "shell_tool", "status": "completed"},
        {"type": "function_call", "name": "exec_command", "status": "completed"},
        {"type": "tool_use", "name": "Bash", "status": "completed"},
        {"type": "mcp_tool_call", "server": "unknown", "tool": "k8s_cluster_inventory"},
    ],
)
def test_non_mcp_tool_event_fails_trial_even_with_valid_final_result(tmp_path, tool_event):
    stdout = (
        json.dumps(tool_event)
        + "\n"
        + json.dumps(valid_agent_result())
    ).encode()

    report = trial.run_trial(
        REPO_ROOT,
        "codex",
        "gpt-5.6",
        execute=True,
        artifact_root=tmp_path,
        parent_env=runtime_env(),
        runner=lambda *_args: trial.CommandResult(returncode=0, stdout=stdout, stderr=b""),
        trial_id="forbidden-tool",
    )

    assert report["status"] == "failed"
    assert report["error"] == "harness exposed or used a non-MCP tool"


def test_execute_claude_stream_json_extracts_nested_final_json(tmp_path):
    final = valid_agent_result("from claude stream-json")
    calls = []

    def fake_runner(argv, stdin, env, timeout_seconds):
        calls.append({"argv": argv, "stdin": stdin, "env": dict(env)})
        stdout = (
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": json.dumps(final)}]}})
            + "\n"
        ).encode("utf-8")
        return trial.CommandResult(returncode=0, stdout=stdout, stderr=b"")

    report = trial.run_trial(
        REPO_ROOT,
        "claude-code",
        "claude-opus-5",
        execute=True,
        artifact_root=tmp_path,
        parent_env=runtime_env(),
        runner=fake_runner,
        trial_id="exec-claude-stream",
    )

    assert report["status"] == "completed"
    assert calls[0]["env"]["ANTHROPIC_BASE_URL"] == runtime_env()["RESBENCH_LLM_BASE_URL"]
    assert calls[0]["env"]["ANTHROPIC_AUTH_TOKEN"] == runtime_env()["RESBENCH_LLM_API_KEY"]
    assert calls[0]["env"]["ANTHROPIC_API_KEY"] == runtime_env()["RESBENCH_LLM_API_KEY"]
    assert calls[0]["env"]["RESBENCH_K8S_MCP_URL"] == runtime_env()["RESBENCH_K8S_MCP_URL"]
    assert "OPENAI_API_KEY" not in calls[0]["env"]
    assert "KUBECONFIG" not in calls[0]["env"]
    assert load_json(artifact_ref_path(report, tmp_path, "agentResultRef"))["suspected_defect"] == "from claude stream-json"


def test_execute_invalid_agent_result_fails_and_redacts_secrets(tmp_path):
    def fake_runner(argv, stdin, env, timeout_seconds):
        return trial.CommandResult(
            returncode=0,
            stdout=b'{"status":"completed","suspected_defect":"too short"}\n',
            stderr=b"gateway used sk-test-secret-value-that-must-not-leak",
        )

    report = trial.run_trial(
        REPO_ROOT,
        "codex",
        "gpt-5.6",
        execute=True,
        artifact_root=tmp_path,
        parent_env=runtime_env(),
        runner=fake_runner,
        trial_id="invalid-result",
    )

    assert report["status"] == "failed"
    text = artifact_text(artifact_dir(report, tmp_path))
    assert "sk-test-secret-value-that-must-not-leak" not in text
    assert "<redacted>" in text


def test_deepseek_execute_prepares_home_files_and_omits_prompt_from_artifacts(tmp_path):
    calls = []
    final = valid_agent_result("from dsh stdout")

    def fake_runner(argv, stdin, env, timeout_seconds):
        dsh_home = Path(env["DSH_HOME"])
        settings = (dsh_home / "settings.yaml").read_text(encoding="utf-8")
        cordis = (dsh_home / "cordis.patch.yml").read_text(encoding="utf-8")
        calls.append({"argv": argv, "stdin": stdin, "env": dict(env), "settings": settings, "cordis": cordis})
        return trial.CommandResult(returncode=0, stdout=json.dumps(final).encode("utf-8"), stderr=b"")

    report = trial.run_trial(
        REPO_ROOT,
        "deepseek-harness",
        "deepseek-v4-pro",
        execute=True,
        artifact_root=tmp_path,
        parent_env=runtime_env(),
        runner=fake_runner,
        trial_id="dsh-fail-closed",
    )

    output_dir = artifact_dir(report, tmp_path)
    planned = load_json(output_dir / "planned.json")
    encoded_artifacts = artifact_text(output_dir)
    assert report["status"] == "completed"
    assert calls
    assert calls[0]["argv"][0].endswith("/dsh")
    assert calls[0]["argv"][0].startswith("/")
    assert calls[0]["argv"][1:3] == ["--profile", "headless"]
    assert "Public episode contract follows" in calls[0]["argv"][-1]
    assert calls[0]["stdin"] == b""
    assert calls[0]["env"]["RESBENCH_LLM_API_KEY"] == runtime_env()["RESBENCH_LLM_API_KEY"]
    assert calls[0]["env"]["DSH_TOOLS_MODE"] == "native"
    assert "OPENAI_API_KEY" not in calls[0]["env"]
    assert "ANTHROPIC_AUTH_TOKEN" not in calls[0]["env"]
    assert "baseURL: https://gateway.example/v1" in calls[0]["settings"]
    assert "model: deepseek-v4-pro" in calls[0]["settings"]
    assert "agent-default-model:" in calls[0]["settings"]
    assert "streamable-http" in calls[0]["cordis"]
    assert calls[0]["cordis"].count("failOnStartupError: true") == 4
    assert planned["argv"][0] == "<absolute-command:dsh>"
    assert "Public episode contract follows" not in encoded_artifacts
    assert "<prompt omitted from artifacts>" in encoded_artifacts
    assert not Path(calls[0]["env"]["DSH_HOME"]).exists()


def test_unknown_harness_and_model_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="unknown harness"):
        trial.run_trial(REPO_ROOT, "missing", "gpt-5.6", artifact_root=tmp_path, parent_env=runtime_env())
    with pytest.raises(ValueError, match="unknown model alias"):
        trial.run_trial(REPO_ROOT, "codex", "missing", artifact_root=tmp_path, parent_env=runtime_env())


def test_agent_visible_episode_rejects_forbidden_hidden_keys(tmp_path):
    episode = tmp_path / "episode.yaml"
    episode.write_text(
        "\n".join(
            [
                "schema_version: episode-public.v0.1",
                "episode_id: bad",
                "ground_truth:",
                "  root_cause: hidden",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="forbidden agent-visible key"):
        trial.run_trial(
            REPO_ROOT,
            "codex",
            "gpt-5.6",
            episode_file=episode,
            artifact_root=tmp_path,
            parent_env=runtime_env(),
        )


def test_agent_visible_episode_rejects_normalized_hidden_keys(tmp_path):
    episode = tmp_path / "episode.yaml"
    episode.write_text(
        "\n".join(
            [
                "schema_version: episode-public.v0.1",
                "episode_id: bad",
                "groundTruth:",
                "  root_cause: hidden",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="forbidden agent-visible key"):
        trial.run_trial(
            REPO_ROOT,
            "codex",
            "gpt-5.6",
            episode_file=episode,
            artifact_root=tmp_path,
            parent_env=runtime_env(),
        )


def test_episode_public_schema_is_enforced(tmp_path):
    episode = tmp_path / "episode.yaml"
    episode.write_text(
        "\n".join(
            [
                "schema_version: episode-public.v0.1",
                "episode_id: invalid-lowercase-id",
                "title: Missing required public fields",
                "status: example",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(Exception, match="is a required property|does not match"):
        trial.run_trial(
            REPO_ROOT,
            "codex",
            "gpt-5.6",
            episode_file=episode,
            artifact_root=tmp_path,
            parent_env=runtime_env(),
        )


def test_forbidden_key_uses_normalized_contains_match(tmp_path):
    episode = tmp_path / "episode.yaml"
    episode.write_text(
        "\n".join(
            [
                "schema_version: episode-public.v0.1",
                "episode_id: bad",
                "safe_hidden_truth_bundle:",
                "  root_cause: hidden",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="forbidden agent-visible key"):
        trial.run_trial(
            REPO_ROOT,
            "codex",
            "gpt-5.6",
            episode_file=episode,
            artifact_root=tmp_path,
            parent_env=runtime_env(),
        )


def test_rejects_path_traversal_and_unsafe_roots(tmp_path):
    with pytest.raises(ValueError, match="trial_id"):
        trial.run_trial(REPO_ROOT, "codex", "gpt-5.6", artifact_root=tmp_path, parent_env=runtime_env(), trial_id="../bad")
    with pytest.raises(ValueError, match="artifact_root"):
        trial.run_trial(REPO_ROOT, "codex", "gpt-5.6", artifact_root=Path("/"), parent_env=runtime_env())
    with pytest.raises(ValueError, match="prompt"):
        trial.run_trial(REPO_ROOT, "codex", "gpt-5.6", prompt_ref="../common-task.md", artifact_root=tmp_path, parent_env=runtime_env())


def test_execute_requires_complete_runtime_env_and_valid_urls(tmp_path):
    env = runtime_env()
    env.pop("RESBENCH_MCP_TOKEN")
    with pytest.raises(ValueError, match="RESBENCH_MCP_TOKEN is required"):
        trial.run_trial(REPO_ROOT, "codex", "gpt-5.6", execute=True, artifact_root=tmp_path, parent_env=env, runner=lambda *_: None)

    env = runtime_env()
    env["RESBENCH_K8S_MCP_URL"] = "https://user@example.test/mcp"
    with pytest.raises(ValueError, match="userinfo"):
        trial.run_trial(REPO_ROOT, "codex", "gpt-5.6", execute=True, artifact_root=tmp_path, parent_env=env, runner=lambda *_: None)

    env = runtime_env()
    env["RESBENCH_MCP_TOKEN"] = "short"
    with pytest.raises(ValueError, match="at least"):
        trial.run_trial(REPO_ROOT, "codex", "gpt-5.6", execute=True, artifact_root=tmp_path, parent_env=env, runner=lambda *_: None)

    env = runtime_env()
    env["RESBENCH_MCP_TOKEN"] = " " + runtime_env()["RESBENCH_MCP_TOKEN"]
    with pytest.raises(ValueError, match="whitespace"):
        trial.run_trial(REPO_ROOT, "codex", "gpt-5.6", execute=True, artifact_root=tmp_path, parent_env=env, runner=lambda *_: None)


def test_numeric_boundaries_are_enforced(tmp_path):
    with pytest.raises(ValueError, match="timeout_seconds"):
        trial.run_trial(REPO_ROOT, "codex", "gpt-5.6", artifact_root=tmp_path, parent_env=runtime_env(), timeout_seconds=0)
    with pytest.raises(ValueError, match="max_output_bytes"):
        trial.run_trial(REPO_ROOT, "codex", "gpt-5.6", artifact_root=tmp_path, parent_env=runtime_env(), max_output_bytes=10)
