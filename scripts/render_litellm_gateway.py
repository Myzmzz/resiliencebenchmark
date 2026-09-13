#!/usr/bin/env python3
"""Render the Stage-2 LiteLLM gateway ConfigMap and Secret from a local env file.

The gateway routing table (``deploy/stage2/litellm/config.yaml``) names
upstream credentials only through ``os.environ/NAME`` placeholders. This script
keeps the routing table, the credential file, and the cluster objects in
agreement:

1. collect every placeholder the routing table references,
2. verify that a local env file (never committed) defines each of them,
3. write three Kubernetes manifests: ConfigMap ``litellm-config`` with the
   routing table and Secret ``litellm-upstream`` with exactly the referenced
   credentials, plus gateway-only ``resbench-stage2-gateway-client`` for the
   trusted Controllers (never evaluated Agent containers),
4. print a redacted summary. Secret values are never echoed.

Typical use::

    uv run python scripts/render_litellm_gateway.py \\
        --env-file "../.secrets/llm-providers.env" --check
    uv run python scripts/render_litellm_gateway.py \\
        --env-file "../.secrets/llm-providers.env" --output-dir /tmp/litellm-render

Apply and roll out only with the reviewed cluster/data preservation procedure
in ``deploy/stage2/litellm/README.md``.
"""

from __future__ import annotations

import argparse
import copy
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
CLIENT_SECRET_NAME = "resbench-stage2-gateway-client"
CONFIG_KEY = "config.yaml"
AUDIT_CALLBACK_KEY = "gateway_audit.py"
AUDIT_CALLBACK_PATH = REPO_ROOT / "stage2_service/gateway_audit_callback.py"
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


def pace_config(
    config: Mapping[str, Any],
    *,
    max_parallel_requests: int | None,
    replicas: int,
    account_rpm: int | None,
    account_tpm: int | None,
) -> dict[str, Any]:
    """Add pacing to a routing table: a concurrency gate and per-replica budgets.

    Every Controller Pod runs its own gateway sidecar and they cannot see one
    another, so an account-wide budget has to be divided by the number of
    replicas that will share it. ``max_parallel_requests`` is the lever that
    actually makes a request wait instead of failing; the per-alias rpm and
    tpm are the guard rail that stops one replica running away with the
    account. Passing none of them returns the table unchanged.
    """
    if replicas < 1:
        raise ValueError("--replicas must be at least 1")
    document = copy.deepcopy(dict(config))
    if max_parallel_requests is not None:
        if max_parallel_requests < 1:
            raise ValueError("--max-parallel-requests must be at least 1")
        settings = dict(document.get("litellm_settings") or {})
        settings["max_parallel_requests"] = max_parallel_requests
        document["litellm_settings"] = settings
    per_replica_rpm = account_rpm // replicas if account_rpm else None
    per_replica_tpm = account_tpm // replicas if account_tpm else None
    if per_replica_rpm is not None and per_replica_rpm < 1:
        raise ValueError("--account-rpm divided by --replicas leaves less than one request per minute")
    if per_replica_tpm is not None and per_replica_tpm < 1:
        raise ValueError("--account-tpm divided by --replicas leaves less than one token per minute")
    if per_replica_rpm is None and per_replica_tpm is None:
        return document
    entries = []
    for entry in document.get("model_list") or []:
        item = dict(entry)
        params = dict(item.get("litellm_params") or {})
        if per_replica_rpm is not None:
            params["rpm"] = per_replica_rpm
        if per_replica_tpm is not None:
            params["tpm"] = per_replica_tpm
        item["litellm_params"] = params
        entries.append(item)
    document["model_list"] = entries
    return document


def render_manifests(
    config_text: str,
    config: Mapping[str, Any],
    env: Mapping[str, str],
    namespace: str,
    configmap_name: str = CONFIGMAP_NAME,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Separate provider credentials from the Controller's gateway-only client."""
    configmap = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": configmap_name,
            "namespace": namespace,
            "labels": dict(MANAGED_LABELS),
        },
        "data": {
            CONFIG_KEY: config_text,
            AUDIT_CALLBACK_KEY: AUDIT_CALLBACK_PATH.read_text(encoding="utf-8"),
        },
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
    client_secret = {
        "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
        "metadata": {"name": CLIENT_SECRET_NAME, "namespace": namespace, "labels": dict(MANAGED_LABELS)},
        "stringData": {"llm-base-url": "http://127.0.0.1:4000/v1", "llm-api-key": env["LITELLM_MASTER_KEY"]},
    }
    return configmap, secret, client_secret


def write_manifests(output_dir: Path, configmap: Mapping[str, Any], secret: Mapping[str, Any], client_secret: Mapping[str, Any]) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename, payload in (
        ("litellm-config.configmap.yaml", configmap),
        ("litellm-upstream.secret.yaml", secret),
        ("resbench-stage2-gateway-client.secret.yaml", client_secret),
    ):
        path = output_dir / filename
        path.write_text(
            yaml.safe_dump(dict(payload), sort_keys=False, allow_unicode=True, default_style=None),
            encoding="utf-8",
        )
        path.chmod(0o600)
        written.append(path)
    return written


def pacing_summary(config: Mapping[str, Any], args: argparse.Namespace) -> Iterable[str]:
    settings = config.get("litellm_settings") or {}
    if settings.get("max_parallel_requests"):
        yield f"pacing: max_parallel_requests={settings['max_parallel_requests']} per gateway"
    first = (config.get("model_list") or [{}])[0].get("litellm_params") or {}
    if first.get("rpm") or first.get("tpm"):
        yield (
            f"pacing: per-alias rpm={first.get('rpm')} tpm={first.get('tpm')} "
            f"(account rpm={args.account_rpm} tpm={args.account_tpm} split over {args.replicas} replicas)"
        )


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
    value.add_argument(
        "--configmap-name",
        default=CONFIGMAP_NAME,
        help=(
            "name of the rendered ConfigMap; use a separate one for a replica fleet "
            "so its pacing does not change the single-system Controller"
        ),
    )
    value.add_argument(
        "--max-parallel-requests",
        type=int,
        help=(
            "concurrent upstream requests one gateway will run; further requests wait "
            "rather than fail. This is the lever that paces, not the one that rejects"
        ),
    )
    value.add_argument(
        "--replicas",
        type=int,
        default=1,
        help="how many Pods share the account budget; each gets account limit / replicas",
    )
    value.add_argument(
        "--account-rpm",
        type=int,
        help="requests per minute the upstream account allows, read from its console",
    )
    value.add_argument(
        "--account-tpm",
        type=int,
        help="tokens per minute the upstream account allows, read from its console",
    )
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
    paced = pace_config(
        config,
        max_parallel_requests=args.max_parallel_requests,
        replicas=args.replicas,
        account_rpm=args.account_rpm,
        account_tpm=args.account_tpm,
    )
    if paced != config:
        # Re-serialize only when pacing changed something, so an unpaced render
        # still ships the reviewed file byte for byte, comments included.
        config_text = yaml.safe_dump(paced, sort_keys=False, allow_unicode=True, width=100)
        for line in pacing_summary(paced, args):
            print(line)
    configmap, secret, client_secret = render_manifests(
        config_text, paced, env, args.namespace, args.configmap_name
    )
    for path in write_manifests(args.output_dir, configmap, secret, client_secret):
        print(f"wrote {path}")
    print(
        "Generated gateway objects only; preserve live workload data paths and "
        "apply the reviewed old-cluster rollout before qualifying Agents."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
