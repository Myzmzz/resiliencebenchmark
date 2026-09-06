"""Trusted single-threaded initializer; config arrives only over an inherited FD."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace


MAX_CONFIG_BYTES = 1_048_576


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or not args[0].isdigit() or int(args[0]) < 3:
        return 126
    descriptor = int(args[0])
    try:
        with os.fdopen(descriptor, "rb") as pipe:
            raw = pipe.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            raise ValueError("initializer config exceeds limit")
        config = json.loads(raw)
        if os.geteuid() != 0 or not isinstance(config, dict):
            raise ValueError("initializer requires trusted daemon identity")
        # -I ignores Agent PYTHONPATH and writable cwd. This immutable image
        # path is the sole additional import root for the initializer.
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from harness.agent_exec.server import _initialize_child

        sandbox = config["sandbox"]
        uid = config["sandbox_uid"] if sandbox else config["agent_uid"]
        if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
            raise ValueError("initializer target identity is invalid")
        _initialize_child(
            SimpleNamespace(**{key: config[key] for key in ("agent_uid", "agent_gid", "sandbox_uid", "sandbox_gid")}),
            sandbox, Path(config["sandbox_tmp"]) if config["sandbox_tmp"] else None,
            Path(config["cgroup"]),
        )
        os.execvpe(config["argv"][0], config["argv"], config["env"])
    except Exception as exc:
        print(f"[agent-exec-init] {type(exc).__name__}: initialization failed", file=sys.stderr)
        return 126
    return 126  # exec never returns on success


if __name__ == "__main__":
    raise SystemExit(main())
