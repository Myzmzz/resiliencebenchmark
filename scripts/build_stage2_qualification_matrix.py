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
from stage2_service.contracts import STAGE2_MODEL_MATRIX
from stage2_service.gateway_evidence import read_gateway_artifact

HARNESSES = ("bladeai", "claude-code", "codex", "deepseek-harness")
# The qualification matrix covers every alias the Stage-2 service exposes.
MODELS = STAGE2_MODEL_MATRIX


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--d0-root", type=Path, required=True)
    value.add_argument(
        "--campaign",
        action="append",
        required=True,
        metavar="MODEL/HARNESS=CAMPAIGN_ID",
        help=(
            "repeat once for every model/Harness pair "
            f"({len(MODELS)} models x {len(HARNESSES)} Harnesses)"
        ),
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
            if (campaign.get("models") or {}).get(harness) != model:
                raise ValueError(
                    f"D0 model identity mismatch: {campaign_id}/{harness}"
                )
            if (result or {}).get("model_alias") != model:
                raise ValueError(
                    f"D0 result model identity mismatch: {campaign_id}/{harness}"
                )
            gateway_route = (result or {}).get("gateway_route")
            gateway_hash = str((result or {}).get("gateway_config_sha256") or "")
            gateway_request_ids = (result or {}).get("gateway_request_ids")
            gateway_evidence_ref = str((result or {}).get("gateway_evidence_ref") or "")
            gateway_trial_id = str((result or {}).get("gateway_trial_id") or "")
            if not isinstance(gateway_route, dict) or not gateway_route:
                raise ValueError(
                    f"D0 gateway route evidence is missing: {campaign_id}/{harness}"
                )
            if not re_fullmatch_sha256(gateway_hash):
                raise ValueError(
                    f"D0 gateway config version evidence is missing: {campaign_id}/{harness}"
                )
            if (
                (result or {}).get("gateway_evidence_verified") is not True
                or not isinstance(gateway_request_ids, list)
                or not gateway_request_ids
                or not all(isinstance(item, str) and item for item in gateway_request_ids)
                or len(set(gateway_request_ids)) != len(gateway_request_ids)
                or not gateway_evidence_ref
                or not gateway_trial_id
            ):
                raise ValueError(
                    f"D0 gateway request evidence is missing: {campaign_id}/{harness}"
                )
            evidence_ref_path = Path(gateway_evidence_ref)
            if (
                evidence_ref_path.is_absolute()
                or ".." in evidence_ref_path.parts
                or not evidence_ref_path.parts
                or evidence_ref_path.parts[0] != "native"
                or evidence_ref_path.name != "gateway-requests.json"
            ):
                raise ValueError(
                    f"D0 gateway request evidence is missing: {campaign_id}/{harness}"
                )
            gateway_rows = read_gateway_artifact(
                root / harness / evidence_ref_path,
                trial_id=gateway_trial_id,
                harness=harness,
                model_alias=model,
                config_sha256=gateway_hash,
                request_ids=set(gateway_request_ids),
            )
            if gateway_rows is None:
                raise ValueError(
                    f"D0 gateway request evidence is missing: {campaign_id}/{harness}"
                )
            models[model][harness] = {
                "campaign_id": campaign_id,
                "manifest_sha256": hashlib.sha256(
                    manifest_path.read_bytes()
                ).hexdigest(),
                "agent_status": status,
                "model_alias": model,
                "gateway_route": gateway_route,
                "gateway_config_sha256": gateway_hash,
                "gateway_evidence_verified": True,
                "gateway_request_ids": gateway_request_ids,
                "gateway_evidence_ref": gateway_evidence_ref,
                "gateway_trial_id": gateway_trial_id,
                "evaluation_ready": evaluation_ready_result(dict(result or {})),
                "invalid_reason": (
                    None
                    if evaluation_ready_result(dict(result or {}))
                    else "D0 result is platform-invalid and Stage-2 must run diagnostic-only"
                ),
            }
    return {
        "schema_version": "stage2-qualification-matrix.v1",
        "models": models,
    }


def re_fullmatch_sha256(value: str) -> bool:
    return len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)


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
