#!/usr/bin/env python3
"""Run one BenchmarkFactory agent harness trial.

The launcher is dry-run first. Execute mode uses subprocess argv lists only,
creates trial-local homes, passes prompts through the harness-declared transport,
and records redacted artifacts for replay and evaluation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import jsonschema
import yaml

from stage2_service.session import (
    HarnessSession,
    ResumeArgvBuilder,
    discover_codex_session_id,
    session_id_from_event,
    structured_feedbacks_from_observer,
)

DEFAULT_HARNESSES_CONFIG = Path("harness/harnesses.yaml")
DEFAULT_MODELS_CONFIG = Path("harness/models.yaml")
DEFAULT_EPISODE = Path("tasks/examples/public/episode.timeout-missing.v0.1.yaml")
DEFAULT_OUTPUT_SCHEMA = Path("harness/schemas/agent-result.schema.json")
DEFAULT_RUN_TRACE_SCHEMA = Path("harness/schemas/run-trace.schema.json")
DEFAULT_EPISODE_SCHEMA = Path("tasks/schemas/episode-public.schema.json")
DEFAULT_MULTI_LEVEL_EPISODE_SCHEMA = Path("tasks/schemas/multi-level-episode.schema.json")
DEFAULT_ARTIFACT_ROOT = Path("artifacts/harness")
DEFAULT_PROMPT_KEY = "full_lifecycle"
# A Trial may contain a 20-minute fault window. Keep ten additional minutes for
# target discovery, baseline/effect checks, bounded cleanup, and final output.
DEFAULT_TIMEOUT_SECONDS = 30 * 60
DEFAULT_MAX_OUTPUT_BYTES = 2_000_000
MAX_PROMPT_BYTES = 200_000
MIN_TIMEOUT_SECONDS = 1
MAX_TIMEOUT_SECONDS = 7_200
MIN_OUTPUT_BYTES = 1_024
MAX_OUTPUT_BYTES = 10_000_000
MIN_MCP_TOKEN_LENGTH = 32
MAX_TRIAL_ID_LENGTH = 128
TRIAL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SAFE_PATH = (
    "/usr/local/bin:/opt/resiliencebenchmark/deepseek-harness/bin:"
    "/opt/homebrew/bin:/usr/bin:/bin"
)
FORMAL_HARNESS_COMMANDS = {
    "codex": "codex",
    "claude-code": "claude",
    "deepseek-harness": "dsh",
}
FORBIDDEN_FORMAL_ARGUMENTS = {
    "--dangerously-bypass-approvals-and-sandbox",
    "--dangerously-skip-permissions",
    "--yolo",
    "bypassPermissions",
    "danger-full-access",
}
FORBIDDEN_FORMAL_ENV = {
    "BLADE_AI_KUBECONFIG_PATH",
    "CLAUDE_CONFIG_FILE",
    "DSH_PERMISSION_MODE",
    "KUBECONFIG",
}
FORBIDDEN_LAUNCHER_MARKERS = {
    "acucompute.env",
    "resbench-mcp.json",
    "--dangerously-bypass-approvals-and-sandbox",
    "--dangerously-skip-permissions",
    "DSH_PERMISSION_MODE",
    "danger-full-access",
}

ALLOWED_RUNTIME_ENV = {
    "RESBENCH_LLM_BASE_URL",
    "RESBENCH_LLM_API_KEY",
    "RESBENCH_K8S_MCP_URL",
    "RESBENCH_TELEMETRY_MCP_URL",
    "RESBENCH_SOURCE_MCP_URL",
    "RESBENCH_CHAOS_CONTROL_MCP_URL",
    "RESBENCH_MCP_TOKEN",
    "RESBENCH_BASELINE_GATE_TOKEN",
    "RESBENCH_CLEANUP_HANDLE",
    "RESBENCH_CHAOS_CONTROLLER_TOKEN_REF",
    "RESBENCH_CHAOS_CONTROLLER_POD_UID",
    "RESBENCH_AUTHORIZED_TARGET_JSON",
    "RESBENCH_MAIN_FAULT_JSON",
    "RESBENCH_AUTHORIZED_RUN_ID",
    "RESBENCH_CODEX_AUTH_FILE",
}
OPTIONAL_RUNTIME_ENV = {
    "RESBENCH_BASELINE_GATE_TOKEN",
    "RESBENCH_CLEANUP_HANDLE",
    "RESBENCH_CHAOS_CONTROLLER_TOKEN_REF",
    "RESBENCH_CHAOS_CONTROLLER_POD_UID",
    "RESBENCH_AUTHORIZED_TARGET_JSON",
    "RESBENCH_MAIN_FAULT_JSON",
    "RESBENCH_AUTHORIZED_RUN_ID",
    "RESBENCH_CODEX_AUTH_FILE",
}
ALLOWED_MCP_TOOLS = {
    "k8s_ro": {
        "k8s_get_resource",
        "k8s_list_resources",
        "k8s_list_events",
        "k8s_pod_logs",
        "k8s_cluster_inventory",
    },
    "telemetry_ro": {
        "telemetry_prom_metric_instant",
        "telemetry_prom_metric_range",
        "telemetry_prom_metric_series",
        "telemetry_prom_list_labels",
        "telemetry_jaeger_list_services",
        "telemetry_jaeger_list_operations",
        "telemetry_jaeger_find_traces",
        "telemetry_loki_list_labels",
        "telemetry_loki_logs",
        "telemetry_loki_logs_range",
    },
    "source_ro": {
        "source_list_repositories",
        "source_list_files",
        "source_search_text",
        "source_read_file",
        "source_show_commit",
    },
    "chaos_control": {
        "chaos_validate_plan",
        "chaos_inventory_run",
        "chaos_create_experiment",
        "chaos_get_experiment",
        "chaos_operation_status",
        "chaos_destroy_experiment",
        "chaos_recovery_status",
    },
}
MCP_URL_ENV = {
    "__RESBENCH_K8S_MCP_URL__": "RESBENCH_K8S_MCP_URL",
    "__RESBENCH_TELEMETRY_MCP_URL__": "RESBENCH_TELEMETRY_MCP_URL",
    "__RESBENCH_SOURCE_MCP_URL__": "RESBENCH_SOURCE_MCP_URL",
    "__RESBENCH_CHAOS_CONTROL_MCP_URL__": "RESBENCH_CHAOS_CONTROL_MCP_URL",
}
FORBIDDEN_AGENT_KEYS = {
    "groundtruth",
    "hiddentruth",
    "oracle",
    "evaluatororacle",
    "oraclerawverdicts",
    "injecteddefect",
    "injecteddefectmanifest",
    "scoringrubricweights",
}
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    re.compile(r"(?i)(api[_-]?key|token|password|secret)\s*[:=]\s*['\"]?[^'\"\s,}]+"),
]


@dataclass
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    cancelled: bool = False


Runner = Callable[[Sequence[str], bytes, Mapping[str, str], int], CommandResult]
EventObserver = Callable[[Mapping[str, Any]], None]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def redaction_values(env: Mapping[str, str]) -> list[str]:
    return [value for key, value in env.items() if key in ALLOWED_RUNTIME_ENV and value]


def redact_text(value: bytes | str, env: Mapping[str, str]) -> str:
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)
    redacted = text
    for secret in redaction_values(env):
        redacted = redacted.replace(secret, "<redacted>")
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub("<redacted>", redacted)
    return redacted


def redact_json(value: Any, env: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        return redact_text(value, env)
    if isinstance(value, list):
        return [redact_json(item, env) for item in value]
    if isinstance(value, dict):
        return {str(key): redact_json(item, env) for key, item in value.items()}
    return value


def make_trial_id(harness: str, episode_id: str, model_alias: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    raw = f"{stamp}-{episode_id}-{harness}-{model_alias}"
    return validate_trial_id(re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip("-")[:MAX_TRIAL_ID_LENGTH])


def validate_trial_id(trial_id: str) -> str:
    if not TRIAL_ID_PATTERN.fullmatch(trial_id):
        raise ValueError("trial_id must match ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    if ".." in trial_id or "/" in trial_id or "\\" in trial_id:
        raise ValueError("trial_id must not contain path traversal or separators")
    return trial_id


def validate_numeric_bounds(timeout_seconds: int, max_output_bytes: int) -> None:
    if not MIN_TIMEOUT_SECONDS <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be between {MIN_TIMEOUT_SECONDS} and {MAX_TIMEOUT_SECONDS}")
    if not MIN_OUTPUT_BYTES <= max_output_bytes <= MAX_OUTPUT_BYTES:
        raise ValueError(f"max_output_bytes must be between {MIN_OUTPUT_BYTES} and {MAX_OUTPUT_BYTES}")


def resolve_artifact_root(repo_root: Path, artifact_root: Path) -> Path:
    if any(part == ".." for part in artifact_root.parts):
        raise ValueError("artifact_root must not contain '..'")
    root = (artifact_root if artifact_root.is_absolute() else repo_root / artifact_root).resolve()
    home = Path.home().resolve()
    if root == Path(root.anchor) or root == home:
        raise ValueError("artifact_root must not be filesystem root or the user home directory")
    return root


def artifact_dir_for(root: Path, trial_id: str) -> Path:
    trial = validate_trial_id(trial_id)
    artifact_dir = (root / trial).resolve()
    try:
        artifact_dir.relative_to(root)
    except ValueError as exc:
        raise ValueError("artifact_dir resolved outside artifact_root") from exc
    return artifact_dir


def assert_no_forbidden_agent_keys(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = re.sub(r"[-_]", "", str(key).lower())
            if any(forbidden in key_text for forbidden in FORBIDDEN_AGENT_KEYS):
                raise ValueError(f"episode contains forbidden agent-visible key at {path}.{key}")
            assert_no_forbidden_agent_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            assert_no_forbidden_agent_keys(item, f"{path}[{index}]")


def resolve_prompt_file(harnesses: Mapping[str, Any], prompt: str, repo_root: Path) -> Path:
    prompt_root = (repo_root / "harness" / "prompts").resolve()
    prompt_templates = harnesses.get("shared", {}).get("prompt_templates", {})
    if prompt in prompt_templates:
        configured_value = Path(prompt_templates[prompt])
        if configured_value.is_absolute() or ".." in configured_value.parts:
            raise ValueError("prompt template references must stay under harness/prompts")
        configured = (repo_root / configured_value).resolve()
        harness_relative = (repo_root / "harness" / configured_value).resolve()
        candidates = [configured, harness_relative]
    else:
        prompt_path = Path(prompt)
        if prompt_path.is_absolute() or ".." in prompt_path.parts:
            raise ValueError("prompt must be a key or path under harness/prompts")
        candidates = [(prompt_root / prompt_path).resolve()]
    for candidate in candidates:
        if candidate.is_file():
            try:
                candidate.relative_to(prompt_root)
            except ValueError as exc:
                raise ValueError("prompt must resolve under harness/prompts") from exc
            return candidate
    raise FileNotFoundError(f"prompt not found under harness/prompts: {prompt}")


def render_prompt(common_file: Path, selected_file: Path, episode: Mapping[str, Any]) -> str:
    common_text = common_file.read_text(encoding="utf-8").rstrip()
    selected_text = selected_file.read_text(encoding="utf-8").rstrip()
    prompt_sections = [common_text]
    if selected_file.resolve() != common_file.resolve():
        prompt_sections.append(selected_text)
    public_episode = yaml.safe_dump(dict(episode), sort_keys=False, allow_unicode=True)
    return (
        "\n\n".join(prompt_sections)
        + "\n\n"
        "Public episode contract follows. It is Agent-visible only and contains no hidden answer key.\n\n"
        "```yaml\n"
        f"{public_episode}"
        "```\n"
    )


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def append_jsonl(path: Path, value: Any) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def validate_prompt_text(prompt_text: str) -> None:
    encoded = prompt_text.encode("utf-8")
    if len(encoded) > MAX_PROMPT_BYTES:
        raise ValueError(f"prompt exceeds {MAX_PROMPT_BYTES} bytes")
    if redact_text(prompt_text, {}) != prompt_text:
        raise ValueError("prompt appears to contain a secret-like value")


def append_runtime_capability_prompt(
    prompt_text: str,
    env: Mapping[str, str],
) -> str:
    names = {
        "baseline_gate_token": "RESBENCH_BASELINE_GATE_TOKEN",
        "cleanup_handle": "RESBENCH_CLEANUP_HANDLE",
        "controller_token_ref": "RESBENCH_CHAOS_CONTROLLER_TOKEN_REF",
        "expected_controller_pod_uid": "RESBENCH_CHAOS_CONTROLLER_POD_UID",
        "target": "RESBENCH_AUTHORIZED_TARGET_JSON",
        "main_fault": "RESBENCH_MAIN_FAULT_JSON",
        "run_id": "RESBENCH_AUTHORIZED_RUN_ID",
    }
    present = {key: env.get(name, "") for key, name in names.items() if env.get(name)}
    if not present:
        return prompt_text
    missing = [name for key, name in names.items() if key not in present]
    if missing:
        raise ValueError(
            "runtime fault capability is incomplete: " + ", ".join(sorted(missing))
        )
    if len(present["baseline_gate_token"]) < 32:
        raise ValueError("runtime baseline capability is too short")
    try:
        target = json.loads(present["target"])
        main_fault = json.loads(present["main_fault"])
    except json.JSONDecodeError as exc:
        raise ValueError("runtime target or main fault is not valid JSON") from exc
    capability = {
        "target": target,
        "main_fault": main_fault,
        "run_id": present["run_id"],
        "cleanup_handle": present["cleanup_handle"],
        "baseline_gate_token": present["baseline_gate_token"],
        "controller_token_ref": present["controller_token_ref"],
        "expected_controller_pod_uid": present["expected_controller_pod_uid"],
    }
    return (
        prompt_text
        + "\n\nController-issued, single-attempt runtime capability follows. It is short-lived, "
        "scope-bound, and must be used only with chaos_control for this trial.\n\n"
        "```json\n"
        + json.dumps(capability, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n```\n"
    )


def validate_url_env(name: str, value: str) -> str | None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return f"{name} must be an explicit http(s) URL"
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return f"{name} must not contain userinfo, query, or fragment"
    return None


def runtime_env_status(
    env: Mapping[str, str],
    execute: bool,
    harness_name: str | None = None,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    status: dict[str, dict[str, Any]] = {}
    issues: list[str] = []
    for name in sorted(ALLOWED_RUNTIME_ENV):
        value = env.get(name, "")
        status[name] = {"present": bool(value)}
        oauth_codex = harness_name == "codex" and bool(env.get("RESBENCH_CODEX_AUTH_FILE"))
        oauth_optional = {"RESBENCH_LLM_BASE_URL", "RESBENCH_LLM_API_KEY"} if oauth_codex else set()
        if execute and not value and name not in OPTIONAL_RUNTIME_ENV | oauth_optional:
            issues.append(f"{name} is required for execute")
    for name in sorted(ALLOWED_RUNTIME_ENV):
        if name.endswith("_URL") and env.get(name):
            message = validate_url_env(name, env[name])
            if execute and message:
                issues.append(message)
    token = env.get("RESBENCH_MCP_TOKEN", "")
    if execute and token and (token != token.strip() or re.search(r"\s", token)):
        issues.append("RESBENCH_MCP_TOKEN must not contain whitespace or leading/trailing spaces")
    if execute and token and len(token) < MIN_MCP_TOKEN_LENGTH:
        issues.append(f"RESBENCH_MCP_TOKEN must be at least {MIN_MCP_TOKEN_LENGTH} characters")
    return status, issues


def render_dsh_contract(repo_root: Path, dsh_home: Path, env: Mapping[str, str], model_alias: str) -> None:
    source_dir = repo_root / "harness" / "deepseek-harness"
    dsh_home.mkdir(parents=True, exist_ok=True)
    settings = (source_dir / "settings.yaml.template").read_text(encoding="utf-8")
    base_url = env.get("RESBENCH_LLM_BASE_URL", "")
    api = "openai-completions"
    if model_alias == "claude-opus-5":
        api = "anthropic-messages"
        base_url = anthropic_base_url(base_url)
    settings = settings.replace("__RESBENCH_DSH_API__", api)
    settings = settings.replace("__RESBENCH_DSH_BASE_URL__", base_url)
    settings = settings.replace("__RESBENCH_MODEL_ALIAS__", model_alias)
    (dsh_home / "settings.yaml").write_text(settings, encoding="utf-8")
    shutil.copyfile(source_dir / "mcp.cordis.patch.yml", dsh_home / "cordis.patch.yml")


def render_codex_config(repo_root: Path, codex_home: Path, env: Mapping[str, str]) -> Path:
    template_name = (
        "config.oauth.toml.template"
        if env.get("RESBENCH_CODEX_AUTH_FILE")
        else "config.toml.template"
    )
    template = (repo_root / "harness" / "codex" / template_name).read_text(
        encoding="utf-8"
    )
    rendered = template.replace("__RESBENCH_LLM_BASE_URL__", env.get("RESBENCH_LLM_BASE_URL", ""))
    for placeholder, env_name in MCP_URL_ENV.items():
        rendered = rendered.replace(placeholder, env.get(env_name, ""))
    token = env.get("RESBENCH_MCP_TOKEN", "")
    if token and token in rendered:
        raise ValueError("codex config rendering attempted to persist the MCP token")
    codex_home.mkdir(parents=True, exist_ok=True)
    path = codex_home / "config.toml"
    path.write_text(rendered, encoding="utf-8")
    return path


def copy_codex_auth(codex_home: Path, env: Mapping[str, str]) -> bool:
    raw = env.get("RESBENCH_CODEX_AUTH_FILE", "")
    if not raw:
        return False
    source = Path(raw).expanduser().resolve()
    if not source.is_file() or source.is_symlink():
        raise ValueError("RESBENCH_CODEX_AUTH_FILE must be a regular file")
    mode = stat.S_IMODE(source.stat().st_mode)
    if mode & 0o077:
        raise ValueError("RESBENCH_CODEX_AUTH_FILE must not be group/world accessible")
    codex_home.mkdir(parents=True, exist_ok=True)
    destination = codex_home / "auth.json"
    shutil.copyfile(source, destination)
    os.chmod(destination, 0o600)
    return True


def render_claude_config(repo_root: Path, claude_home: Path) -> Path:
    source = repo_root / "harness" / "claude-code" / "mcp.json.template"
    claude_home.mkdir(parents=True, exist_ok=True)
    path = claude_home / "mcp.json"
    shutil.copyfile(source, path)
    return path


def anthropic_base_url(value: str) -> str:
    normalized = value.rstrip("/")
    return normalized.removesuffix("/v1")


def child_env_for_harness(harness_name: str, parent_env: Mapping[str, str], homes: Mapping[str, str]) -> dict[str, str]:
    child: dict[str, str] = {}
    if harness_name == "codex":
        child = {
            "OPENAI_BASE_URL": parent_env.get("RESBENCH_LLM_BASE_URL", ""),
            "OPENAI_API_KEY": parent_env.get("RESBENCH_LLM_API_KEY", ""),
            "RESBENCH_MCP_TOKEN": parent_env.get("RESBENCH_MCP_TOKEN", ""),
        }
    elif harness_name == "claude-code":
        child = {
            "ANTHROPIC_BASE_URL": anthropic_base_url(parent_env.get("RESBENCH_LLM_BASE_URL", "")),
            "ANTHROPIC_API_KEY": parent_env.get("RESBENCH_LLM_API_KEY", ""),
            "ANTHROPIC_AUTH_TOKEN": parent_env.get("RESBENCH_LLM_API_KEY", ""),
            "RESBENCH_K8S_MCP_URL": parent_env.get("RESBENCH_K8S_MCP_URL", ""),
            "RESBENCH_TELEMETRY_MCP_URL": parent_env.get("RESBENCH_TELEMETRY_MCP_URL", ""),
            "RESBENCH_SOURCE_MCP_URL": parent_env.get("RESBENCH_SOURCE_MCP_URL", ""),
            "RESBENCH_CHAOS_CONTROL_MCP_URL": parent_env.get("RESBENCH_CHAOS_CONTROL_MCP_URL", ""),
            "RESBENCH_MCP_TOKEN": parent_env.get("RESBENCH_MCP_TOKEN", ""),
        }
    elif harness_name == "deepseek-harness":
        child = {key: value for key, value in parent_env.items() if key in ALLOWED_RUNTIME_ENV and value}
        child["DSH_TOOLS_MODE"] = "native"
    else:
        child = {key: value for key, value in parent_env.items() if key in ALLOWED_RUNTIME_ENV and value}
    child = {key: value for key, value in child.items() if value}
    child.update(homes)
    home_key = {
        "codex": "CODEX_HOME",
        "claude-code": "CLAUDE_CONFIG_DIR",
        "deepseek-harness": "DSH_HOME",
    }.get(harness_name)
    if home_key and homes.get(home_key):
        child["HOME"] = homes[home_key]
    child["USER"] = "resbench"
    child["LOGNAME"] = "resbench"
    child["PATH"] = SAFE_PATH
    return child


def _path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


def validate_formal_harness_runtime(
    harness_name: str,
    argv: Sequence[str],
    child_env: Mapping[str, str],
    homes: Mapping[str, str],
    paths: Mapping[str, Path],
    trial_root: Path,
    expected_mcp_token: str,
) -> None:
    """Fail closed when a formal Trial could inherit a global or privileged runtime."""

    expected_command = FORMAL_HARNESS_COMMANDS.get(harness_name)
    if not expected_command:
        raise ValueError(f"formal runtime validation is unavailable for harness: {harness_name}")
    if not argv:
        raise ValueError("formal harness argv is empty")
    arguments = list(argv)
    command = Path(argv[0])
    if command.name != expected_command or (not command.is_absolute() and argv[0] != expected_command):
        raise ValueError(
            f"formal harness command must resolve from the trusted name {expected_command!r}"
        )

    unsafe_arguments = sorted(
        marker
        for marker in FORBIDDEN_FORMAL_ARGUMENTS
        if any(marker in argument for argument in argv[1:])
    )
    if unsafe_arguments:
        raise ValueError(
            "formal harness invocation contains forbidden permission bypass arguments: "
            + ", ".join(unsafe_arguments)
        )

    leaked_env = sorted(FORBIDDEN_FORMAL_ENV.intersection(child_env))
    if leaked_env:
        raise ValueError(
            "formal harness environment contains forbidden host capability variables: "
            + ", ".join(leaked_env)
        )
    if child_env.get("PATH") != SAFE_PATH:
        raise ValueError("formal harness PATH does not match SAFE_PATH")
    if not expected_mcp_token or child_env.get("RESBENCH_MCP_TOKEN") != expected_mcp_token:
        raise ValueError("formal harness did not receive the current Trial MCP token")

    home_key = {
        "codex": "CODEX_HOME",
        "claude-code": "CLAUDE_CONFIG_DIR",
        "deepseek-harness": "DSH_HOME",
    }[harness_name]
    expected_home = homes.get(home_key, "")
    if not expected_home or child_env.get(home_key) != expected_home:
        raise ValueError(f"formal harness did not receive its Trial-local {home_key}")
    if child_env.get("HOME") != expected_home:
        raise ValueError(f"formal harness HOME must equal its Trial-local {home_key}")
    home_path = Path(expected_home)
    if not home_path.is_dir() or not _path_within(home_path, trial_root):
        raise ValueError(f"formal harness {home_key} is outside the Trial-private directory")

    if harness_name == "codex":
        if (
            "--sandbox" not in arguments
            or arguments[arguments.index("--sandbox") + 1 :][:1] != ["read-only"]
        ):
            raise ValueError("formal Codex invocation must use the read-only sandbox")
        codex_config = home_path / "config.toml"
        if not codex_config.is_file() or not _path_within(codex_config, trial_root):
            raise ValueError("formal Codex config is not Trial-local")
        config_text = codex_config.read_text(encoding="utf-8")
        required_settings = (
            'approval_policy = "never"',
            "shell_tool = false",
            "unified_exec = false",
            "browser_use = false",
            "computer_use = false",
        )
        if any(setting not in config_text for setting in required_settings):
            raise ValueError("formal Codex config does not disable non-MCP execution tools")
    elif harness_name == "claude-code":
        try:
            config_index = arguments.index("--mcp-config") + 1
            configured_path = Path(arguments[config_index])
        except (ValueError, IndexError) as exc:
            raise ValueError("formal Claude invocation requires a Trial-local --mcp-config") from exc
        expected_config = paths.get("mcp_config_file")
        if (
            expected_config is None
            or configured_path.resolve(strict=False) != expected_config.resolve(strict=False)
            or not configured_path.is_file()
            or not _path_within(configured_path, trial_root)
        ):
            raise ValueError("formal Claude MCP config is not the rendered Trial-local config")
        if "--strict-mcp-config" not in argv:
            raise ValueError("formal Claude invocation must use --strict-mcp-config")
        try:
            tools_value = arguments[arguments.index("--tools") + 1]
            allowed_tools = arguments[arguments.index("--allowedTools") + 1]
        except (ValueError, IndexError) as exc:
            raise ValueError("formal Claude invocation must declare its MCP-only tool surface") from exc
        if tools_value or not allowed_tools:
            raise ValueError("formal Claude invocation must disable built-in tools")
        if any(not item.startswith("mcp__") for item in allowed_tools.split(",")):
            raise ValueError("formal Claude invocation allows a non-MCP tool")
    elif harness_name == "deepseek-harness":
        if child_env.get("DSH_TOOLS_MODE") != "native":
            raise ValueError("formal DeepSeek Harness must use the Trial-local native tool graph")
        cordis_path = home_path / "cordis.patch.yml"
        if not cordis_path.is_file() or not _path_within(cordis_path, trial_root):
            raise ValueError("formal DeepSeek Harness Cordis graph is not Trial-local")
        cordis_text = cordis_path.read_text(encoding="utf-8")
        required_disabled_tools = (
            "- id: code-runtime\n  disabled: true",
            "- id: tool-bash\n  disabled: true",
            "- id: tool-fs\n  disabled: true",
            "- id: tool-web\n  disabled: true",
        )
        if any(setting not in cordis_text for setting in required_disabled_tools):
            raise ValueError("formal DeepSeek Harness graph does not disable non-MCP tools")


def resolve_formal_harness_command(harness_name: str, command: str) -> str:
    """Resolve one formal harness through SAFE_PATH and reject mutable or polluted launchers."""

    expected_command = FORMAL_HARNESS_COMMANDS.get(harness_name)
    if command != expected_command:
        raise ValueError(
            f"formal harness command must be the trusted basename {expected_command!r}, got {command!r}"
        )
    if os.geteuid() == 0:
        raise ValueError("formal harness runner must execute as a non-root OS user")
    resolved = shutil.which(command, path=SAFE_PATH)
    if not resolved:
        raise ValueError(f"harness command is not available on SAFE_PATH: {command}")
    launcher = Path(resolved)
    safe_roots = [Path(item) for item in SAFE_PATH.split(os.pathsep) if item]
    if not launcher.is_absolute() or not any(_path_within(launcher, root) for root in safe_roots):
        raise ValueError(f"formal harness launcher is outside SAFE_PATH: {launcher}")
    try:
        launcher_lstat = launcher.lstat()
        target_stat = launcher.stat()
    except OSError as exc:
        raise ValueError(f"formal harness launcher cannot be inspected: {launcher}") from exc
    if not stat.S_ISREG(target_stat.st_mode) or not os.access(launcher, os.X_OK):
        raise ValueError(f"formal harness launcher is not an executable regular file: {launcher}")
    if launcher_lstat.st_uid != 0 or target_stat.st_uid != 0:
        raise ValueError(f"formal harness launcher must be root-owned: {launcher}")
    if (
        (not stat.S_ISLNK(launcher_lstat.st_mode) and launcher_lstat.st_mode & 0o022)
        or target_stat.st_mode & 0o022
    ):
        raise ValueError(f"formal harness launcher must not be group/world writable: {launcher}")
    try:
        prefix = launcher.read_bytes()[:131_072]
    except OSError as exc:
        raise ValueError(f"formal harness launcher cannot be read: {launcher}") from exc
    if prefix.startswith(b"#!"):
        script_text = prefix.decode("utf-8", errors="ignore")
        polluted = sorted(
            marker for marker in FORBIDDEN_LAUNCHER_MARKERS if marker in script_text
        )
        if polluted:
            raise ValueError(
                "formal harness launcher script contains global or privileged "
                f"configuration markers: {launcher}"
            )
    return launcher.as_posix()


def subprocess_runner(argv: Sequence[str], stdin: bytes, env: Mapping[str, str], timeout_seconds: int) -> CommandResult:
    try:
        completed = subprocess.run(
            list(argv),
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
            shell=False,
            env=dict(env),
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            returncode=-1,
            stdout=exc.stdout or b"",
            stderr=exc.stderr or b"",
            timed_out=True,
        )
    return CommandResult(returncode=int(completed.returncode), stdout=completed.stdout, stderr=completed.stderr)


def subprocess_streaming_runner(
    argv: Sequence[str],
    stdin: bytes,
    env: Mapping[str, str],
    timeout_seconds: int,
    stdout_line_observer: Callable[[bytes], Any],
    cancel_requested: Callable[[], bool] | None = None,
    *,
    resume_argv_builder: ResumeArgvBuilder | None = None,
    session_id_provider: Callable[[], str | None] | None = None,
    turn_complete_observer: Callable[[Mapping[str, Any]], Any] | None = None,
    interaction_mode: str = "guided",
    transcript_path: Path | None = None,
    redactor: Callable[[Any], Any] | None = None,
) -> CommandResult:
    """Run a harness and forward stdout JSONL before process completion."""

    session_result = HarnessSession(
        argv=argv,
        stdin=stdin,
        env=env,
        timeout_seconds=timeout_seconds,
        stdout_line_observer=stdout_line_observer,
        cancel_requested=cancel_requested,
        resume_argv_builder=resume_argv_builder,
        session_id_provider=session_id_provider,
        turn_complete_observer=turn_complete_observer,
        interaction_mode=interaction_mode,
        transcript_path=transcript_path,
        redactor=redactor,
    ).start().wait()
    return CommandResult(
        returncode=session_result.returncode,
        stdout=session_result.stdout,
        stderr=session_result.stderr,
        timed_out=session_result.timed_out,
        cancelled=session_result.cancelled,
    )


def truncate_output(data: bytes, max_output_bytes: int) -> tuple[bytes, bool]:
    if len(data) <= max_output_bytes:
        return data, False
    return data[:max_output_bytes], True


def write_payload(artifact_dir: Path, name: str, payload: bytes | str, env: Mapping[str, str], max_output_bytes: int) -> tuple[str, bool]:
    data = payload if isinstance(payload, bytes) else payload.encode("utf-8")
    truncated, was_truncated = truncate_output(data, max_output_bytes)
    path = artifact_dir / name
    path.write_text(redact_text(truncated, env), encoding="utf-8")
    return path.relative_to(artifact_dir).as_posix(), was_truncated


def extract_json_objects(text: str) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    seen_nodes: set[int] = set()

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            identity = id(value)
            if identity in seen_nodes:
                return
            seen_nodes.add(identity)
            objects.append(value)
            for key in ("structured_output", "result", "text", "content", "message", "item"):
                if key in value:
                    collect(value[key])
            for item in value.values():
                if isinstance(item, (dict, list)):
                    collect(item)
        elif isinstance(value, list):
            identity = id(value)
            if identity in seen_nodes:
                return
            seen_nodes.add(identity)
            for item in value:
                collect(item)
        elif isinstance(value, str):
            for candidate in string_json_candidates(value):
                collect(candidate)

    stripped = text.strip()
    parsed_whole = False
    if stripped:
        try:
            parsed = json.loads(stripped)
            collect(parsed)
            parsed_whole = True
        except json.JSONDecodeError:
            pass
        if not parsed_whole:
            for candidate in string_json_candidates(stripped):
                collect(candidate)
    if not parsed_whole:
        for line in text.splitlines():
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            collect(parsed)
    return objects


def extract_provider_reported_model(text: str) -> str | None:
    """Read model identity only from known CLI/runtime metadata events."""

    metadata_markers = {
        "assistant",
        "message",
        "message_start",
        "response.completed",
        "response.created",
        "session",
        "system",
        "thread.started",
        "turn.started",
    }
    for item in extract_json_objects(text):
        marker = str(item.get("type") or item.get("event") or "")
        if marker in metadata_markers:
            candidate = item.get("provider_reported_model") or item.get("model")
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        usage = item.get("modelUsage")
        if isinstance(usage, Mapping) and len(usage) == 1:
            candidate = next(iter(usage))
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return None


def string_json_candidates(value: str) -> list[Any]:
    candidates = [value.strip()]
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", value, flags=re.DOTALL)
    candidates.extend(item.strip() for item in fenced)
    parsed: list[Any] = []
    for candidate in candidates:
        if not candidate.startswith("{"):
            continue
        try:
            parsed.append(json.loads(candidate))
        except json.JSONDecodeError:
            continue
    return parsed


def trace_kind_from_event(event: Mapping[str, Any]) -> str | None:
    marker = str(event.get("type") or event.get("event") or event.get("kind") or "")
    if marker == "mcp_tool_call":
        completed = str(event.get("status") or "").lower() in {"completed", "failed"}
        has_result = any(
            event.get(key) is not None for key in ("result", "error", "output")
        )
        return "tool_result" if completed or has_result else "tool_call"
    if marker in {"tool_call", "tool_use", "function_call"}:
        return "tool_call"
    if marker in {"tool_result", "function_result"}:
        return "tool_result"
    if marker in {"message", "agent_message", "assistant"}:
        return "agent_message"
    return None


def event_tool_name(event: Mapping[str, Any]) -> str | None:
    tool = event.get("tool") or event.get("name")
    server = event.get("server") or event.get("server_name")
    if isinstance(tool, str) and tool:
        if isinstance(server, str) and server and not tool.startswith(("mcp__", f"{server}.")):
            return f"{server}.{tool}"
        return tool
    return None


def forbidden_non_mcp_tool_event(event: Mapping[str, Any]) -> bool:
    marker = str(event.get("type") or event.get("event") or event.get("kind") or "")
    if marker in {
        "command_execution",
        "file_change",
        "apply_patch",
        "web_search",
        "computer_use",
        "browser_use",
        "subagent_call",
    }:
        return True
    if marker in {"tool_call", "tool_use", "function_call", "mcp_tool_call"}:
        return not allowed_mcp_tool_event(event)
    return False


def allowed_mcp_tool_event(event: Mapping[str, Any]) -> bool:
    marker = str(event.get("type") or event.get("event") or event.get("kind") or "")
    raw_tool = event.get("tool") or event.get("name")
    raw_server = event.get("server") or event.get("server_name")
    if marker == "mcp_tool_call":
        return (
            isinstance(raw_server, str)
            and isinstance(raw_tool, str)
            and raw_tool in ALLOWED_MCP_TOOLS.get(raw_server, set())
        )
    name = event_tool_name(event)
    if not name:
        return False
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) != 3:
            return False
        _, server, tool = parts
        return tool in ALLOWED_MCP_TOOLS.get(server, set())
    if "." in name:
        server, tool = name.split(".", 1)
        return tool in ALLOWED_MCP_TOOLS.get(server, set())
    return False


def build_argv(
    harness_name: str,
    harness: Mapping[str, Any],
    model_alias: str,
    prompt_text: str,
    paths: Mapping[str, Path],
) -> tuple[list[str], bytes, str | None]:
    entrypoint = harness.get("entrypoint", {})
    command = entrypoint.get("command")
    if not isinstance(command, str) or not command:
        raise ValueError(f"harness {harness_name} does not define an entrypoint command")
    transport = entrypoint.get("prompt_transport")
    args: list[str] = []
    for raw in entrypoint.get("args", []):
        item = str(raw)
        item = item.replace("{{model_alias}}", model_alias)
        item = item.replace("{{mcp_config_file}}", paths.get("mcp_config_file", Path("")).as_posix())
        item = item.replace("{{output_schema_file}}", paths.get("output_schema_file", Path("")).as_posix())
        item = item.replace("{{prompt_text}}", prompt_text)
        args.append(item)
    if harness_name == "codex":
        args = [item for item in args if item != "--ephemeral"]
    elif harness_name == "claude-code":
        args = [item for item in args if item != "--no-session-persistence"]
    if harness_name == "codex" and paths.get("codex_last_message_file"):
        output_args = ["--output-last-message", paths["codex_last_message_file"].as_posix()]
        if args and args[-1] == "-":
            args = [*args[:-1], *output_args, "-"]
        else:
            args.extend(output_args)
    argv = [command, *args]
    if transport == "stdin":
        return argv, prompt_text.encode("utf-8"), None
    if harness_name == "deepseek-harness":
        return argv, b"", None
    return argv, b"", f"unsupported prompt transport for execute mode: {transport}"


def artifact_argv(
    argv: Sequence[str],
    harness_name: str,
    *,
    repo_root: Path,
    temp_root: Path,
    artifact_dir: Path,
) -> list[str]:
    values = list(argv)
    if harness_name == "deepseek-harness" and values:
        values[-1] = "<prompt omitted from artifacts>"
    sanitized: list[str] = []
    roots = (
        (temp_root.resolve(), "<trial-temp>"),
        (artifact_dir.resolve(), "<trial-artifact>"),
        (repo_root.resolve(), "<repo>"),
    )
    for index, value in enumerate(values):
        candidate = Path(value)
        if not candidate.is_absolute():
            sanitized.append(value)
            continue
        resolved = candidate.resolve(strict=False)
        replacement = ""
        for root, marker in roots:
            try:
                relative = resolved.relative_to(root)
            except ValueError:
                continue
            replacement = marker if relative == Path(".") else f"{marker}/{relative.as_posix()}"
            break
        if not replacement:
            replacement = f"<absolute-command:{candidate.name}>" if index == 0 else "<absolute-path>"
        sanitized.append(replacement)
    return sanitized


def build_codex_resume_argv_builder(
    *,
    command: str,
    model_alias: str,
    paths: Mapping[str, Path],
    candidate_files: list[Path],
) -> ResumeArgvBuilder:
    def build(session_id: str, turn_index: int) -> Sequence[str]:
        output_path = paths["codex_last_message_file"].with_name(
            f"codex-last-message-resume-{turn_index:02d}.json"
        )
        candidate_files.append(output_path)
        return [
            command,
            "exec",
            "--sandbox",
            "read-only",
            "resume",
            "--model",
            model_alias,
            "--skip-git-repo-check",
            "--json",
            "--output-schema",
            paths["output_schema_file"].as_posix(),
            "--output-last-message",
            output_path.as_posix(),
            session_id,
            "-",
        ]

    return build


def build_claude_resume_argv_builder(
    *,
    command: str,
    model_alias: str,
    paths: Mapping[str, Path],
) -> ResumeArgvBuilder:
    def build(session_id: str, turn_index: int) -> Sequence[str]:
        del turn_index
        return [
            command,
            "--print",
            "--verbose",
            "--bare",
            "--output-format",
            "stream-json",
            "--model",
            model_alias,
            "--mcp-config",
            paths["mcp_config_file"].as_posix(),
            "--strict-mcp-config",
            "--tools",
            "",
            "--allowedTools",
            "mcp__k8s_ro,mcp__telemetry_ro,mcp__source_ro,mcp__chaos_control",
            "--resume",
            session_id,
        ]

    return build


def validate_final_agent_result(
    stdout: bytes,
    candidate_files: Sequence[Path],
    artifact_dir: Path,
    output_schema: Mapping[str, Any],
    env: Mapping[str, str],
) -> tuple[str, str | None]:
    candidates: list[dict[str, Any]] = []
    for path in candidate_files:
        if path.is_file():
            candidates.extend(extract_json_objects(redact_text(path.read_bytes(), env)))
    candidates.extend(extract_json_objects(redact_text(stdout, env)))
    for candidate in reversed(candidates):
        try:
            jsonschema.validate(candidate, output_schema)
        except jsonschema.ValidationError:
            continue
        ref = "agent-result.json"
        write_json(artifact_dir / ref, redact_json(candidate, env))
        return ref, None
    return "", "agent output did not contain a JSON object matching agent-result.schema.json"


def capture_dsh_session_trace(
    dsh_home: Path,
    artifact_dir: Path,
    env: Mapping[str, str],
    events: list[dict[str, Any]],
) -> list[str]:
    """Export DSH Trial-local compressed sessions before the ephemeral home is deleted."""
    references: list[str] = []
    zstd = shutil.which("zstd")
    for index, source in enumerate(sorted(dsh_home.rglob("session.jsonl.zstd"))):
        raw_name = f"dsh-session-{index:02d}.jsonl.zstd"
        raw_path = artifact_dir / raw_name
        shutil.copyfile(source, raw_path)
        references.append(raw_name)
        if not zstd:
            continue
        completed = subprocess.run(
            [zstd, "-dc", str(source)],
            check=False,
            capture_output=True,
        )
        if completed.returncode:
            continue
        text = redact_text(completed.stdout, env)
        jsonl_name = f"dsh-session-{index:02d}.jsonl"
        (artifact_dir / jsonl_name).write_text(text, encoding="utf-8")
        references.append(jsonl_name)
        for event_index, line in enumerate(text.splitlines()):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            event_type = str(value.get("type") or "")
            if "tool" not in event_type and "mcp" not in event_type:
                continue
            ref = f"dsh-session-{index:02d}-event-{event_index:04d}.json"
            write_json(artifact_dir / ref, value)
            kind = "tool_result" if any(
                marker in event_type for marker in ("result", "response", "end")
            ) else "tool_call"
            events.append(
                {
                    "ts": utc_now(),
                    "kind": kind,
                    "payload_ref": ref,
                    "redacted": True,
                }
            )
    return references


def run_trial(
    repo_root: Path,
    harness_name: str,
    model_alias: str,
    prompt_ref: str = DEFAULT_PROMPT_KEY,
    episode_file: Path = DEFAULT_EPISODE,
    execute: bool = False,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    parent_env: Mapping[str, str] | None = None,
    runner: Runner | None = None,
    event_observer: EventObserver | None = None,
    trial_id: str | None = None,
    prompt_text_override: str | None = None,
    require_structured_result: bool = True,
    enforce_formal_runtime: bool = True,
    cancel_requested: Callable[[], bool] | None = None,
    interaction_mode: str = "guided",
) -> dict[str, Any]:
    env = dict(parent_env if parent_env is not None else os.environ)
    repo = repo_root.resolve()
    validate_numeric_bounds(timeout_seconds, max_output_bytes)
    harnesses = load_yaml(repo / DEFAULT_HARNESSES_CONFIG)
    models = load_yaml(repo / DEFAULT_MODELS_CONFIG)
    output_schema = load_json(repo / DEFAULT_OUTPUT_SCHEMA)
    run_trace_schema = load_json(repo / DEFAULT_RUN_TRACE_SCHEMA)
    episode_schema = load_json(repo / DEFAULT_EPISODE_SCHEMA)
    episode_path = episode_file if episode_file.is_absolute() else repo / episode_file
    episode = load_yaml(episode_path)
    assert_no_forbidden_agent_keys(episode)
    jsonschema.validate(episode, episode_schema)

    registry = harnesses.get("harnesses", {})
    model_registry = models.get("models", {})
    if harness_name not in registry:
        raise ValueError(f"unknown harness: {harness_name}")
    if harness_name == "bladeai":
        raise ValueError("bladeai trials use the dedicated BladeAI adapter, not run_harness_trial.py")
    if model_alias not in model_registry:
        raise ValueError(f"unknown model alias: {model_alias}")

    common_prompt_file = resolve_prompt_file(harnesses, "common_task", repo)
    prompt_file = resolve_prompt_file(harnesses, prompt_ref, repo)
    prompt_text = (
        str(prompt_text_override)
        if prompt_text_override is not None
        else render_prompt(common_prompt_file, prompt_file, episode)
    )
    validate_prompt_text(prompt_text)
    if prompt_text_override is None:
        prompt_text = append_runtime_capability_prompt(prompt_text, env)
        trace_prompt_ref = prompt_file.relative_to(repo).as_posix()
    else:
        trace_prompt_ref = "inline-sha256:" + hashlib.sha256(
            prompt_text.encode("utf-8")
        ).hexdigest()
    episode_id = str(episode.get("episode_id") or "unknown-episode")
    trial = validate_trial_id(trial_id) if trial_id else make_trial_id(harness_name, episode_id, model_alias)
    resolved_artifact_root = resolve_artifact_root(repo, artifact_root)
    artifact_dir = artifact_dir_for(resolved_artifact_root, trial)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    events_jsonl = artifact_dir / "events.jsonl"
    session_events_jsonl = artifact_dir / "session-events.jsonl"
    env_status, env_issues = runtime_env_status(env, execute, harness_name)
    if execute and env_issues:
        raise ValueError("; ".join(env_issues))

    temp_root = Path(tempfile.mkdtemp(prefix=f"{trial}-", dir=artifact_dir))
    codex_home = temp_root / "codex-home"
    claude_home = temp_root / "claude-config"
    dsh_home = temp_root / "dsh-home"
    codex_template = repo / "harness" / "codex" / (
        "config.oauth.toml.template"
        if env.get("RESBENCH_CODEX_AUTH_FILE")
        else "config.toml.template"
    )
    claude_template = repo / "harness" / "claude-code" / "mcp.json.template"
    dsh_settings = repo / "harness" / "deepseek-harness" / "settings.yaml.template"
    dsh_cordis = repo / "harness" / "deepseek-harness" / "mcp.cordis.patch.yml"

    started_at = utc_now()
    events: list[dict[str, Any]] = [
        {
            "ts": started_at,
            "kind": "controller_gate",
            "summary": "trial prepared; hidden evaluator data is not included in the agent prompt",
        }
    ]

    status = "aborted_by_controller"
    error: str | None = None
    agent_ref = ""
    result: CommandResult | None = None
    homes_deleted = False
    native_session_refs: list[str] = []

    try:
        paths: dict[str, Path] = {
            "output_schema_file": repo / DEFAULT_OUTPUT_SCHEMA,
            "codex_last_message_file": temp_root / "codex-last-message.json",
        }
        codex_result_files = [paths["codex_last_message_file"]]
        render_codex_config(repo, codex_home, env)
        codex_auth_copied = copy_codex_auth(codex_home, env)
        paths["mcp_config_file"] = render_claude_config(repo, claude_home)
        render_dsh_contract(repo, dsh_home, env, model_alias)

        homes = {
            "CODEX_HOME": codex_home.as_posix(),
            "CLAUDE_CONFIG_DIR": claude_home.as_posix(),
            "DSH_HOME": dsh_home.as_posix(),
        }
        child_env = child_env_for_harness(harness_name, env, homes)
        if not enforce_formal_runtime:
            child_env["PATH"] = env.get("RESBENCH_D0_NATIVE_PATH") or env.get(
                "PATH", SAFE_PATH
            )
        harness = registry[harness_name]
        argv, stdin, fail_closed_reason = build_argv(harness_name, harness, model_alias, prompt_text, paths)
        if execute and not fail_closed_reason and enforce_formal_runtime:
            validate_formal_harness_runtime(
                harness_name,
                argv,
                child_env,
                homes,
                paths,
                temp_root,
                env.get("RESBENCH_MCP_TOKEN", ""),
            )
            if runner is None:
                resolved_command = resolve_formal_harness_command(harness_name, argv[0])
                argv = [resolved_command, *argv[1:]]
        elif execute and not fail_closed_reason and runner is None:
            resolved_command = shutil.which(argv[0])
            if not resolved_command:
                raise ValueError(f"D0 native harness command is unavailable: {argv[0]}")
            argv = [resolved_command, *argv[1:]]
        planned = {
            "argv": artifact_argv(
                argv,
                harness_name,
                repo_root=repo,
                temp_root=temp_root,
                artifact_dir=artifact_dir,
            ),
            "stdinBytes": len(stdin),
            "envKeys": sorted(child_env.keys()),
            "runtimeEnv": env_status,
            "session": {
                "nativeResume": harness_name in {"codex", "claude-code"},
                "interactionMode": interaction_mode,
                "feedbackCategories": [
                    "FACT_EVENT",
                    "AUTH_CONFIRM",
                    "SEMANTIC_NUDGE",
                ],
            },
            "templates": {
                "codexConfigTemplateSha256": sha256_file(codex_template),
                "codexAuthMode": "chatgpt-oauth" if codex_auth_copied else "gateway-api-key",
                "claudeMcpTemplateSha256": sha256_file(claude_template),
                "dshSettingsTemplateSha256": sha256_file(dsh_settings),
                "dshCordisPatchSha256": sha256_file(dsh_cordis),
            },
            "homes": {
                "isolated": True,
                "retainedInArtifacts": False,
            },
        }
        write_json(artifact_dir / "planned.json", redact_json(planned, env))
        append_jsonl(events_jsonl, {"ts": started_at, "event": "prepared", "payload": redact_json(planned, env)})

        if not execute:
            error = "dry-run; agent process was not launched"
        elif fail_closed_reason:
            error = fail_closed_reason
            events.append({"ts": utc_now(), "kind": "error", "summary": error})
            append_jsonl(events_jsonl, {"ts": utc_now(), "event": "fail_closed", "error": redact_text(error, env)})
        else:
            if event_observer is not None and runner is not None:
                raise ValueError("event_observer requires the built-in streaming subprocess runner")
            append_jsonl(
                events_jsonl,
                {
                    "ts": utc_now(),
                    "event": "launch",
                    "argv": artifact_argv(
                        argv,
                        harness_name,
                        repo_root=repo,
                        temp_root=temp_root,
                        artifact_dir=artifact_dir,
                    ),
                },
            )
            if event_observer is None:
                result = (runner or subprocess_runner)(argv, stdin, child_env, timeout_seconds)
            else:
                observed_stream_events: list[dict[str, Any]] = []
                captured_session_id: str | None = None

                def observe_stdout_line(line: bytes) -> list[Any]:
                    nonlocal captured_session_id
                    feedbacks: list[Any] = []
                    for stream_event in extract_json_objects(redact_text(line, env)):
                        observed_stream_events.append(stream_event)
                        if captured_session_id is None:
                            captured_session_id = session_id_from_event(stream_event)
                        response = event_observer(redact_json(stream_event, env))
                        feedbacks.extend(structured_feedbacks_from_observer(response))
                    return feedbacks

                def observe_turn_complete(summary: Mapping[str, Any]) -> list[Any]:
                    response = event_observer(
                        {
                            "type": "native_turn_completed",
                            "summary": dict(summary),
                            "observed_event_count": len(observed_stream_events),
                        }
                    )
                    return structured_feedbacks_from_observer(response)

                resume_builder: ResumeArgvBuilder | None = None
                session_id_provider: Callable[[], str | None] | None = None
                if harness_name == "codex":
                    resume_builder = build_codex_resume_argv_builder(
                        command=argv[0],
                        model_alias=model_alias,
                        paths=paths,
                        candidate_files=codex_result_files,
                    )
                    session_id_provider = (
                        lambda: captured_session_id or discover_codex_session_id(codex_home)
                    )
                elif harness_name == "claude-code":
                    resume_builder = build_claude_resume_argv_builder(
                        command=argv[0],
                        model_alias=model_alias,
                        paths=paths,
                    )
                    session_id_provider = lambda: captured_session_id

                result = subprocess_streaming_runner(
                    argv,
                    stdin,
                    child_env,
                    timeout_seconds,
                    observe_stdout_line,
                    cancel_requested,
                    resume_argv_builder=resume_builder,
                    session_id_provider=session_id_provider,
                    turn_complete_observer=observe_turn_complete,
                    interaction_mode=interaction_mode,
                    transcript_path=session_events_jsonl,
                    redactor=lambda value: redact_json(value, env),
                )
            if session_events_jsonl.is_file():
                events.append(
                    {
                        "ts": utc_now(),
                        "kind": "controller_gate",
                        "summary": "harness session transcript captured",
                        "payload_ref": "session-events.jsonl",
                        "redacted": True,
                    }
                )
            stdout_ref, stdout_truncated = write_payload(artifact_dir, "stdout.txt", result.stdout, env, max_output_bytes)
            stderr_ref, stderr_truncated = write_payload(artifact_dir, "stderr.txt", result.stderr, env, max_output_bytes)
            if stdout_ref:
                events.append({"ts": utc_now(), "kind": "agent_message", "payload_ref": stdout_ref, "redacted": True})
            if stderr_ref and result.stderr:
                events.append({"ts": utc_now(), "kind": "error", "payload_ref": stderr_ref, "redacted": True})
            if stdout_truncated or stderr_truncated:
                events.append({"ts": utc_now(), "kind": "error", "summary": "agent output exceeded max_output_bytes and was truncated"})
            forbidden_tool_seen = False
            for index, item in enumerate(extract_json_objects(redact_text(result.stdout, env))):
                if forbidden_non_mcp_tool_event(item):
                    forbidden_tool_seen = True
                kind = trace_kind_from_event(item)
                if not kind:
                    continue
                ref = f"event-{index:04d}.json"
                write_json(artifact_dir / ref, redact_json(item, env))
                event: dict[str, Any] = {"ts": utc_now(), "kind": kind, "payload_ref": ref, "redacted": True}
                tool = event_tool_name(item)
                if tool:
                    event["tool"] = tool
                events.append(event)
            if forbidden_tool_seen:
                events.append(
                    {
                        "ts": utc_now(),
                        "kind": "error",
                        "summary": "harness emitted a non-MCP tool event outside the benchmark surface",
                    }
                )
            if result.timed_out:
                status = "timeout"
                error = "agent process exceeded timeout_seconds"
            elif result.cancelled:
                status = "aborted_by_controller"
                error = "agent process cancelled by D0 Controller deadline"
            elif result.returncode != 0:
                status = "failed"
                error = f"agent process exited with code {result.returncode}"
            elif forbidden_tool_seen:
                status = "failed"
                error = "harness exposed or used a non-MCP tool"
            else:
                if require_structured_result:
                    agent_ref, validation_error = validate_final_agent_result(
                        result.stdout,
                        codex_result_files,
                        artifact_dir,
                        output_schema,
                        env,
                    )
                    if validation_error:
                        status = "failed"
                        error = validation_error
                    else:
                        status = "completed"
                else:
                    status = "completed"
    finally:
        if harness_name == "deepseek-harness" and dsh_home.exists():
            native_session_refs = capture_dsh_session_trace(
                dsh_home, artifact_dir, env, events
            )
        if session_events_jsonl.is_file():
            native_session_refs.append("session-events.jsonl")
        shutil.rmtree(temp_root, ignore_errors=True)
        homes_deleted = not temp_root.exists()

    final_output: dict[str, Any] = {"status": status, "agent_report_ref": agent_ref}
    if error:
        final_output["error"] = redact_text(error, env)
    events.append({"ts": utc_now(), "kind": "controller_gate", "summary": f"trial homes deleted: {homes_deleted}"})
    trace = {
        "schema_version": "resiliencebenchmark.harness.run-trace/v1",
        "trial_id": trial,
        "episode_id": episode_id,
        "harness": harness_name,
        "model_alias": model_alias,
        "prompt_ref": trace_prompt_ref,
        "started_at": started_at,
        "finished_at": utc_now(),
        "events": events,
        "final_output": final_output,
    }
    jsonschema.validate(trace, run_trace_schema)
    write_json(artifact_dir / "run-trace.json", redact_json(trace, env))
    append_jsonl(events_jsonl, {"ts": utc_now(), "event": "final", "payload": redact_json(final_output, env)})

    return {
        "schemaVersion": "resiliencebenchmark.harness.trial-run/v1",
        "dryRun": not execute,
        "status": status,
        "trialId": trial,
        "artifactRef": trial,
        "runTraceRef": f"{trial}/run-trace.json",
        "eventsJsonlRef": f"{trial}/events.jsonl",
        "agentResultRef": f"{trial}/{agent_ref}" if agent_ref else "",
        "nativeSessionRefs": [f"{trial}/{value}" for value in native_session_refs],
        "promptSha256": hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
        "error": final_output.get("error", ""),
    }


def run_multi_level_episode(
    repo_root: Path,
    *,
    episode_file: Path,
    run_id: str,
    agent_id: str,
    trial_runner: Any,
    level_evaluator: Any,
    injector_factory: Any,
    trial_preparer: Any | None = None,
    trial_finalizer: Any | None = None,
    progression_store: Any | None = None,
    resume: bool = False,
    continue_after_failure: bool = False,
) -> dict[str, Any]:
    """Run a multi-level episode through live, event-emitting adapters.

    The generic CLI subprocess runner intentionally is not adapted here: it
    captures stdout only after process exit, which is too late for precise
    lifecycle or Nth-tool-call disturbances. Harness-specific streaming
    adapters must call ``emit_event`` while the Agent is running.
    """

    from progression.builder import validate_multi_level_episode
    from progression.controller import ProgressionController
    from progression.orchestrator import MultiLevelOrchestrator

    repo = repo_root.resolve()
    path = episode_file if episode_file.is_absolute() else repo / episode_file
    episode = load_yaml(path)
    jsonschema.validate(episode, load_json(repo / DEFAULT_MULTI_LEVEL_EPISODE_SCHEMA))
    validate_multi_level_episode(episode)
    agent_visible_task = episode.get("base_task", {}).get("agent_visible_task")
    if agent_visible_task is not None:
        assert_no_forbidden_agent_keys(agent_visible_task)
    controller = ProgressionController(
        episode,
        run_id=run_id,
        agent_id=agent_id,
        store=progression_store,
        resume=resume,
        continue_after_failure=continue_after_failure,
    )
    return MultiLevelOrchestrator(
        controller,
        trial_runner=trial_runner,
        level_evaluator=level_evaluator,
        injector_factory=injector_factory,
        trial_preparer=trial_preparer,
        trial_finalizer=trial_finalizer,
    ).run().as_dict()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--harness", required=True, choices=["claude-code", "codex", "deepseek-harness"])
    parser.add_argument("--model", required=True)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT_KEY)
    parser.add_argument("--episode", type=Path, default=DEFAULT_EPISODE)
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--execute", action="store_true", help="launch the harness; default is dry-run")
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--max-output-bytes", type=int, default=DEFAULT_MAX_OUTPUT_BYTES)
    args = parser.parse_args(argv)
    try:
        report = run_trial(
            repo_root=args.repo_root,
            harness_name=args.harness,
            model_alias=args.model,
            prompt_ref=args.prompt,
            episode_file=args.episode,
            execute=args.execute,
            artifact_root=args.artifact_root,
            timeout_seconds=args.timeout_seconds,
            max_output_bytes=args.max_output_bytes,
        )
    except Exception as exc:  # noqa: BLE001 - CLI should return structured redacted failure.
        report = {
            "schemaVersion": "resiliencebenchmark.harness.trial-run/v1",
            "status": "failed",
            "error": redact_text(str(exc), os.environ),
        }
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] in {"completed", "aborted_by_controller"} else 1


if __name__ == "__main__":
    sys.exit(main())
