#!/usr/bin/env python3
"""Publish verified base-channel evidence for the existing task/D0 preflight."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stage2_service.capability_qualification import publish_capabilities
from stage2_service.gateway_config import GatewayConfigSnapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", type=Path, action="append", required=True,
                        help="Base qualification record; repeat for every Harness to include.")
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--gateway-config", type=Path, required=True,
                        help="Current Controller-mounted LiteLLM route configuration.")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        gateway = GatewayConfigSnapshot.from_file(args.gateway_config, required_aliases=())
        output = publish_capabilities(args.record, artifact_root=args.artifact_root, output=args.output,
                                      gateway=gateway)
    except ValueError as error:
        print(json.dumps({"status": "rejected", "reason": str(error)}))
        return 1
    print(json.dumps({"status": "published", "output": str(output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
