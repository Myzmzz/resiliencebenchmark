#!/usr/bin/env python3
"""Serve sealed Stage-2 evidence and the built review UI without execution rights."""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from stage2_service.api import CampaignSupervisor, create_app


class ReadOnlyRunner:
    def run(self, request):
        raise RuntimeError("the local matrix review server is read-only")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--frontend-root", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18088)
    args = parser.parse_args()
    app = create_app(
        CampaignSupervisor(ReadOnlyRunner()),
        artifact_root=args.artifact_root.resolve(),
        frontend_root=args.frontend_root.resolve(),
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
