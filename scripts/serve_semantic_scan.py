#!/usr/bin/env python3
"""Small host-local control API for the semantic scan pipeline."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUN_ROOT = Path("/data/mj/resbench-runs")
DEFAULT_SYSTEM_ROOT = Path("/data/mj/resbench-system")
DEFAULT_REPOSITORY_URL = "https://github.com/open-telemetry/opentelemetry-demo.git"
DEFAULT_REVISION = "2.2.0"
DEFAULT_EXPECTED_COMMIT = "b74a7bc7bbe66099c61951f42b24dab8b6f02d18"
STATE_FILE_NAME = "semantic-scan-service-state.json"


class StartRequest(BaseModel):
    execute: bool = False
    qualification_only: bool = False
    run_id: str | None = Field(default=None, pattern=r"^semantic-[a-z0-9-]{8,80}$")
    namespace: str = Field(
        default="otel-demo", pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_id() -> str:
    now = datetime.now(timezone.utc)
    digest = hashlib.sha256(now.isoformat().encode()).hexdigest()[:8]
    return f"semantic-{now:%Y%m%dt%H%M%sz}-{digest}"


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else None


def _safe_run_dir(run_root: Path, run_id: str) -> Path:
    root = run_root.expanduser().resolve()
    path = (root / run_id).resolve()
    if path == Path("/") or root not in path.parents:
        raise HTTPException(status_code=400, detail="unsafe run directory")
    return path


def build_app(
    *,
    run_root: Path,
    system_root: Path,
    repository_url: str,
    revision: str,
    expected_commit: str,
    codegraph_command: str,
    kubeconfig: Path,
    kubeconfig_ref: str,
) -> FastAPI:
    app = FastAPI(title="ResBench Semantic Scan Control API", version="1.0")
    state_file = system_root / "state" / STATE_FILE_NAME
    process_lock = threading.Lock()
    process_holder: dict[str, subprocess.Popen[bytes] | None] = {"process": None}
    stale = _read_json(state_file)
    if stale and stale.get("status") == "running":
        _write_json(
            state_file,
            {
                **stale,
                "status": "interrupted_by_service_restart",
                "finished_at": _utc_now(),
            },
        )

    def watch_process(
        process: subprocess.Popen[bytes], run_state: dict[str, Any]
    ) -> None:
        return_code = process.wait()
        with process_lock:
            if process_holder["process"] is process:
                process_holder["process"] = None
            _write_json(
                state_file,
                {
                    **run_state,
                    "status": "completed" if return_code == 0 else "failed",
                    "exit_code": return_code,
                    "finished_at": _utc_now(),
                },
            )

    @app.get("/health")
    def health() -> dict[str, Any]:
        state = _read_json(state_file)
        with process_lock:
            process = process_holder["process"]
            active = bool(process is not None and process.poll() is None)
        return {"status": "ok", "active_run": state.get("run_id") if active and state else None}

    @app.post("/runs/start")
    def start(request: StartRequest) -> dict[str, Any]:
        with process_lock:
            active_process = process_holder["process"]
            if active_process is not None and active_process.poll() is None:
                state = _read_json(state_file) or {}
                raise HTTPException(
                    status_code=409,
                    detail={
                        "active_run": state.get("run_id"),
                        "pid": active_process.pid,
                    },
                )

        selected_run_id = request.run_id or _run_id()
        argv = [
            sys.executable,
            str(REPO_ROOT / "scripts/run_semantic_pipeline.py"),
            "--run-id",
            selected_run_id,
            "--run-root",
            str(run_root),
            "--system-root",
            str(system_root),
            "--repository-url",
            repository_url,
            "--revision",
            revision,
            "--expected-commit",
            expected_commit,
            "--namespace",
            request.namespace,
            "--codegraph-command",
            codegraph_command,
            "--kubeconfig",
            str(kubeconfig),
            "--kubeconfig-ref",
            kubeconfig_ref,
        ]
        if request.execute:
            argv.append("--execute")
        if request.qualification_only:
            argv.append("--qualification-only")
        if not request.execute:
            completed = subprocess.run(
                argv,
                cwd=str(REPO_ROOT),
                text=True,
                capture_output=True,
                check=False,
            )
            if completed.returncode != 0:
                raise HTTPException(status_code=400, detail=completed.stdout[-2000:] or completed.stderr[-1000:])
            return json.loads(completed.stdout)

        service_log_dir = system_root / "logs"
        service_log_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = service_log_dir / f"{selected_run_id}.stdout.log"
        stderr_path = service_log_dir / f"{selected_run_id}.stderr.log"
        stdout = stdout_path.open("ab")
        stderr = stderr_path.open("ab")
        process = subprocess.Popen(
            argv,
            cwd=str(REPO_ROOT),
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
            close_fds=True,
        )
        stdout.close()
        stderr.close()
        run_state = {
            "schema_version": "resbench-semantic-scan-service-state.v1",
            "run_id": selected_run_id,
            "pid": process.pid,
            "started_at": _utc_now(),
            "mode": "execute",
            "run_root": str(run_root),
            "stdout": str(stdout_path),
            "stderr": str(stderr_path),
            "status": "running",
        }
        _write_json(state_file, run_state)
        with process_lock:
            process_holder["process"] = process
        threading.Thread(
            target=watch_process,
            args=(process, run_state),
            name=f"semantic-scan-watch-{selected_run_id}",
            daemon=True,
        ).start()
        return {"status": "started", **run_state}

    @app.get("/runs/status")
    def status() -> dict[str, Any]:
        state = _read_json(state_file)
        if not state:
            return {"status": "idle"}
        with process_lock:
            process = process_holder["process"]
            running = bool(process is not None and process.poll() is None)
        result = {
            **state,
            "status": "running" if running else state.get("status", "idle"),
        }
        run_id = state.get("run_id")
        if run_id:
            report = _safe_run_dir(run_root, run_id) / "pipeline-report.json"
            if report.is_file():
                result["pipeline_report"] = str(report)
                result["pipeline_status"] = _read_json(report).get("status")
        return result

    @app.post("/runs/cancel")
    def cancel() -> dict[str, Any]:
        state = _read_json(state_file) or {}
        with process_lock:
            process = process_holder["process"]
        if process is None or process.poll() is not None:
            return {"status": "not_running", **state}
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return {"status": "not_running", **state}
        updated = {**state, "cancel_requested_at": _utc_now()}
        _write_json(state_file, updated)
        return {"status": "cancel_requested", **updated}

    @app.get("/runs/{run_id}/artifacts")
    def artifacts(run_id: str) -> dict[str, Any]:
        run_dir = _safe_run_dir(run_root, run_id)
        if not run_dir.exists():
            raise HTTPException(status_code=404, detail="run not found")
        files = [
            {"path": str(path), "bytes": path.stat().st_size}
            for path in sorted(run_dir.rglob("*"))
            if path.is_file()
        ]
        return {"run_id": run_id, "run_dir": str(run_dir), "files": files}

    return app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18085)
    parser.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    parser.add_argument("--system-root", type=Path, default=DEFAULT_SYSTEM_ROOT)
    parser.add_argument("--repository-url", default=DEFAULT_REPOSITORY_URL)
    parser.add_argument("--revision", default=DEFAULT_REVISION)
    parser.add_argument("--expected-commit", default=DEFAULT_EXPECTED_COMMIT)
    parser.add_argument(
        "--codegraph-command",
        default=os.environ.get(
            "RESBENCH_CODEGRAPH_COMMAND",
            "/data/mj/resbench-tools/node_modules/.bin/codegraph",
        ),
    )
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument(
        "--kubeconfig-ref",
        default="/data/mj/resbench-system/kubeconfig",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.host not in {"127.0.0.1", "::1"}:
        raise SystemExit("semantic scan control API must bind to loopback")
    import uvicorn

    app = build_app(
        run_root=args.run_root,
        system_root=args.system_root,
        repository_url=args.repository_url,
        revision=args.revision,
        expected_commit=args.expected_commit,
        codegraph_command=args.codegraph_command,
        kubeconfig=args.kubeconfig,
        kubeconfig_ref=args.kubeconfig_ref,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
