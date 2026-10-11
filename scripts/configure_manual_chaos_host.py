#!/usr/bin/env python3
"""Atomically enable host chaos_control behind the manual controller lease."""

from __future__ import annotations

import argparse
import grp
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")
SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#@-]{2,255}$")
DEFAULT_ENV_FILE = Path("/etc/resiliencebenchmark/mcp/chaos_control.env")
DEFAULT_LEASE_FILE = Path(
    "/var/lib/resiliencebenchmark/chaos-control/controller/controller-lease.json"
)


def quote_env(value: str) -> str:
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("environment value contains control characters")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def update_environment(
    path: Path,
    *,
    controller_id: str,
    controller_token_ref: str,
    lease_file: Path,
) -> Path:
    if not SAFE_ID.fullmatch(controller_id):
        raise ValueError("invalid controller id")
    if not SAFE_REF.fullmatch(controller_token_ref):
        raise ValueError("invalid controller token reference")
    source = path.resolve()
    if not source.is_file() or source.is_symlink():
        raise ValueError("chaos_control env must be a regular file")
    text = source.read_text(encoding="utf-8")
    updates = {
        "RESBENCH_CHAOS_EXECUTE_ENABLED": "true",
        "RESBENCH_CHAOS_CONTROLLER_TOKEN_REF": controller_token_ref,
        "RESBENCH_CHAOS_CONTROLLER_POD_UID": controller_id,
        "RESBENCH_CHAOS_CONTROLLER_POD_NAMESPACE": "",
        "RESBENCH_CHAOS_CONTROLLER_POD_NAME": "",
        "RESBENCH_CHAOS_CONTROLLER_LEASE_FILE": str(lease_file.resolve()),
    }
    for key, value in updates.items():
        replacement = f"{key}={quote_env(value)}"
        pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
        matches = pattern.findall(text)
        if len(matches) > 1:
            raise ValueError(f"duplicate runtime key: {key}")
        if matches:
            text = pattern.sub(replacement, text)
        else:
            text = text.rstrip("\n") + "\n" + replacement + "\n"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = source.with_name(f"{source.name}.backup-{stamp}")
    shutil.copy2(source, backup)
    descriptor, raw_path = tempfile.mkstemp(prefix=f".{source.name}.", dir=source.parent)
    temporary = Path(raw_path)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chown(temporary, 0, grp.getgrnam("resbench-chaos-control").gr_gid)
        os.chmod(temporary, 0o640)
        os.replace(temporary, source)
    finally:
        temporary.unlink(missing_ok=True)
    return backup


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--controller-id", default="manual-codex-controller")
    parser.add_argument(
        "--controller-token-ref",
        default="runtime://manual-codex-controller/token",
    )
    parser.add_argument("--lease-file", type=Path, default=DEFAULT_LEASE_FILE)
    return parser


def main(argv: list[str] | None = None) -> int:
    if os.geteuid() != 0:
        raise SystemExit("configuration requires root")
    args = build_parser().parse_args(argv)
    backup = update_environment(
        args.env_file,
        controller_id=args.controller_id,
        controller_token_ref=args.controller_token_ref,
        lease_file=args.lease_file,
    )
    print(
        "configured host chaos_control with a live controller lease; "
        f"backup={backup.name}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
