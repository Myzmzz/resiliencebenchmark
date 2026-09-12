#!/usr/bin/env python3
"""CLI wrapper around the Fleet API, for people who would rather not write JSON.

Every subcommand maps to one endpoint. Anything that can change the cluster is
a dry run unless ``--execute`` is given, and deletions additionally need
``--confirm <namespace>``.

    python scripts/fleet_ctl.py --base http://127.0.0.1:28090 status
    python scripts/fleet_ctl.py config --replicas 5 --controller-image ... --execute
    python scripts/fleet_ctl.py provision            # dry run
    python scripts/fleet_ctl.py provision --execute
    python scripts/fleet_ctl.py batch round.json     # dry run, prints the schedule
    python scripts/fleet_ctl.py results dx-parallel-20260912-01 --format csv
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_BASE = "http://127.0.0.1:28090"


def call(base: str, method: str, path: str, body: Any = None, *, raw: bool = False) -> Any:
    url = base.rstrip("/") + path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
            payload = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print(f"HTTP {exc.code}: {detail}", file=sys.stderr)
        raise SystemExit(1) from exc
    except Exception as exc:  # noqa: BLE001
        print(f"request failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    if raw:
        return payload.decode("utf-8", errors="replace")
    return json.loads(payload) if payload else {}


def emit(value: Any) -> None:
    if isinstance(value, str):
        print(value)
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default=DEFAULT_BASE, help=f"Fleet base URL (default {DEFAULT_BASE})")
    sub = parser.add_subparsers(dest="command", required=True)

    config = sub.add_parser("config", help="write the fleet configuration")
    config.add_argument("--from-file", type=Path, help="read the whole configuration document from JSON")
    config.add_argument("--replicas", type=int)
    config.add_argument("--namespace-prefix")
    config.add_argument("--controller-image")
    config.add_argument("--agent-image")
    config.add_argument("--litellm-image")
    config.add_argument("--coroot-project-id")
    config.add_argument("--node", action="append", dest="nodes", default=None)
    config.add_argument("--source-head")
    config.add_argument("--max-concurrency", type=int)
    config.add_argument("--execute", action="store_true", help="write it; without this the body is only printed")

    sub.add_parser("show-config", help="read the fleet configuration")

    provision = sub.add_parser("provision", help="align every slot with the configuration")
    provision.add_argument("--execute", action="store_true")
    provision.add_argument("--no-wait", action="store_true")

    slot = sub.add_parser("add-slot", help="provision one more slot")
    slot.add_argument("index", type=int)
    slot.add_argument("--execute", action="store_true")

    status = sub.add_parser("status", help="phase of every slot")
    status.add_argument("--refresh", action="store_true", help="re-read the cluster first")

    sub.add_parser("preflight", help="per-slot preflight, environment gate and gateway probe")

    prompt = sub.add_parser("prompt", help="what a slot renders for a level")
    prompt.add_argument("slot")
    prompt.add_argument("--level", default="L0")
    prompt.add_argument("--case", default="C0")

    reset = sub.add_parser("reset", help="reset one replica through its Controller")
    reset.add_argument("slot")

    drain = sub.add_parser("drain", help="stop giving a slot new work")
    drain.add_argument("slot")

    delete = sub.add_parser("delete-slot", help="reclaim one slot")
    delete.add_argument("slot")
    delete.add_argument("--confirm", required=True, help="the namespace being destroyed")
    delete.add_argument("--execute", action="store_true")

    batch = sub.add_parser("batch", help="submit a batch document")
    batch.add_argument("file", type=Path)
    batch.add_argument("--execute", action="store_true")

    show = sub.add_parser("batch-status", help="state of every item in a batch")
    show.add_argument("batch_id")

    results = sub.add_parser("results", help="result matrix of a batch")
    results.add_argument("batch_id")
    results.add_argument("--format", choices=("json", "csv"), default="json")

    stop = sub.add_parser("stop", help="stop a batch")
    stop.add_argument("batch_id")
    stop.add_argument("--reason", default="operator stop requested")

    artifacts = sub.add_parser("artifacts", help="where one item's evidence lives")
    artifacts.add_argument("batch_id")
    artifacts.add_argument("item_id")

    audit = sub.add_parser("audit", help="destructive-operation audit log")
    audit.add_argument("--limit", type=int, default=50)
    return parser


def config_body(args: argparse.Namespace) -> dict[str, Any]:
    if args.from_file:
        return json.loads(args.from_file.read_text(encoding="utf-8"))
    body: dict[str, Any] = {}
    for key in ("replicas", "namespace_prefix", "controller_image", "agent_image",
                "litellm_image", "coroot_project_id", "source_head", "max_concurrency"):
        value = getattr(args, key, None)
        if value is not None:
            body[key] = value
    if args.nodes:
        body["nodes"] = args.nodes
    return body


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base = args.base
    command = args.command
    if command == "config":
        body = config_body(args)
        if not args.execute:
            print("dry run; re-run with --execute to write this configuration:", file=sys.stderr)
            emit(body)
            return 0
        emit(call(base, "PUT", "/api/v1/fleet/config", body))
    elif command == "show-config":
        emit(call(base, "GET", "/api/v1/fleet/config"))
    elif command == "provision":
        query = f"?dry_run={'false' if args.execute else 'true'}&wait={'false' if args.no_wait else 'true'}"
        emit(call(base, "POST", "/api/v1/fleet/provision" + query))
    elif command == "add-slot":
        query = f"?dry_run={'false' if args.execute else 'true'}"
        emit(call(base, "POST", "/api/v1/fleet/slots" + query, {"index": args.index}))
    elif command == "status":
        emit(call(base, "GET", f"/api/v1/fleet/status?refresh={'true' if args.refresh else 'false'}"))
    elif command == "preflight":
        emit(call(base, "GET", "/api/v1/fleet/preflight"))
    elif command == "prompt":
        emit(call(base, "GET", f"/api/v1/fleet/slots/{args.slot}/prompt?level={args.level}&case={args.case}"))
    elif command == "reset":
        emit(call(base, "POST", f"/api/v1/fleet/slots/{args.slot}/reset"))
    elif command == "drain":
        emit(call(base, "POST", f"/api/v1/fleet/slots/{args.slot}/drain"))
    elif command == "delete-slot":
        query = f"?confirm={args.confirm}&dry_run={'false' if args.execute else 'true'}"
        emit(call(base, "DELETE", f"/api/v1/fleet/slots/{args.slot}" + query))
    elif command == "batch":
        document = json.loads(args.file.read_text(encoding="utf-8"))
        query = f"?dry_run={'false' if args.execute else 'true'}"
        emit(call(base, "POST", "/api/v1/fleet/batches" + query, document))
    elif command == "batch-status":
        emit(call(base, "GET", f"/api/v1/fleet/batches/{args.batch_id}"))
    elif command == "results":
        emit(call(base, "GET", f"/api/v1/fleet/batches/{args.batch_id}/results?format={args.format}",
                  raw=args.format == "csv"))
    elif command == "stop":
        emit(call(base, "POST", f"/api/v1/fleet/batches/{args.batch_id}/stop", {"reason": args.reason}))
    elif command == "artifacts":
        emit(call(base, "GET", f"/api/v1/fleet/batches/{args.batch_id}/items/{args.item_id}/artifacts"))
    elif command == "audit":
        emit(call(base, "GET", f"/api/v1/fleet/audit?limit={args.limit}"))
    else:  # pragma: no cover - argparse rejects anything else
        raise SystemExit(2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
