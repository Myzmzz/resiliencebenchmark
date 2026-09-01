#!/usr/bin/env python3
"""Build model-bound Stage-2 qualification refs from two sealed D0 campaigns."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

HARNESSES = ("bladeai", "claude-code", "codex", "deepseek-harness")
MODELS = ("gpt-5.6-sol", "claude-opus-5")


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--d0-root", type=Path, required=True)
    value.add_argument(
        "--campaign",
        action="append",
        required=True,
        metavar="MODEL=CAMPAIGN_ID",
        help="repeat once for gpt-5.6-sol and once for claude-opus-5",
    )
    value.add_argument("--output", type=Path, required=True)
    return value


def build(d0_root: Path, assignments: dict[str, str]) -> dict:
    if set(assignments) != set(MODELS):
        raise ValueError("exactly one D0 campaign is required for each matrix model")
    models = {}
    for model in MODELS:
        campaign_id = assignments[model]
        root = (d0_root / campaign_id).resolve()
        root.relative_to(d0_root.resolve())
        campaign_path = root / "campaign.json"
        manifest_path = root / "manifest.sha256"
        if not campaign_path.is_file() or not manifest_path.is_file():
            raise ValueError(f"D0 campaign evidence is incomplete: {campaign_id}")
        campaign = json.loads(campaign_path.read_text(encoding="utf-8"))
        if campaign.get("host", {}).get("verified") is not True:
            raise ValueError(f"D0 campaign host is not verified: {campaign_id}")
        results = {
            item.get("agent"): item for item in campaign.get("results", [])
        }
        campaign_models = campaign.get("models") or {}
        models[model] = {}
        for harness in HARNESSES:
            status = str((results.get(harness) or {}).get("status") or "MISSING")
            if status != "PASS":
                raise ValueError(
                    f"D0 qualification is not PASS: {campaign_id}/{harness}={status}"
                )
            if campaign_models.get(harness) != model:
                raise ValueError(
                    f"D0 model identity mismatch: {campaign_id}/{harness}"
                )
            models[model][harness] = {
                "campaign_id": campaign_id,
                "manifest_sha256": hashlib.sha256(
                    manifest_path.read_bytes()
                ).hexdigest(),
                "agent_status": status,
                "model_alias": model,
            }
    return {
        "schema_version": "stage2-qualification-matrix.v1",
        "models": models,
    }


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    assignments = {}
    for raw in args.campaign:
        if "=" not in raw:
            raise SystemExit("--campaign must use MODEL=CAMPAIGN_ID")
        model, campaign_id = raw.split("=", 1)
        if model in assignments:
            raise SystemExit(f"duplicate --campaign model: {model}")
        assignments[model] = campaign_id
    payload = build(args.d0_root.expanduser().resolve(), assignments)
    output = args.output.expanduser().resolve()
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, output)
    print(json.dumps({"status": "ready", "output": str(output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
