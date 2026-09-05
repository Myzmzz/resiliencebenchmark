#!/usr/bin/env python3
"""Render the Stage-2 LiteLLM gateway ConfigMap and Secret from a local env file.

The gateway routing table (``deploy/stage2/litellm/config.yaml``) names
upstream credentials only through ``os.environ/NAME`` placeholders. This script
keeps the routing table, the credential file, and the cluster objects in
agreement:

1. collect every placeholder the routing table references,
2. verify that a local env file (never committed) defines each of them,
3. write two Kubernetes manifests: ConfigMap ``litellm-config`` with the
   routing table and Secret ``litellm-upstream`` with exactly the referenced
   credentials,
4. print a redacted summary. Secret values are never echoed.

Typical use::

    uv run python scripts/render_litellm_gateway.py \\
        --env-file "../.secrets/llm-providers.env" --check
    uv run python scripts/render_litellm_gateway.py \\
        --env-file "../.secrets/llm-providers.env" --output-dir /tmp/litellm-render
    kubectl apply -f /tmp/litellm-render/
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "deploy/stage2/litellm/config.yaml"
DEFAULT_NAMESPACE = "resiliencebenchmark-system"
CONFIGMAP_NAME = "litellm-config"
SECRET_NAME = "litellm-upstream"
CONFIG_KEY = "config.yaml"
# The sidecar authenticates callers with this key even if a future routing
# table stops referencing it explicitly.
ALWAYS_REQUIRED = frozenset({"LITELLM_MASTER_KEY"})
ENVIRON_REFERENCE = re.compile(r"^os\.environ/([A-Za-z_][A-Za-z0-9_]*)$")
ENV_LINE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
MANAGED_LABELS = {"app.kubernetes.io/managed-by": "resiliencebenchmark"}


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines; ``export`` prefixes, quotes and comments are tolerated."""
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = ENV_LINE.match(line)
        if not match:
            raise ValueError(f"{path}: cannot parse line: {raw_line!r}")
        name, value = match.group(1), match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[name] = value
    return values


def collect_environ_references(node: Any) -> set[str]:
    """Return every ``os.environ/NAME`` placeholder found anywhere in ``node``."""
    found: set[str] = set()
    if isinstance(node, str):
        match = ENVIRON_REFERENCE.match(node.strip())
        if match:
            found.add(match.group(1))
    elif isinstance(node, Mapping):
        for value in node.values():
            found |= collect_environ_references(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            found |= collect_environ_references(value)
    return found


def model_aliases(config: Mapping[str, Any]) -> list[str]:
    """Public aliases (``model_name``) declared in a LiteLLM routing table."""
    aliases: list[str] = []
    for entry in config.get("model_list") or []:
        if isinstance(entry, Mapping) and entry.get("model_name"):
            aliases.append(str(entry["model_name"]))
    return aliases


def required_names(config: Mapping[str, Any]) -> set[str]:
    return collect_environ_references(config) | set(ALWAYS_REQUIRED)


def validate_credentials(config: Mapping[str, Any], env: Mapping[str, str]) -> list[str]:
    """Return human-readable problems; an empty list means the env file is complete."""
    problems: list[str] = []
    for name in sorted(required_names(config)):
        value = env.get(name, "")
        if not value.strip():
            problems.append(f"missing or empty credential: {name}")
        elif value != value.strip():
            problems.append(f"credential has surrounding whitespace: {name}")
    if not model_aliases(config):
        problems.append("routing table declares no model_list entries")
    return problems


def unused_names(config: Mapping[str, Any], env: Mapping[str, str]) -> list[str]:
    return sorted(set(env) - required_names(config))


def render_manifests(
    config_text: str,
    config: Mapping[str, Any],
    env: Mapping[str, str],
    namespace: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the ConfigMap and Secret objects (Secret carries only referenced names)."""
    configmap = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": CONFIGMAP_NAME,
            "namespace": namespace,
            "labels": dict(MANAGED_LABELS),
        },
        "data": {CONFIG_KEY: config_text},
    }
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "Opaque",
        "metadata": {
            "name": SECRET_NAME,
            "namespace": namespace,
            "labels": dict(MANAGED_LABELS),
        },
        "stringData": {name: env[name] for name in sorted(required_names(config))},
    }
    return configmap, secret


def write_manifests(output_dir: Path, configmap: Mapping[str, Any], secret: Mapping[str, Any]) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename, payload in (
        ("litellm-config.configmap.yaml", configmap),
        ("litellm-upstream.secret.yaml", secret),
    ):
        path = output_dir / filename
        path.write_text(
            yaml.safe_dump(dict(payload), sort_keys=False, allow_unicode=True, default_style=None),
            encoding="utf-8",
        )
        path.chmod(0o600)
        written.append(path)
    return written


def summary_lines(config: Mapping[str, Any], env: Mapping[str, str]) -> Iterable[str]:
    yield f"aliases ({len(model_aliases(config))}): " + ", ".join(model_aliases(config))
    for name in sorted(required_names(config)):
        state = "set" if env.get(name, "").strip() else "MISSING"
        yield f"  {name}: {state}"
    extra = unused_names(config, env)
    if extra:
        yield "  (not referenced by the routing table, ignored: " + ", ".join(extra) + ")"


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    value.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="LiteLLM routing table")
    value.add_argument("--env-file", type=Path, required=True, help="KEY=VALUE credential file kept outside git")
    value.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    value.add_argument("--output-dir", type=Path, help="where to write the ConfigMap and Secret manifests")
    value.add_argument("--check", action="store_true", help="validate only; write nothing")
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    config_text = args.config.read_text(encoding="utf-8")
    config = yaml.safe_load(config_text) or {}
    env = parse_env_file(args.env_file)
    problems = validate_credentials(config, env)
    for line in summary_lines(config, env):
        print(line)
    if problems:
        for problem in problems:
            print(f"ERROR: {problem}", file=sys.stderr)
        return 2
    if args.check or not args.output_dir:
        print("credentials complete; nothing written" if args.check else "credentials complete; pass --output-dir to render")
        return 0
    configmap, secret = render_manifests(config_text, config, env, args.namespace)
    for path in write_manifests(args.output_dir, configmap, secret):
        print(f"wrote {path}")
    print(
        "apply with: kubectl apply -f "
        f"{args.output_dir}/ && kubectl -n {args.namespace} rollout restart deploy/resbench-stage2-integration"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
