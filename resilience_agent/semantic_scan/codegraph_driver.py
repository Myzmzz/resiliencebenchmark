"""Controller-owned CodeGraph indexing and bounded read-only queries."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
from collections.abc import Callable, Sequence
from copy import deepcopy
from typing import Any

from .config import CodebaseConfig, CodeGraphConfig
from .contracts import CodeGraphManifest


class CodeGraphError(RuntimeError):
    pass


Runner = Callable[[Sequence[str], int], str]


def _default_runner(argv: Sequence[str], timeout: int) -> str:
    completed = subprocess.run(
        list(argv),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={
            "HOME": os.environ.get("HOME", ""),
            "PATH": os.environ.get("PATH", ""),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
        },
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()[:2000]
        raise CodeGraphError(
            f"CodeGraph command failed ({completed.returncode}): {detail}"
        )
    return completed.stdout


class CodeGraphDriver:
    """The Controller mutates the index; agents receive query methods only."""

    def __init__(
        self,
        codebase: CodebaseConfig,
        config: CodeGraphConfig,
        *,
        runner: Runner | None = None,
    ):
        self.codebase = codebase
        self.config = config
        self.runner = runner or _default_runner
        resolved = shutil.which(config.command)
        if runner is None and not resolved:
            raise CodeGraphError(f"CodeGraph command is unavailable: {config.command}")
        self.command = resolved or config.command
        self._manifest: CodeGraphManifest | None = None
        self._command_lock = threading.Lock()
        self._cache_lock = threading.RLock()
        self._query_cache: dict[tuple[str, ...], Any] = {}

    @property
    def manifest(self) -> CodeGraphManifest:
        if self._manifest is None:
            raise CodeGraphError("CodeGraph index has not been qualified")
        return self._manifest

    def ensure_index(self) -> CodeGraphManifest:
        status = self._status()
        if not status.get("initialized"):
            self._run("init", str(self.codebase.path))
            self._run("index", str(self.codebase.path), "--quiet")
        elif self.config.force_reindex:
            self._run("index", str(self.codebase.path), "--force", "--quiet")
        else:
            pending = status.get("pendingChanges")
            if isinstance(pending, dict) and any(int(value or 0) for value in pending.values()):
                self._run("sync", str(self.codebase.path))
        status = self._status()
        if not status.get("initialized") or int(status.get("nodeCount") or 0) <= 0:
            raise CodeGraphError("CodeGraph index did not produce any nodes")
        version = self._run("--version").strip()
        self._manifest = CodeGraphManifest(
            codegraph_version=version,
            codebase_path=str(self.codebase.path),
            source_identity=self.codebase.source_identity,
            initialized=True,
            file_count=int(status.get("fileCount") or 0),
            node_count=int(status.get("nodeCount") or 0),
            edge_count=int(status.get("edgeCount") or 0),
            languages=[str(item) for item in status.get("languages", [])],
            index_sha256=self._index_digest(status, version),
            status=status,
            parse_failures=self._extract_parse_failures(status),
            coverage=self._extract_coverage(status),
        )
        return self._manifest

    def query(self, search: str, *, kind: str | None = None, limit: int | None = None) -> Any:
        if not search or len(search) > 300:
            raise CodeGraphError("CodeGraph search must contain 1-300 characters")
        argv = [
            "query",
            search,
            "--path",
            str(self.codebase.path),
            "--limit",
            str(limit or self.config.max_query_results),
            "--json",
        ]
        if kind:
            argv.extend(["--kind", kind])
        return self._cached_json(*argv)

    def callers(self, symbol: str, *, limit: int | None = None) -> Any:
        return self._relationship("callers", symbol, limit)

    def callees(self, symbol: str, *, limit: int | None = None) -> Any:
        return self._relationship("callees", symbol, limit)

    def context(
        self,
        task: str,
        *,
        max_nodes: int | None = None,
        max_code_blocks: int | None = None,
    ) -> Any:
        if not task or len(task) > 4000:
            raise CodeGraphError("CodeGraph context task must contain 1-4000 characters")
        return self._cached_json(
            "context",
            task,
            "--path",
            str(self.codebase.path),
            "--max-nodes",
            str(max_nodes or self.config.max_nodes),
            "--max-code",
            str(
                self.config.max_code_blocks
                if max_code_blocks is None
                else max_code_blocks
            ),
            "--format",
            "json",
        )

    def _relationship(self, command: str, symbol: str, limit: int | None) -> Any:
        if not symbol or len(symbol) > 300:
            raise CodeGraphError("CodeGraph symbol must contain 1-300 characters")
        return self._cached_json(
            command,
            symbol,
            "--path",
            str(self.codebase.path),
            "--limit",
            str(limit or self.config.max_query_results),
            "--json",
        )

    def _status(self) -> dict[str, Any]:
        value = self._json("status", str(self.codebase.path), "--json")
        if not isinstance(value, dict):
            raise CodeGraphError("CodeGraph status is not a JSON object")
        return value

    def _run(self, *args: str) -> str:
        with self._command_lock:
            return self.runner([self.command, *args], self.config.timeout_seconds)

    def _json(self, *args: str) -> Any:
        raw = self._run(*args)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CodeGraphError(
                f"CodeGraph returned invalid JSON for {args[0]}"
            ) from exc

    def _cached_json(self, *args: str) -> Any:
        key = tuple(args)
        with self._cache_lock:
            if key in self._query_cache:
                return deepcopy(self._query_cache[key])
        value = self._json(*args)
        with self._cache_lock:
            cached = self._query_cache.setdefault(key, deepcopy(value))
            return deepcopy(cached)

    def _index_digest(self, status: dict[str, Any], version: str) -> str:
        digest = hashlib.sha256()
        digest.update(self.codebase.source_identity.encode())
        digest.update(version.encode())
        digest.update(json.dumps(status, sort_keys=True, separators=(",", ":")).encode())
        index_root = self.codebase.path / ".codegraph"
        if index_root.is_dir():
            for path in sorted(index_root.rglob("*")):
                if not path.is_file() or path.name.endswith(("-wal", "-shm")):
                    continue
                digest.update(path.relative_to(index_root).as_posix().encode())
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _extract_parse_failures(status: dict[str, Any]) -> list[dict[str, Any]]:
        for key in ("parseFailures", "parse_failures", "failedFiles", "failed_files"):
            value = status.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []

    @staticmethod
    def _extract_coverage(status: dict[str, Any]) -> dict[str, Any]:
        coverage: dict[str, Any] = {}
        for key in (
            "fileCount",
            "nodeCount",
            "edgeCount",
            "languages",
            "unsupportedLanguages",
            "unsupported_languages",
            "skippedFiles",
            "skipped_files",
        ):
            if key in status:
                coverage[key] = status[key]
        return coverage
