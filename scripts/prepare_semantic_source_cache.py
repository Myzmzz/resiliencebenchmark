#!/usr/bin/env python3
"""Create a verified OTel Demo source-cache bundle from an existing clone."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts.run_semantic_pipeline import (
    DEFAULT_REPOSITORY_URL,
    DEFAULT_REVISION,
    EXPECTED_OTEL_COMMIT,
    _write_source_cache_bundle,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--repository-url", default=DEFAULT_REPOSITORY_URL)
    parser.add_argument("--revision", default=DEFAULT_REVISION)
    parser.add_argument("--expected-commit", default=EXPECTED_OTEL_COMMIT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        archive, manifest = _write_source_cache_bundle(
            args.source.expanduser().resolve(),
            args.cache_dir.expanduser().resolve(),
            repository_url=args.repository_url,
            revision=args.revision,
            expected_commit=args.expected_commit,
        )
    except Exception as exc:  # noqa: BLE001 - CLI emits a bounded failure record.
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:2000],
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 2
    print(
        json.dumps(
            {
                "status": "completed",
                "archive": str(archive),
                "manifest": str(manifest),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
