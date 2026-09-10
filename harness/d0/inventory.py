"""Non-sensitive execution identity captured before a real D0 Campaign."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

from .common import AGENTS, append_jsonl, redact_sensitive_text, sha256_file, utc_now


Runner = Callable[..., subprocess.CompletedProcess[str]]

PROTOCOLS = {
    "bladeai": {
        "agent_transport": "bladeai-task-via-agent-exec",
        "tool_boundary": "trial-bound-mcp-and-controlled-shim",
    },
    "codex": {
        "agent_transport": "codex-headless-jsonl",
        "provider_protocol": "openai-responses",
        "tool_boundary": "trial-bound-streamable-http-mcp",
    },
    "claude-code": {
        "agent_transport": "claude-code-headless-stream-json",
        "provider_protocol": "anthropic-messages",
        "tool_boundary": "trial-bound-http-mcp-allowedTools",
    },
    "deepseek-harness": {
        "agent_transport": "dsh-headless-session-jsonl",
        "provider_protocol": "openai-completions",
        "tool_boundary": "trial-bound-streamable-http-mcp",
    },
}


def _command_record(
    *,
    command_path: Path,
    host: Mapping[str, Any],
    argv: list[str],
    runner: Runner,
    parse_json: bool = False,
    timeout: int = 10,
) -> tuple[subprocess.CompletedProcess[str], Any]:
    started_at = utc_now()
    started = time.monotonic()
    completed = runner(
        argv,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    finished_at = utc_now()
    parsed: Any = None
    if parse_json and completed.returncode == 0:
        try:
            parsed = json.loads(completed.stdout)
        except json.JSONDecodeError:
            parsed = None
    stdout = redact_sensitive_text(completed.stdout[:2000])
    if parse_json:
        stdout = (
            "<structured identity response omitted; "
            f"sha256={hashlib.sha256(completed.stdout.encode()).hexdigest()}; "
            f"bytes={len(completed.stdout.encode())}>"
        )
    recorded_argv = list(argv)
    if "--kubeconfig" in recorded_argv:
        index = recorded_argv.index("--kubeconfig")
        if index + 1 < len(recorded_argv):
            recorded_argv[index + 1] = "<kubeconfig>"
    append_jsonl(
        command_path,
        {
            "ts": finished_at,
            "actor": "controller",
            "kind": "campaign_identity_inventory",
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_ms": round((time.monotonic() - started) * 1000, 1),
            "execution_host_id": host.get("declared_host_id"),
            "hostname": host.get("hostname"),
            "platform": host.get("platform"),
            "pid": os.getpid(),
            "working_directory": str(Path.cwd()),
            "argv": recorded_argv,
            "returncode": completed.returncode,
            "stdout": stdout,
            "stderr": redact_sensitive_text(completed.stderr[:1000]),
        },
    )
    return completed, parsed


def _executable_identity(
    agent: str,
    command: str,
    env: Mapping[str, str],
    runner: Runner,
    command_path: Path,
    host: Mapping[str, Any],
    *,
    runtime_descriptor: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    descriptor = dict(runtime_descriptor or {})
    capability = dict(descriptor.get("capability") or {})
    return {
        "command": command,
        **PROTOCOLS[agent],
        "available": capability.get("qualification_passed") is True,
        "runtime_source": "qualification_descriptor",
        "execution_boundary": "agent_exec_sidecar",
        "capability": capability,
        "qualification": descriptor.get("qualification", {"status": "capability_probe_missing"}),
    }


def collect_execution_inventory(
    *,
    repo_root: Path,
    kubeconfig: Path,
    artifact_root: Path,
    campaign_dir: Path,
    host: Mapping[str, Any],
    models: Mapping[str, str],
    environment: Mapping[str, str],
    runtime_descriptors: Mapping[str, Mapping[str, Any]] | None = None,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    harness_registry = yaml.safe_load(
        (repo_root / "harness/harnesses.yaml").read_text(encoding="utf-8")
    )
    model_registry = yaml.safe_load(
        (repo_root / "harness/models.yaml").read_text(encoding="utf-8")
    )
    harnesses = harness_registry.get("harnesses", {})
    registered_models = model_registry.get("models", {})
    runtimes = {}
    model_identities = {}
    commands = campaign_dir / "campaign-controller-commands.jsonl"
    for agent in AGENTS:
        definition = harnesses.get(agent, {})
        entrypoint = definition.get("entrypoint", {})
        runtimes[agent] = _executable_identity(
            agent,
            str(entrypoint.get("command") or agent),
            environment,
            runner,
            commands,
            host,
            runtime_descriptor=(runtime_descriptors or {}).get(agent),
        ) | {
            "adapter_registry_version": harness_registry.get("version"),
            "entrypoint_mode": entrypoint.get("mode"),
            "prompt_transport": entrypoint.get("prompt_transport"),
        }
        alias = str(models[agent])
        registered = dict(registered_models.get(alias, {}))
        model_identities[agent] = {
            "requested_alias": alias,
            "declared_upstream_model": registered.get("upstream_model"),
            "protocol_candidates": registered.get("protocol_candidates", []),
            "provider_protocol": PROTOCOLS[agent].get("provider_protocol", "product-managed"),
        }

    kubectl = ["kubectl", "--kubeconfig", str(kubeconfig)]
    context_result, _ = _command_record(
        command_path=commands,
        host=host,
        argv=[*kubectl, "config", "current-context"],
        runner=runner,
    )
    _, view = _command_record(
        command_path=commands,
        host=host,
        argv=[*kubectl, "config", "view", "--minify", "-o", "json"],
        runner=runner,
        parse_json=True,
    )
    _, whoami = _command_record(
        command_path=commands,
        host=host,
        argv=[*kubectl, "auth", "whoami", "-o", "json"],
        runner=runner,
        parse_json=True,
    )
    server = ""
    if isinstance(view, dict):
        clusters = view.get("clusters") or []
        if clusters:
            server = str(clusters[0].get("cluster", {}).get("server") or "")
    identity = {}
    if isinstance(whoami, dict):
        status = whoami.get("status") or whoami
        identity = {
            "username": status.get("userInfo", {}).get("username")
            or status.get("username"),
            "groups": status.get("userInfo", {}).get("groups")
            or status.get("groups", []),
        }
    python_path = Path(sys.executable).resolve()
    return {
        "schema_version": "d0-execution-inventory.v1",
        "captured_at": utc_now(),
        "controller": {
            "version": "d0-campaign.v1",
            "python_executable": str(python_path),
            "python_executable_sha256": sha256_file(python_path)
            if python_path.is_file()
            else None,
            "working_directory": str(Path.cwd()),
            "repo_root": str(repo_root),
            "artifact_root": str(artifact_root),
            "campaign_artifact_dir": str(campaign_dir),
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
        },
        "agents": runtimes,
        "models": model_identities,
        "kubernetes": {
            "context": context_result.stdout.strip()
            if context_result.returncode == 0
            else None,
            "namespace": "otel-demo",
            "api_server_sha256": hashlib.sha256(server.encode()).hexdigest()
            if server
            else None,
            "identity": identity,
        },
    }
