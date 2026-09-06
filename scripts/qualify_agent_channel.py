#!/usr/bin/env python3
"""Run WP11 no-fault native Harness channel qualification."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from stage2_service.channel_qualification import (
    ALL_CHANNEL_HARNESSES,
    ChannelQualificationRunner,
    collective_equality_check,
    write_collective_check,
)
from stage2_service.contracts import HarnessKind
from stage2_service.episode import load_fixed_episode
from stage2_service.matrix import fixed_otel_episode_ref
from stage2_service.runtime_factory import Stage2RuntimeConfig, Stage2System


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Qualify real native Harness MCP channel behavior without creating chaos faults.",
    )
    parser.add_argument("--model", required=True, help="Model alias passed to every selected Harness.")
    parser.add_argument(
        "--harness",
        action="append",
        choices=[item.value for item in ALL_CHANNEL_HARNESSES],
        help="Harness to qualify. Repeat to run multiple; omitted means all four.",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="Directory where per-Harness qualification JSON records are written.",
    )
    parser.add_argument(
        "--protected-root",
        type=Path,
        default=None,
        help="Expected Stage-2 private root. If supplied, it must match STAGE2_PRIVATE_ROOT.",
    )
    parser.add_argument(
        "--namespace",
        default="otel-demo",
        choices=["otel-demo"],
        help="Application namespace. Current qualification scope supports only otel-demo.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = Stage2RuntimeConfig.from_env()
    _validate_protected_root(config.private_root, args.protected_root)
    output_dir = _validate_output_dir(args.output_dir)
    harnesses = tuple(
        HarnessKind(value) for value in (args.harness or [item.value for item in ALL_CHANNEL_HARNESSES])
    )
    episode = load_fixed_episode(fixed_otel_episode_ref(config.repo_root), root=config.repo_root)
    runner = ChannelQualificationRunner(Stage2System(config), namespace=args.namespace)
    records = runner.run_all(
        episode=episode,
        model=args.model,
        harnesses=harnesses,
        output_dir=output_dir,
    )
    collective_path = output_dir / "channel-qualification-collective.json"
    if not collective_path.is_file():
        collective_path = write_collective_check(output_dir)
    result = {
        "records": [record.as_dict() for record in records],
        "collective": (
            json.loads(collective_path.read_text(encoding="utf-8"))
            if collective_path is not None
            else collective_equality_check(records)
        ),
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if all(record.passed for record in records) else 1


def _validate_protected_root(config_private_root: Path, supplied: Path | None) -> None:
    root = Path(supplied).resolve() if supplied is not None else config_private_root.resolve()
    if root != config_private_root.resolve():
        raise SystemExit(
            f"--protected-root {root} does not match STAGE2_PRIVATE_ROOT {config_private_root.resolve()}"
        )
    if root == Path("/") or root == Path.home().resolve():
        raise SystemExit("protected root must not be filesystem root or the user home")


def _validate_output_dir(path: Path) -> Path:
    _reject_symlink_path(path, "output directory")
    resolved = path.resolve()
    if resolved == Path("/") or resolved == Path.home().resolve():
        raise SystemExit("output directory must not be filesystem root or the user home")
    if resolved.exists() and resolved.is_symlink():
        raise SystemExit("output directory must not be a symlink")
    os.makedirs(resolved, mode=0o700, exist_ok=True)
    os.chmod(resolved, 0o700)
    return resolved


def _reject_symlink_path(path: Path, label: str) -> None:
    candidate = path if path.is_absolute() else Path.cwd() / path
    current = candidate
    while True:
        if current.exists() or current.is_symlink():
            if current.is_symlink():
                raise SystemExit(f"{label} path must not contain symlinks: {current}")
        if current.parent == current:
            break
        current = current.parent


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
