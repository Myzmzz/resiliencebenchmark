#!/usr/bin/env python3
"""Build model/Harness-bound Stage-2 refs from sealed D0 campaigns."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness.d0.common import evaluation_ready_result

HARNESSES = ("bladeai", "claude-code", "codex", "deepseek-harness")
MODELS = ("gpt-5.6-sol", "claude-opus-5")


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--d0-root", type=Path, required=True)
    value.add_argument(
        "--campaign",
        action="append",
        required=True,
        metavar="MODEL/HARNESS=CAMPAIGN_ID",
        help="repeat once for every one of the eight model/Harness pairs",
    )
    value.add_argument("--output", type=Path, required=True)
    return value


def build(d0_root: Path, assignments: dict[tuple[str, str], str]) -> dict:
    expected = {(model, harness) for model in MODELS for harness in HARNESSES}
    if set(assignments) != expected:
        raise ValueError("exactly one D0 campaign is required for each model/Harness pair")
    models = {}
    for model in MODELS:
        models[model] = {}
        for harness in HARNESSES:
            campaign_id = assignments[(model, harness)]
            root = (d0_root / campaign_id).resolve()
            root.relative_to(d0_root.resolve())
            campaign_path = root / "campaign.json"
            manifest_path = root / "manifest.sha256"
            if not campaign_path.is_file() or not manifest_path.is_file():
                raise ValueError(f"D0 campaign evidence is incomplete: {campaign_id}")
            campaign = json.loads(campaign_path.read_text(encoding="utf-8"))
            if campaign.get("host", {}).get("verified") is not True:
                raise ValueError(f"D0 campaign host is not verified: {campaign_id}")
            result = next(
                (
                    item
                    for item in campaign.get("results", [])
                    if item.get("agent") == harness
                ),
                None,
            )
            status = str((result or {}).get("status") or "MISSING")
            if not evaluation_ready_result(dict(result or {})):
                raise ValueError(
                    f"D0 evidence is not evaluation-ready: {campaign_id}/{harness}={status}"
                )
            if (campaign.get("models") or {}).get(harness) != model:
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
        if "=" not in raw or "/" not in raw.split("=", 1)[0]:
            raise SystemExit("--campaign must use MODEL/HARNESS=CAMPAIGN_ID")
        pair, campaign_id = raw.split("=", 1)
        model, harness = pair.split("/", 1)
        key = (model, harness)
        if key in assignments:
            raise SystemExit(f"duplicate --campaign pair: {pair}")
        assignments[key] = campaign_id
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
