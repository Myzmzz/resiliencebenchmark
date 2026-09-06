#!/usr/bin/env python3
"""Build a WP8 qualification record from completed, protected canary artifacts.

This command never launches an Agent, creates a fault, or publishes capabilities.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stage2_service.capability_qualification import evaluate_wp8_artifacts
from stage2_service.gateway_config import GatewayConfigSnapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--artifact", action="append", required=True,
                        help="Relative evidence path; repeat for every required artifact.")
    parser.add_argument("--gateway-config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        gateway = GatewayConfigSnapshot.from_file(args.gateway_config, required_aliases=())
        record = evaluate_wp8_artifacts(args.artifact, artifact_root=args.artifact_root, gateway=gateway)
        destination = args.output.absolute()
        if any(part.is_symlink() for part in (destination, *destination.parents)):
            raise ValueError("qualification output must not contain symbolic links")
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if destination.parent.stat().st_mode & 0o022:
            raise ValueError("qualification output directory must not be group/world writable")
        with open(destination, "x", encoding="utf-8",
                  opener=lambda path, flags: os.open(path, flags | os.O_NOFOLLOW, 0o600)) as output:
            json.dump(record, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
    except (OSError, ValueError) as error:
        print(json.dumps({"status": "rejected", "error_type": type(error).__name__}))
        return 1
    print(json.dumps({"status": record["status"], "output": str(destination),
                      "failure_reasons": record["failure_reasons"]}, ensure_ascii=False))
    return 0 if record["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
