#!/usr/bin/env python3
"""Close an interrupted D0 Campaign as invalid while preserving provenance."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness.d0.common import append_jsonl, utc_now, write_json, write_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign_dir", type=Path)
    parser.add_argument("--reason-code", required=True)
    parser.add_argument("--reason", required=True)
    args = parser.parse_args()
    root = args.campaign_dir.expanduser().resolve()
    path = root / "campaign.json"
    campaign = json.loads(path.read_text(encoding="utf-8"))
    if campaign.get("status") not in {"RUNNING", "STOPPED_RESET_FAILED"}:
        raise SystemExit(f"campaign is already terminal: {campaign.get('status')}")
    original = root / "campaign.before-invalidation.json"
    if not original.exists():
        shutil.copyfile(path, original)
    finished_at = utc_now()
    campaign.update(
        {
            "status": "QUALIFICATION_INVALID",
            "finished_at": finished_at,
            "invalid_reason": {
                "code": args.reason_code,
                "message": args.reason,
                "source": "harness-implementation",
            },
        }
    )
    write_json(path, campaign)
    append_jsonl(
        root / "campaign-events.jsonl",
        {
            "ts": finished_at,
            "actor": "harness",
            "kind": "campaign_invalidated",
            "reason_code": args.reason_code,
            "reason": args.reason,
        },
    )
    write_manifest(root)
    print(json.dumps({"campaign_id": campaign.get("campaign_id"), "status": campaign["status"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
