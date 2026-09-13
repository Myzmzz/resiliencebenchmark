#!/usr/bin/env python3
"""Apply one environment's overlay to the Stage-2 base manifest.

The repository's rendered manifests are deliberately not applied as-is: four
settings they cannot carry are per-cluster, and losing any of them is silent at
deploy time (see README.md). This renderer puts exactly those four back, plus
the storage class and this cluster's DNS fallback, and leaves everything else
untouched.

It does not touch image references. Those still go through
``tools/dx-round/deploy_boundary.sh``, which replaces exactly four of them and
refuses to run while an evaluation is in flight.

    python deploy/stage2/env3/render.py --out /tmp/env3-stage2.yaml
    python scripts/verify_stage2_deployment.py --manifest /tmp/env3-stage2.yaml \\
        --coroot-project po24tcoz --require-node-selector
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
DEFAULT_BASE = REPO_ROOT / "deploy/stage2/stage2-integration.yaml"
DEFAULT_GATEWAY = REPO_ROOT / "deploy/stage2/litellm/config.yaml"
DEFAULT_VALUES = HERE / "values.yaml"
DEPLOYMENT_NAME = "resbench-stage2-integration"
PVC_NAME = "resbench-stage2-data"
COROOT_PROJECT_ENV = "RESBENCH_COROOT_PROJECT_ID"
COROOT_ANONYMOUS_ENV = "RESBENCH_COROOT_ALLOW_ANONYMOUS_READ"
HARNESS_CAPABILITIES_ENV = "STAGE2_HARNESS_CAPABILITIES_FILE"


class OverlayError(RuntimeError):
    """The overlay cannot be applied to this base manifest."""


def load_documents(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    return [doc for doc in yaml.safe_load_all(text) if isinstance(doc, dict)]


def apply_overlay(
    documents: Sequence[Mapping[str, Any]], values: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Return the documents with this environment's three settings applied."""
    rendered = [yaml.safe_load(yaml.safe_dump(doc)) for doc in documents]
    touched = {"deployment": False, "pvc": False}

    for doc in rendered:
        kind, name = doc.get("kind"), (doc.get("metadata") or {}).get("name")
        if kind == "Deployment" and name == DEPLOYMENT_NAME:
            _apply_to_deployment(doc, values)
            touched["deployment"] = True
        elif kind == "PersistentVolumeClaim" and name == PVC_NAME:
            storage_class = values.get("storageClassName")
            if storage_class:
                doc["spec"]["storageClassName"] = storage_class
            touched["pvc"] = True

    if not touched["deployment"]:
        raise OverlayError(
            f"base manifest has no Deployment/{DEPLOYMENT_NAME}; nothing to overlay"
        )
    return rendered




def apply_gateway_routes(config_text: str, values: Mapping[str, Any]) -> str:
    """Repoint named routes in the LiteLLM table, keeping the table's comments.

    The base table carries the reasoning behind every route choice -- which
    relay was sampled against which, and why one was preferred. A YAML
    round-trip would drop all of it, so the edit is done on the text: find the
    ``- model_name: <alias>`` block, then replace only the keys named for that
    alias, leaving everything else (including neighbouring comments) alone.
    """
    routes = values.get("gatewayRoutes") or {}
    if not routes:
        return config_text

    lines = config_text.splitlines(keepends=True)
    out: list[str] = []
    current: str | None = None
    seen: set[str] = set()

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("- model_name:"):
            current = stripped.split(":", 1)[1].strip()
        elif current and stripped.startswith("- model_name"):
            current = None

        overrides = routes.get(current or "")
        if overrides:
            key = stripped.split(":", 1)[0].strip() if ":" in stripped else ""
            if key in overrides:
                indent = line[: len(line) - len(line.lstrip())]
                out.append(f"{indent}{key}: {overrides[key]}\n")
                seen.add(current or "")
                continue
        out.append(line)

    missing = sorted(set(routes) - seen)
    if missing:
        raise OverlayError(
            "the gateway table has no route to override for: " + ", ".join(missing)
        )
    return "".join(out)


def _apply_to_deployment(doc: dict[str, Any], values: Mapping[str, Any]) -> None:
    spec = doc.setdefault("spec", {}).setdefault("template", {}).setdefault("spec", {})

    security = dict(values.get("securityContext") or {})
    if security:
        spec.setdefault("securityContext", {}).update(security)

    selector = values.get("nodeSelector")
    if selector:
        spec["nodeSelector"] = dict(selector)

    dns = values.get("dnsFallback") or {}
    nameservers = [str(item) for item in (dns.get("nameservers") or []) if item]
    if nameservers:
        # ClusterFirst appends these after the cluster resolver, so in-cluster
        # names still go to CoreDNS first and only a SERVFAIL falls through.
        config = spec.setdefault("dnsConfig", {})
        config["nameservers"] = nameservers

    coroot = values.get("coroot") or {}
    capabilities_file = values.get("harnessCapabilitiesFile")
    if coroot or capabilities_file:
        container = _container(spec, "stage2")
        if container is None:
            raise OverlayError("base manifest has no stage2 container")
        env = container.setdefault("env", [])
        if coroot.get("projectId"):
            _set_env(env, COROOT_PROJECT_ENV, str(coroot["projectId"]))
        if "allowAnonymousRead" in coroot:
            _set_env(
                env,
                COROOT_ANONYMOUS_ENV,
                "true" if coroot["allowAnonymousRead"] else "false",
            )
        if capabilities_file:
            _set_env(env, HARNESS_CAPABILITIES_ENV, str(capabilities_file))


def _container(spec: Mapping[str, Any], name: str) -> dict[str, Any] | None:
    for item in spec.get("containers", []):
        if isinstance(item, dict) and item.get("name") == name:
            return item
    return None


def _set_env(env: list[dict[str, Any]], name: str, value: str) -> None:
    """Replace the variable in place, keeping its position in the list."""
    for item in env:
        if isinstance(item, dict) and item.get("name") == name:
            item.pop("valueFrom", None)
            item["value"] = value
            return
    env.append({"name": name, "value": value})


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=DEFAULT_BASE)
    parser.add_argument("--values", type=Path, default=DEFAULT_VALUES)
    parser.add_argument("--out", type=Path, help="write here instead of stdout")
    parser.add_argument(
        "--gateway-config",
        type=Path,
        help="write this environment's LiteLLM routing table here instead of "
        "rendering the Deployment; feed it to render_litellm_gateway.py --config",
    )
    parser.add_argument("--gateway-base", type=Path, default=DEFAULT_GATEWAY)
    args = parser.parse_args(argv)

    values = yaml.safe_load(args.values.read_text(encoding="utf-8")) or {}

    if args.gateway_config is not None:
        try:
            table = apply_gateway_routes(
                args.gateway_base.read_text(encoding="utf-8"), values
            )
        except OverlayError as exc:
            print(f"overlay error: {exc}", file=sys.stderr)
            return 2
        args.gateway_config.write_text(table, encoding="utf-8")
        print(f"rendered the gateway table to {args.gateway_config}")
        return 0

    try:
        rendered = apply_overlay(load_documents(args.base), values)
    except OverlayError as exc:
        print(f"overlay error: {exc}", file=sys.stderr)
        return 2

    text = "---\n" + "---\n".join(
        yaml.safe_dump(doc, sort_keys=False, allow_unicode=True) for doc in rendered
    )
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"rendered {len(rendered)} documents to {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
