#!/usr/bin/env python3
"""Maintain the live process lease required by host-managed chaos_control writes."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import stat
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

CONTROLLER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")
DEFAULT_LEASE = Path(
    "/var/lib/resiliencebenchmark/chaos-control/controller/controller-lease.json"
)


def write_lease(
    path: Path,
    *,
    controller_id: str,
    pid: int,
    ttl_seconds: int,
) -> None:
    if not CONTROLLER_ID.fullmatch(controller_id):
        raise ValueError("invalid controller id")
    if pid <= 1:
        raise ValueError("controller pid must identify a live non-init process")
    if not 30 <= ttl_seconds <= 300:
        raise ValueError("lease ttl must be between 30 and 300 seconds")
    parent = path.parent.resolve()
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(parent, 0o700)
    info = parent.lstat()
    if stat.S_ISLNK(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("controller lease directory must be a real mode-0700 directory")
    now = datetime.now(timezone.utc)
    payload = {
        "schema_version": "local-controller-lease.v1",
        "controller_id": controller_id,
        "pid": pid,
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
    }
    descriptor, raw_path = tempfile.mkstemp(prefix=".controller-lease-", dir=parent)
    temporary = Path(raw_path)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lease-file", type=Path, default=DEFAULT_LEASE)
    parser.add_argument("--controller-id", default="manual-codex-controller")
    parser.add_argument("--renew-seconds", type=int, default=30)
    parser.add_argument("--ttl-seconds", type=int, default=90)
    parser.add_argument("--once", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 5 <= args.renew_seconds < args.ttl_seconds:
        raise SystemExit("renew-seconds must be at least 5 and less than ttl-seconds")
    lease_file = args.lease_file.expanduser().resolve()
    stopped = False

    def stop(*_args: object) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopped:
        write_lease(
            lease_file,
            controller_id=args.controller_id,
            pid=os.getpid(),
            ttl_seconds=args.ttl_seconds,
        )
        if args.once:
            break
        deadline = time.monotonic() + args.renew_seconds
        while not stopped and time.monotonic() < deadline:
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
