import json
import sys
import threading
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
        "BLADE_AI_KUBECONFIG_PATH": "/tmp/bladeai-should-not-pass",
        "CLAUDE_CONFIG_FILE": "/root/.claude/resbench-mcp.json",
        "DSH_PERMISSION_MODE": "danger-full-access",
        "HOME": "/root",
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


def test_streaming_runner_honors_controller_cancellation():
    cancel = threading.Event()
    timer = threading.Timer(0.2, cancel.set)
    timer.start()
    try:
        result = trial.subprocess_streaming_runner(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            b"",
            {},
            60,
            lambda _line: None,
            cancel.is_set,
        )
    finally:
        timer.cancel()

    assert result.cancelled is True
    assert result.timed_out is False


def test_streaming_runner_archives_unsupported_structured_feedback(tmp_path):
    transcript = tmp_path / "session-events.jsonl"

    result = trial.subprocess_streaming_runner(
        [
            sys.executable,
            "-c",
            "import json; print(json.dumps({'type':'message','status':'done'}), flush=True)",
        ],
        b"",
        {},
        5,
        lambda _line: {
            "category": "FACT_EVENT",
            "message": "channel restored",
            "payload": {"case_id": "D5"},
        },
        transcript_path=transcript,
    )

    records = [
        json.loads(line)
        for line in transcript.read_text(encoding="utf-8").splitlines()
    ]

    assert result.returncode == 0
    assert any(item["event"] == "FEEDBACK_UNSUPPORTED" for item in records)
    unsupported = next(item for item in records if item["event"] == "FEEDBACK_UNSUPPORTED")
    assert unsupported["payload"]["category"] == "FACT_EVENT"
    assert unsupported["payload"]["reason"] == "harness native resume API is not configured"


def test_streaming_runner_delivers_turn_complete_feedback_with_native_resume(tmp_path):
    transcript = tmp_path / "session-events.jsonl"
    prompts = []

    def turn_complete(_summary):
        if prompts:
            return None
        prompts.append("queued")
        return {
            "category": "FACT_EVENT",
            "message": "target rebound",
            "payload": {"case_id": "D2"},
        }

    result = trial.subprocess_streaming_runner(
        [sys.executable, "-c", "print('initial', flush=True)"],
        b"",
        {},
        5,
        lambda _line: None,
        resume_argv_builder=lambda session_id, _turn: [
            sys.executable,
            "-c",
            f"import sys; assert {session_id!r} == 'session-1'; sys.stdin.read(); print('resumed', flush=True)",
        ],
        session_id_provider=lambda: "session-1",
        turn_complete_observer=turn_complete,
        transcript_path=transcript,
    )

    records = [
        json.loads(line)
        for line in transcript.read_text(encoding="utf-8").splitlines()
    ]

    assert result.returncode == 0
    assert b"initial" in result.stdout
    assert b"resumed" in result.stdout
    assert any(item["event"] == "FEEDBACK_DELIVERED" for item in records)


def test_autonomous_session_rejects_semantic_nudge(tmp_path):
    transcript = tmp_path / "session-events.jsonl"

    result = trial.subprocess_streaming_runner(
        [sys.executable, "-c", "print('initial', flush=True)"],
        b"",
        {},
        5,
        lambda _line: {
            "category": "SEMANTIC_NUDGE",
            "message": "please verify effect now",
        },
        resume_argv_builder=lambda _session_id, _turn: [
            sys.executable,
            "-c",
            "raise SystemExit(99)",
        ],
        session_id_provider=lambda: "session-1",
        interaction_mode="autonomous",
        transcript_path=transcript,
    )

    records = [
        json.loads(line)
        for line in transcript.read_text(encoding="utf-8").splitlines()
    ]

    assert result.returncode == 0
    failed = next(item for item in records if item["event"] == "FEEDBACK_FAILED")
    assert failed["payload"]["category"] == "SEMANTIC_NUDGE"
    assert "forbidden" in failed["payload"]["reason"]


def test_run_trace_schema_uses_existing_controller_abort_status():
    schema = load_json(REPO_ROOT / "harness/schemas/run-trace.schema.json")
    allowed = schema["properties"]["final_output"]["properties"]["status"]["enum"]

    assert "aborted_by_controller" in allowed
    assert "cancelled_by_controller" not in allowed


def valid_agent_result(defect: str = "missing outbound timeout"):
    return {
        "status": "completed",
        "interaction_mode": "guided",
        "autonomy_level": "L1_COMPLETE_EXPERIMENT",
        "assisted": False,
        "assistance_events": [],
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


def test_codex_build_argv_removes_ephemeral_for_native_resume():
    harnesses = yaml.safe_load((REPO_ROOT / "harness/harnesses.yaml").read_text(encoding="utf-8"))
    argv, stdin, fail_closed = trial.build_argv(
        "codex",
        harnesses["harnesses"]["codex"],
        "gpt-5.6",
        "prompt",
        {
            "output_schema_file": REPO_ROOT / "harness/schemas/agent-result.schema.json",
            "codex_last_message_file": REPO_ROOT / "codex-last-message.json",
        },
    )

    assert fail_closed is None
    assert stdin == b"prompt"
    assert "--ephemeral" not in argv


def test_codex_resume_builder_uses_same_trial_local_session_files(tmp_path):
    result_files = [tmp_path / "codex-last-message.json"]
    builder = trial.build_codex_resume_argv_builder(
        command="codex-eval",
        model_alias="gpt-5.6-sol",
        paths={
            "output_schema_file": REPO_ROOT / "harness/schemas/agent-result.schema.json",
            "codex_last_message_file": result_files[0],
        },
        candidate_files=result_files,
    )

    argv = list(builder("session-123", 1))

    assert argv[:3] == ["codex-eval", "exec", "resume"]
    assert "session-123" in argv
    assert "--ephemeral" not in argv
    assert result_files[-1] == tmp_path / "codex-last-message-resume-01.json"


def test_claude_build_argv_removes_no_session_persistence_for_native_resume():
    harnesses = yaml.safe_load((REPO_ROOT / "harness/harnesses.yaml").read_text(encoding="utf-8"))
    argv, stdin, fail_closed = trial.build_argv(
        "claude-code",
        harnesses["harnesses"]["claude-code"],
        "claude-opus-5",
        "prompt",
        {"mcp_config_file": REPO_ROOT / "mcp.json"},
    )

    assert fail_closed is None
    assert stdin == b"prompt"
    assert "--no-session-persistence" not in argv


def test_claude_resume_builder_uses_official_resume_flag(tmp_path):
    builder = trial.build_claude_resume_argv_builder(
        command="claude",
        model_alias="claude-opus-5",
        paths={"mcp_config_file": tmp_path / "mcp.json"},
    )

    argv = list(builder("claude-session-1", 1))

    assert argv[:4] == ["claude", "--print", "--verbose", "--bare"]
    assert "--resume" in argv
    assert argv[argv.index("--resume") + 1] == "claude-session-1"
    assert "--no-session-persistence" not in argv


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
    assert "BLADE_AI_KUBECONFIG_PATH" not in planned["envKeys"]
    assert "CLAUDE_CONFIG_FILE" not in planned["envKeys"]
    assert "DSH_PERMISSION_MODE" not in planned["envKeys"]
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


def test_codex_oauth_mode_uses_ephemeral_auth_without_gateway_or_artifact_leak(
    tmp_path,
):
    auth = tmp_path / "source-auth.json"
    auth_secret = "oauth-refresh-secret-that-must-not-leak"
    auth.write_text(json.dumps({"refresh_token": auth_secret}), encoding="utf-8")
    auth.chmod(0o600)
    env = runtime_env()
    env.pop("RESBENCH_LLM_BASE_URL")
    env.pop("RESBENCH_LLM_API_KEY")
    env["RESBENCH_CODEX_AUTH_FILE"] = str(auth)
    observed = {}

    def fake_runner(argv, stdin, child_env, timeout_seconds):
        codex_home = Path(child_env["CODEX_HOME"])
        observed["auth_exists"] = (codex_home / "auth.json").is_file()
        observed["auth_mode"] = (codex_home / "auth.json").stat().st_mode & 0o777
        observed["config"] = (codex_home / "config.toml").read_text(encoding="utf-8")
        observed["child_env"] = dict(child_env)
        return trial.CommandResult(
            returncode=0,
            stdout=(json.dumps(valid_agent_result()) + "\n").encode(),
            stderr=b"",
        )

    report = trial.run_trial(
        REPO_ROOT,
        "codex",
        "gpt-5.6",
        execute=True,
        artifact_root=tmp_path / "artifacts",
        parent_env=env,
        runner=fake_runner,
        trial_id="exec-codex-oauth",
    )
    output_dir = artifact_dir(report, tmp_path / "artifacts")
    planned = load_json(output_dir / "planned.json")

    assert report["status"] == "completed"
    assert observed["auth_exists"] is True
    assert observed["auth_mode"] == 0o600
    assert "model_provider" not in observed["config"]
    assert 'approval_policy = "never"' in observed["config"]
    assert "chaos_create_experiment" in observed["config"]
    assert 'approval_mode = "approve"' in observed["config"]
    assert "OPENAI_API_KEY" not in observed["child_env"]
    assert "OPENAI_BASE_URL" not in observed["child_env"]
    assert planned["templates"]["codexAuthMode"] == "chatgpt-oauth"
    assert auth_secret not in artifact_text(output_dir)
    assert not any(path.is_dir() for path in output_dir.iterdir())


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


def test_connectivity_smoke_prompt_is_bounded_and_non_mutating():
    harnesses = yaml.safe_load((REPO_ROOT / "harness/harnesses.yaml").read_text(encoding="utf-8"))
    prompt = trial.resolve_prompt_file(harnesses, "connectivity_smoke", REPO_ROOT).read_text(encoding="utf-8")

    assert "no more than four MCP tool calls" in prompt
    assert "not authorization to inject a fault" in prompt
    assert "Do not call any create, destroy, mutation" in prompt


def test_claude_stream_json_registry_enables_verbose_mode():
    harnesses = yaml.safe_load((REPO_ROOT / "harness/harnesses.yaml").read_text(encoding="utf-8"))
    args = harnesses["harnesses"]["claude-code"]["entrypoint"]["args"]

    assert "--print" in args
    assert "stream-json" in args
    assert "--verbose" in args


def test_runtime_fault_capability_is_short_lived_and_redacted_from_artifacts():
    token = "baseline-capability-token-with-at-least-32-characters"
    env = {
        "RESBENCH_BASELINE_GATE_TOKEN": token,
        "RESBENCH_CLEANUP_HANDLE": "cleanup-run-001-attempt-001",
        "RESBENCH_CHAOS_CONTROLLER_TOKEN_REF": "runtime://controller/token",
        "RESBENCH_CHAOS_CONTROLLER_POD_UID": "controller-uid",
        "RESBENCH_AUTHORIZED_TARGET_JSON": json.dumps(
            {"namespace": "otel-demo", "name": "frontend-abc", "uid": "pod-uid"}
        ),
        "RESBENCH_MAIN_FAULT_JSON": json.dumps(
            {"type": "network-delay", "parameters": {"delay_ms": 100}}
        ),
        "RESBENCH_AUTHORIZED_RUN_ID": "run-capability-001",
    }

    prompt = trial.append_runtime_capability_prompt("public task", env)

    assert token in prompt
    assert "frontend-abc" in prompt
    assert "run-capability-001" in prompt
    assert token not in trial.redact_text(prompt, env)


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
    assert calls[0]["env"]["HOME"] == calls[0]["env"]["CODEX_HOME"]
    assert "KUBECONFIG" not in calls[0]["env"]
    assert "BLADE_AI_KUBECONFIG_PATH" not in calls[0]["env"]
    assert "DSH_PERMISSION_MODE" not in calls[0]["env"]
    assert "HARBOR_REGISTRY" not in calls[0]["env"]
    trace = load_json(artifact_ref_path(report, tmp_path, "runTraceRef"))
    assert trace["events"][1]["kind"] == "agent_message"
    assert any(event["kind"] == "tool_call" and event["tool"] == "k8s_ro.k8s_cluster_inventory" for event in trace["events"])
    assert load_json(artifact_ref_path(report, tmp_path, "agentResultRef"))["suspected_defect"] == "missing outbound timeout"
    assert not any(path.is_dir() for path in artifact_dir(report, tmp_path).iterdir())


def test_d0_exact_prompt_override_is_not_wrapped_and_does_not_require_result_schema(tmp_path):
    calls = []
    prompt = "请针对otel-demo下的accounting服务的一个 pod 注入高 cpu 故障，持续 5 分钟，5 分钟后需要自动恢复"

    def fake_runner(argv, stdin, env, timeout_seconds):
        calls.append({"stdin": stdin, "argv": argv})
        return trial.CommandResult(returncode=0, stdout=b"agent finished\n", stderr=b"")

    report = trial.run_trial(
        REPO_ROOT,
        "codex",
        "gpt-5.6",
        execute=True,
        artifact_root=tmp_path,
        parent_env=runtime_env(),
        runner=fake_runner,
        trial_id="d0-exact-prompt",
        prompt_text_override=prompt,
        require_structured_result=False,
        enforce_formal_runtime=False,
    )

    assert report["status"] == "completed"
    assert calls[0]["stdin"] == prompt.encode("utf-8")
    assert b"Public episode contract follows" not in calls[0]["stdin"]
    assert report["promptSha256"] == trial.hashlib.sha256(prompt.encode()).hexdigest()


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


def test_codex_item_started_and_completed_are_distinct_nonduplicated_events():
    started = {
        "type": "item.started",
        "item": {
            "type": "mcp_tool_call",
            "server": "telemetry_ro",
            "tool": "telemetry_prom_metric_range",
            "status": "in_progress",
            "result": None,
            "error": None,
        },
    }
    completed = {
        "type": "item.completed",
        "item": {
            "type": "mcp_tool_call",
            "server": "telemetry_ro",
            "tool": "telemetry_prom_metric_range",
            "status": "completed",
            "result": {"content": []},
            "error": None,
        },
    }
    objects = trial.extract_json_objects(
        json.dumps(started) + "\n" + json.dumps(completed) + "\n"
    )
    tool_events = [item for item in objects if item.get("type") == "mcp_tool_call"]

    assert len(tool_events) == 2
    assert [trial.trace_kind_from_event(item) for item in tool_events] == [
        "tool_call",
        "tool_result",
    ]


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
    assert calls[0]["env"]["ANTHROPIC_BASE_URL"] == "https://gateway.example"
    assert calls[0]["env"]["ANTHROPIC_AUTH_TOKEN"] == runtime_env()["RESBENCH_LLM_API_KEY"]
    assert calls[0]["env"]["ANTHROPIC_API_KEY"] == runtime_env()["RESBENCH_LLM_API_KEY"]
    assert calls[0]["env"]["RESBENCH_K8S_MCP_URL"] == runtime_env()["RESBENCH_K8S_MCP_URL"]
    assert calls[0]["env"]["HOME"] == calls[0]["env"]["CLAUDE_CONFIG_DIR"]
    assert "OPENAI_API_KEY" not in calls[0]["env"]
    assert "KUBECONFIG" not in calls[0]["env"]
    assert "CLAUDE_CONFIG_FILE" not in calls[0]["env"]
    assert "DSH_PERMISSION_MODE" not in calls[0]["env"]
    assert load_json(artifact_ref_path(report, tmp_path, "agentResultRef"))["suspected_defect"] == "from claude stream-json"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://gateway.example/v1", "https://gateway.example"),
        ("https://gateway.example/v1/", "https://gateway.example"),
        ("https://gateway.example/api", "https://gateway.example/api"),
    ],
)
def test_anthropic_base_url_removes_only_v1_suffix(value, expected):
    assert trial.anthropic_base_url(value) == expected


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
        "gpt-5.6-sol",
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
    assert calls[0]["argv"][0] == "dsh"
    assert calls[0]["argv"][1:3] == ["--profile", "headless"]
    assert "Public episode contract follows" in calls[0]["argv"][-1]
    assert calls[0]["stdin"] == b""
    assert calls[0]["env"]["RESBENCH_LLM_API_KEY"] == runtime_env()["RESBENCH_LLM_API_KEY"]
    assert calls[0]["env"]["DSH_TOOLS_MODE"] == "native"
    assert "OPENAI_API_KEY" not in calls[0]["env"]
    assert "ANTHROPIC_AUTH_TOKEN" not in calls[0]["env"]
    assert "baseURL: https://gateway.example/v1" in calls[0]["settings"]
    assert "api: openai-completions" in calls[0]["settings"]
    assert "model: gpt-5.6-sol" in calls[0]["settings"]
    assert "id: gpt-5.6-sol" in calls[0]["settings"]
    assert "deepseek-v4-pro" not in calls[0]["settings"]
    assert "agent-default-model:" in calls[0]["settings"]
    assert "streamable-http" in calls[0]["cordis"]
    assert calls[0]["cordis"].count("failOnStartupError: true") == 4
    assert planned["argv"][0] == "dsh"
    assert "Public episode contract follows" not in encoded_artifacts
    assert "<prompt omitted from artifacts>" in encoded_artifacts
    assert not Path(calls[0]["env"]["DSH_HOME"]).exists()
    assert calls[0]["env"]["HOME"] == calls[0]["env"]["DSH_HOME"]
    assert "KUBECONFIG" not in calls[0]["env"]
    assert "BLADE_AI_KUBECONFIG_PATH" not in calls[0]["env"]
    assert "DSH_PERMISSION_MODE" not in calls[0]["env"]


def test_deepseek_claude_model_uses_anthropic_protocol(tmp_path):
    calls = []

    def fake_runner(argv, stdin, env, timeout_seconds):
        del argv, stdin, timeout_seconds
        calls.append(
            (Path(env["DSH_HOME"]) / "settings.yaml").read_text(encoding="utf-8")
        )
        return trial.CommandResult(
            returncode=0,
            stdout=json.dumps(valid_agent_result("opus through dsh")).encode(),
            stderr=b"",
        )

    report = trial.run_trial(
        REPO_ROOT,
        "deepseek-harness",
        "claude-opus-5",
        execute=True,
        artifact_root=tmp_path,
        parent_env=runtime_env(),
        runner=fake_runner,
        trial_id="dsh-opus-protocol",
    )

    assert report["status"] == "completed"
    assert "api: anthropic-messages" in calls[0]
    assert "baseURL: https://gateway.example" in calls[0]
    assert "id: claude-opus-5" in calls[0]
    assert "gpt-5.6-sol" not in calls[0]


def test_formal_runtime_preflight_rejects_bypass_arguments_and_host_permissions(tmp_path):
    trial_root = tmp_path / "trial"
    codex_home = trial_root / "codex-home"
    codex_home.mkdir(parents=True)
    (codex_home / "config.toml").write_text(
        '\n'.join(
            (
                'approval_policy = "never"',
                'shell_tool = false',
                'unified_exec = false',
                'browser_use = false',
                'computer_use = false',
            )
        ),
        encoding="utf-8",
    )
    homes = {
        "CODEX_HOME": str(codex_home),
        "CLAUDE_CONFIG_DIR": str(trial_root / "claude-home"),
        "DSH_HOME": str(trial_root / "dsh-home"),
    }
    token = runtime_env()["RESBENCH_MCP_TOKEN"]
    child_env = {
        "PATH": trial.SAFE_PATH,
        "HOME": str(codex_home),
        "CODEX_HOME": str(codex_home),
        "RESBENCH_MCP_TOKEN": token,
    }

    trial.validate_formal_harness_runtime(
        "codex",
        ["codex", "exec", "--sandbox", "read-only"],
        child_env,
        homes,
        {},
        trial_root,
        token,
    )

    with pytest.raises(ValueError, match="permission bypass"):
        trial.validate_formal_harness_runtime(
            "codex",
            ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox"],
            child_env,
            homes,
            {},
            trial_root,
            token,
        )

    polluted_env = {**child_env, "KUBECONFIG": "/root/.kube/config"}
    with pytest.raises(ValueError, match="host capability"):
        trial.validate_formal_harness_runtime(
            "codex",
            ["codex", "exec", "--sandbox", "read-only"],
            polluted_env,
            homes,
            {},
            trial_root,
            token,
        )


def test_formal_claude_preflight_rejects_global_mcp_config(tmp_path):
    trial_root = tmp_path / "trial"
    claude_home = trial_root / "claude-home"
    claude_home.mkdir(parents=True)
    global_config = tmp_path / "global-resbench-mcp.json"
    global_config.write_text("{}", encoding="utf-8")
    homes = {
        "CODEX_HOME": str(trial_root / "codex-home"),
        "CLAUDE_CONFIG_DIR": str(claude_home),
        "DSH_HOME": str(trial_root / "dsh-home"),
    }
    token = runtime_env()["RESBENCH_MCP_TOKEN"]
    child_env = {
        "PATH": trial.SAFE_PATH,
        "HOME": str(claude_home),
        "CLAUDE_CONFIG_DIR": str(claude_home),
        "RESBENCH_MCP_TOKEN": token,
    }

    with pytest.raises(ValueError, match="Trial-local config"):
        trial.validate_formal_harness_runtime(
            "claude-code",
            ["claude", "--mcp-config", str(global_config), "--strict-mcp-config"],
            child_env,
            homes,
            {"mcp_config_file": global_config},
            trial_root,
            token,
        )


def test_formal_command_resolution_rejects_non_registry_command():
    with pytest.raises(ValueError, match="trusted basename"):
        trial.resolve_formal_harness_command("deepseek-harness", "/root/bin/dsh")


def test_formal_command_resolution_rejects_root_runner(monkeypatch):
    monkeypatch.setattr(trial.os, "geteuid", lambda: 0)

    with pytest.raises(ValueError, match="non-root"):
        trial.resolve_formal_harness_command("codex", "codex")


def test_extract_json_objects_accepts_multiline_fenced_agent_result():
    final = valid_agent_result("from fenced dsh output")
    text = "DeepSeek result follows:\n```json\n" + json.dumps(final, indent=2) + "\n```\n"

    candidates = trial.extract_json_objects(text)

    assert final in candidates


def test_extract_provider_reported_model_uses_only_runtime_metadata():
    output = "\n".join(
        (
            json.dumps({"status": "completed", "model": "agent-claimed-model"}),
            json.dumps(
                {
                    "type": "system",
                    "subtype": "init",
                    "model": "claude-opus-5",
                }
            ),
        )
    )

    assert trial.extract_provider_reported_model(output) == "claude-opus-5"
    assert (
        trial.extract_provider_reported_model(
            json.dumps({"status": "completed", "model": "agent-claimed-model"})
        )
        is None
    )


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
