"""Render only Agent-visible BladeAI task inputs and controlled endpoints."""

from __future__ import annotations

import json
import os
from pathlib import Path
from string import Template
from typing import Mapping

from mcp_servers.bladeai_k8s_proxy.service import ProxyConfig, proxy_kubeconfig
from scripts.run_harness_trial import child_env_for_harness, write_json


# BladeAI starts several authenticated SSE clients in one worker.  The old
# 30-second SDK default was too narrow for the old cluster under transient I/O
# pressure; keep the wider wait local to BladeAI so other Harnesses retain
# their own startup contracts.
BLADEAI_MCP_CONNECT_TIMEOUT_SECONDS = 120


def prepare_bladeai_launch(
    *, repo_root: Path, trial_root: Path, trial_id: str, namespace: str,
    prompt: str, model_alias: str, environment: Mapping[str, str],
    proxy_config: ProxyConfig, python_executable: str,
    qualification_fault: Mapping[str, object] | None = None,
) -> tuple[list[str], bytes, dict[str, str]]:
    """Use current SDK HOME loader and never supply a preselected experiment."""
    agent_home = trial_root / "bladeai-home"
    config_root = agent_home / ".blade-ai"
    config_root.mkdir(mode=0o700, parents=True)
    kubeconfig = agent_home / "proxy.kubeconfig"
    write_json(kubeconfig, proxy_kubeconfig(proxy_config))
    kubeconfig.chmod(0o600)
    request_path = agent_home / "task.json"
    request = {
        "mode": "task", "trial_id": trial_id, "intent": prompt,
        "namespace": namespace, "kubeconfig": str(kubeconfig),
    }
    if qualification_fault is not None:
        request["qualification_fault"] = dict(qualification_fault)
    write_json(request_path, request)
    request_path.chmod(0o600)

    template = json.loads((repo_root / "harness/bladeai/mcp.json.template").read_text())
    servers = {}
    for name, entry in template["mcpServers"].items():
        if entry.get("enabled") is False:
            continue
        # Only services provisioned for this case are exposed. Missing optional
        # Coroot/Mesh endpoints do not become broken placeholder URLs.
        variables = Template(entry["url"]).get_identifiers()
        if any(not environment.get(key) for key in variables):
            continue
        rendered = json.loads(Template(json.dumps(entry)).substitute(environment))
        servers[name] = rendered
    if not {"harness_channel", "k8s_ro", "telemetry_ro", "source_ro"} <= set(servers):
        raise ValueError("BladeAI task requires provisioned Harness and read-only discovery endpoints")
    if not environment.get("RESBENCH_BLADEAI_CHAOS_CONTROL_MCP_SSE_URL"):
        raise ValueError("BladeAI controlled blade shim requires a chaos_control endpoint")
    mcp_path = config_root / "mcp.json"
    write_json(mcp_path, {"mcpServers": servers})
    mcp_path.chmod(0o600)
    python_overlay = _write_agent_python_overlay(repo_root=repo_root, trial_root=trial_root)
    child = child_env_for_harness("bladeai", environment, {})
    child.update({key: value for key, value in environment.items()
                  if key.startswith("RESBENCH_BLADEAI_") and key.endswith("_MCP_SSE_URL") and value})
    child.update({
        "HOME": str(agent_home), "BLADE_AI_MEMORY_DIR": str(agent_home / "memory"),
        "BLADE_AI_LLM_API_KEY": environment.get("RESBENCH_LLM_API_KEY", ""),
        "BLADE_AI_API_BASE_URL": environment.get("RESBENCH_LLM_BASE_URL", ""),
        "BLADE_AI_MODEL_NAME": model_alias,
        "BLADE_AI_KUBECONFIG_PATH": str(kubeconfig),
        "BLADE_AI_BLADE_PATH": str(repo_root / "harness/bladeai/blade-shim/blade"),
        "BLADE_AI_KUBECTL_PATH": str(repo_root / "harness/bladeai/kubectl-shim/kubectl"),
        "BLADE_AI_MCP_ENABLED": "true",
        "BLADE_AI_MCP_CONFIG_PATH": str(mcp_path),
        "BLADE_AI_MCP_CONNECT_TIMEOUT_SECONDS": str(BLADEAI_MCP_CONNECT_TIMEOUT_SECONDS),
        # The upstream catalogue is versioned independently from the
        # Controller's trial-bound shim.  Tell the isolated Worker to use the
        # latter's published Stage-2 capability contract when planning; this
        # is runtime policy metadata and does not alter the user's prompt.
        "RESBENCH_BLADEAI_STAGE2": "true",
        # The WP8 qualification prompt is deliberately a direct MCP workflow.
        # The worker uses this marker to shorten the built-in skill tool
        # description, preventing the upstream "activate_skill" instruction
        # from injecting a large catalogue before the first read.
        "RESBENCH_BLADEAI_WP8": "true" if qualification_fault is not None else "false",
        # The evaluated Worker runs in the agent-runtime sidecar, whose image
        # is intentionally immutable and may lag the Controller overlay. Put
        # the Trial-local source overlay first, then extend the package path
        # to the immutable image's remaining Stage-2 modules.
        "PYTHONPATH": os.pathsep.join((str(python_overlay), str(repo_root))),
        "RESBENCH_TRIAL_NAMESPACE": namespace,
        "RESBENCH_BLADE_SHIM_STATE_FILE": str(agent_home / "blade-aliases.json"),
    })
    return [python_executable, "-m", "stage2_service.bladeai_worker", str(request_path)], b"", child


def _write_agent_python_overlay(*, repo_root: Path, trial_root: Path) -> Path:
    """Copy the patched Worker modules into the shared Trial workspace."""
    overlay = trial_root / "python-overlay"
    package = overlay / "stage2_service"
    package.mkdir(mode=0o700, parents=True, exist_ok=True)
    (package / "__init__.py").write_text(
        '"""Trial-local Stage-2 source overlay for the BladeAI Worker."""\n'
        f"__path__.append({str(repo_root / 'stage2_service')!r})\n",
        encoding="utf-8",
    )
    for module in ("bladeai_worker.py", "bladeai_events.py", "bladeai_shim.py"):
        source = repo_root / "stage2_service" / module
        target = package / module
        target.write_bytes(source.read_bytes())
        target.chmod(0o600)
    return overlay
