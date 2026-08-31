#!/usr/bin/env python3
"""Qualify the Codex disturbance E2E stack on an explicitly selected cluster."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = REPO_ROOT / "runs/local-e2e"
CODEX_EVAL = RUNTIME_ROOT / "bin/codex-eval"
REQUIRED_NAMESPACES = ("otel-demo", "observability")


class LocalE2EError(RuntimeError):
    pass


def parse_bashrc_assignments(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    result: dict[str, str] = {}
    pattern = re.compile(r"^(?:export\s+)?(acuurl|acukey)=(.*)$")
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        match = pattern.match(raw.strip())
        if not match:
            continue
        key, value = match.groups()
        try:
            parts = shlex.split(value, comments=False, posix=True)
        except ValueError:
            parts = []
        result[key] = parts[0] if parts else value.strip("'\"")
    return result


def sanitize(value: str) -> str:
    redacted = value
    secrets = parse_bashrc_assignments(Path.home() / ".bashrc")
    for raw in secrets.values():
        if raw:
            redacted = redacted.replace(raw, "<redacted>")
    return re.sub(r"sk-[A-Za-z0-9_-]{8,}", "<redacted>", redacted)


def selected_context(value: str | None) -> str:
    context = value or os.environ.get("RESBENCH_E2E_CONTEXT", "")
    if not context:
        raise LocalE2EError("explicit --context or RESBENCH_E2E_CONTEXT is required")
    return context


def run(argv: list[str], *, timeout: int = 30, check: bool = False) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and completed.returncode:
        detail = sanitize(completed.stderr or completed.stdout)
        raise LocalE2EError(f"command failed: {argv[0]}: {detail[:400]}")
    return completed


def kubectl(context: str, *args: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return run(["kubectl", "--context", context, *args], timeout=timeout)


def _probe(context: str, name: str, *args: str) -> dict[str, Any]:
    result = kubectl(context, *args)
    return {
        "name": name,
        "ok": result.returncode == 0,
        "detail": sanitize((result.stdout or result.stderr).strip())[:500],
    }


def inventory(context: str | None) -> dict[str, Any]:
    chosen = selected_context(context)
    bashrc = parse_bashrc_assignments(Path.home() / ".bashrc")
    current = run(["kubectl", "config", "current-context"])
    return {
        "schema_version": "resbench-local-e2e-inventory.v4",
        "selected_context": chosen,
        "current_context": current.stdout.strip() if current.returncode == 0 else "",
        "cluster_lifecycle_managed": False,
        "codex_eval": str(CODEX_EVAL),
        "codex_eval_present": CODEX_EVAL.is_file(),
        "llm_env": {
            "base_url_present": bool(os.environ.get("RESBENCH_LLM_BASE_URL") or bashrc.get("acuurl")),
            "api_key_present": bool(os.environ.get("RESBENCH_LLM_API_KEY") or bashrc.get("acukey")),
        },
    }


def qualify(context: str | None) -> dict[str, Any]:
    chosen = selected_context(context)
    base = inventory(chosen)
    probes = [
        _probe(chosen, "nodes_ready", "get", "nodes"),
        *(_probe(chosen, f"namespace:{namespace}", "get", "namespace", namespace) for namespace in REQUIRED_NAMESPACES),
        _probe(chosen, "chaosblade_crd", "get", "crd", "chaosblades.chaosblade.io"),
        _probe(chosen, "chaosblade_operator", "get", "deployment", "chaosblade-operator", "-n", "default"),
        _probe(chosen, "otel_demo", "get", "deployment", "cart", "-n", "otel-demo"),
        _probe(chosen, "load_generator", "get", "deployment", "load-generator", "-n", "otel-demo"),
        _probe(chosen, "prometheus", "get", "service", "prometheus", "-n", "observability"),
        _probe(chosen, "jaeger", "get", "service", "jaeger-query", "-n", "observability"),
        _probe(chosen, "loki", "get", "service", "loki", "-n", "observability"),
        _probe(chosen, "stage2_runtime", "get", "deployment", "resbench-stage2", "-n", "resiliencebenchmark-system"),
    ]
    chaos = kubectl(chosen, "get", "chaosblades.chaosblade.io", "-o", "json")
    active: list[dict[str, str]] = []
    if chaos.returncode == 0:
        payload = json.loads(chaos.stdout or "{}")
        for item in payload.get("items", []):
            if item.get("status", {}).get("phase") == "Running":
                labels = item.get("metadata", {}).get("labels") or {}
                active.append(
                    {
                        "name": str(item.get("metadata", {}).get("name") or ""),
                        "owner": str(labels.get("benchmark.run_id") or labels.get("resiliencebenchmark.io/run") or "unowned"),
                    }
                )
    probes.append(
        {
            "name": "active_chaosblade_inventory",
            "ok": not active,
            "detail": "clean" if not active else f"blocked_by_active_fault:{active}",
        }
    )
    expected_head = run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"])
    deployed_head = kubectl(
        chosen,
        "get",
        "deployment",
        "resbench-stage2",
        "-n",
        "resiliencebenchmark-system",
        "-o",
        "jsonpath={.spec.template.metadata.labels.resiliencebenchmark\\.io/source-head}",
    )
    expected = expected_head.stdout.strip() if expected_head.returncode == 0 else ""
    actual = deployed_head.stdout.strip() if deployed_head.returncode == 0 else ""
    probes.append(
        {
            "name": "stage2_source_identity",
            "ok": bool(expected and actual == expected),
            "detail": f"expected={expected or 'unknown'} deployed={actual or 'unlabelled'}",
        }
    )
    codex_version = run([str(CODEX_EVAL), "--version"]) if CODEX_EVAL.is_file() else None
    probes.append(
        {
            "name": "codex_eval",
            "ok": bool(codex_version and codex_version.returncode == 0),
            "detail": sanitize((codex_version.stdout if codex_version else "missing").strip()),
        }
    )
    qualified = all(item["ok"] for item in probes) and all(base["llm_env"].values())
    return {**base, "qualified": qualified, "probes": probes}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("inventory", "up", "check", "down"))
    parser.add_argument("--context")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    del args.timeout
    try:
        if args.command in {"inventory", "check", "up"}:
            result = inventory(args.context) if args.command == "inventory" else qualify(args.context)
        else:
            result = {
                "status": "no_cluster_resources_owned",
                "context": selected_context(args.context),
                "removed": [],
            }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command in {"check", "up"} and not result.get("qualified"):
            return 2
        return 0
    except (LocalE2EError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"status": "failed", "error": sanitize(str(exc))}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
