from __future__ import annotations

import os
import ipaddress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse, urlunparse

import yaml


DEFAULT_LOCKFILE = Path("environment/shared/source-locks.yaml")
SOURCE_ROOT_ENV = "RESBENCH_SOURCE_ROOT"
ALLOWED_APPLICATIONS_ENV = "RESBENCH_SOURCE_ALLOWED_APPLICATIONS"
ALLOWED_REPOSITORIES_ENV = "RESBENCH_SOURCE_ALLOWED_REPOSITORIES"
MAX_FILE_BYTES = 1_000_000
MAX_OUTPUT_CHARS = 25_000
MAX_PAGE_LIMIT = 500
MAX_REPOSITORY_PAGE_LIMIT = 100
DEFAULT_PAGE_LIMIT = 100
MAX_LIST_SCAN_ENTRIES = 10_000
MAX_SEARCH_SCAN_FILES = 2_000
MAX_SEARCH_SCAN_BYTES = 20_000_000
DENIED_PARTS = {
    ".git",
    ".hg",
    ".svn",
    "ground-truth",
    "ground-truth-private",
    "ground_truth",
    "ground_truth_private",
    "hidden-patch",
    "hidden-patches",
    "hidden_patch",
    "hidden_patches",
    "oracle-private",
    "oracle_private",
    "evaluator-private",
    "evaluator_private",
}
DENIED_SUFFIXES = (".hidden.patch", ".secret.patch", ".ground-truth.patch")
TEXT_SAMPLE_BYTES = 4096


class SourceROError(ValueError):
    """Structured user-facing error for the source_ro tools."""

    def __init__(self, code: str, message: str, action: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.action = action

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "action": self.action}


@dataclass(frozen=True)
class SourceLock:
    id: str
    application: str
    remote: str
    commit: str
    archive_sha256: str
    component: str | None = None
    tag: str | None = None
    agent_mount_path: str | None = None
    runtime_mapping_status: str | None = None
    note: str | None = None

    @classmethod
    def from_raw(cls, raw: dict[str, Any], index: int) -> "SourceLock":
        missing = [key for key in ("id", "application", "remote", "commit", "archiveSha256") if not raw.get(key)]
        if missing:
            raise SourceROError(
                "invalid_lockfile",
                f"source lock #{index} is missing required field(s): {', '.join(missing)}",
                "Fix environment/shared/source-locks.yaml before starting source_ro.",
            )
        return cls(
            id=str(raw["id"]),
            application=str(raw["application"]),
            component=str(raw["component"]) if raw.get("component") else None,
            remote=str(raw["remote"]),
            tag=str(raw["tag"]) if raw.get("tag") else None,
            commit=str(raw["commit"]),
            archive_sha256=str(raw["archiveSha256"]),
            agent_mount_path=str(raw["agentMountPath"]) if raw.get("agentMountPath") else None,
            runtime_mapping_status=str(raw["runtimeMappingStatus"]) if raw.get("runtimeMappingStatus") else None,
            note=str(raw["note"]) if raw.get("note") else None,
        )


def envelope(payload: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, **payload}


def error_envelope(exc: SourceROError) -> dict[str, Any]:
    return {"ok": False, "error": exc.to_dict()}


def scan_warning(kind: str, message: str, action: str) -> dict[str, str]:
    return {"code": kind, "message": message, "action": action}


def parse_csv_allowlist(value: str | None) -> set[str]:
    if value is None:
        return set()
    return {item.strip() for item in value.split(",") if item.strip()}


def _format_scope(values: set[str]) -> list[str]:
    return sorted(values)


def _bounded_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_PAGE_LIMIT
    if limit < 1:
        raise SourceROError("invalid_pagination", "limit must be at least 1.", "Use a positive limit value.")
    return min(limit, MAX_PAGE_LIMIT)


def _bounded_offset(offset: int | None) -> int:
    if offset is None:
        return 0
    if offset < 0:
        raise SourceROError("invalid_pagination", "offset must not be negative.", "Use offset >= 0.")
    return offset


def _markdown_table(headers: list[str], rows: Iterable[list[Any]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        escaped = [str(cell).replace("|", "\\|").replace("\n", " ") for cell in row]
        lines.append("| " + " | ".join(escaped) + " |")
    return "\n".join(lines)


def _validate_format(output_format: str) -> str:
    normalized = output_format.lower()
    if normalized not in {"json", "markdown"}:
        raise SourceROError(
            "invalid_format",
            f"unsupported output format: {output_format}",
            "Use format='json' or format='markdown'.",
        )
    return normalized


def _is_local_or_private_host(hostname: str | None) -> bool:
    if not hostname:
        return True
    lowered = hostname.lower()
    if lowered in {"localhost", "local"} or lowered.endswith(".local"):
        return True
    try:
        address = ipaddress.ip_address(lowered)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local


def _redacted_remote(remote: str) -> str:
    parsed = urlparse(remote)
    if parsed.scheme not in {"http", "https"} or _is_local_or_private_host(parsed.hostname):
        return "<redacted-local-or-private-remote>"
    host = parsed.hostname or "<redacted-host>"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunparse((parsed.scheme, host, parsed.path, "", "", ""))


def _bounded_repository_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_PAGE_LIMIT
    if limit < 1:
        raise SourceROError("invalid_pagination", "limit must be at least 1.", "Use a positive limit value.")
    return min(limit, MAX_REPOSITORY_PAGE_LIMIT)


def _is_denied_part(part: str) -> bool:
    lower = part.lower()
    return lower in DENIED_PARTS or lower.endswith(DENIED_SUFFIXES)


def _check_relative_path(path: str) -> Path:
    if not path or path == ".":
        return Path(".")
    if "\x00" in path:
        raise SourceROError("unsafe_path", "paths must not contain NUL bytes.", "Pass a normal relative path.")
    if "\\" in path:
        raise SourceROError("unsafe_path", "backslash paths are not accepted.", "Use POSIX-style relative paths.")
    candidate = Path(path)
    if candidate.is_absolute():
        raise SourceROError("unsafe_path", "absolute paths are not allowed.", "Pass a path relative to the lock id root.")
    if any(part in {"..", ""} for part in candidate.parts):
        raise SourceROError("unsafe_path", "path traversal is not allowed.", "Remove '..' and repeated separators.")
    denied = [part for part in candidate.parts if _is_denied_part(part)]
    if denied:
        raise SourceROError(
            "restricted_path",
            f"restricted source path component: {denied[0]}",
            "Use application source paths only; private ground truth and VCS internals are intentionally hidden.",
        )
    return candidate


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_binary_file(path: Path) -> bool:
    with path.open("rb") as handle:
        sample = handle.read(TEXT_SAMPLE_BYTES)
    return b"\x00" in sample


def _safe_read_text(path: Path) -> str:
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise SourceROError(
            "file_too_large",
            f"file is {size} bytes, exceeding the {MAX_FILE_BYTES} byte source_ro limit.",
            "Use list_files or a narrower search path, or add a smaller purpose-built fixture.",
        )
    if _is_binary_file(path):
        raise SourceROError("binary_file", "binary files are not readable through source_ro.", "Select a text source file.")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise SourceROError(
            "unsupported_encoding",
            "file is not valid UTF-8 text.",
            "Use a UTF-8 source file or inspect it outside the benchmark agent surface.",
        ) from exc


class SourceROIndex:
    """Read-only source snapshot index backed by source-locks.yaml."""

    def __init__(
        self,
        *,
        source_root: str | Path | None = None,
        lockfile: str | Path = DEFAULT_LOCKFILE,
        allowed_applications: set[str] | None = None,
        allowed_repositories: set[str] | None = None,
        require_scope: bool = False,
    ) -> None:
        root_value = source_root if source_root is not None else os.environ.get(SOURCE_ROOT_ENV)
        if not root_value:
            raise SourceROError(
                "missing_source_root",
                f"{SOURCE_ROOT_ENV} is not set.",
                "Set RESBENCH_SOURCE_ROOT to the materialized source snapshot directory.",
            )
        self.source_root = Path(root_value).expanduser().resolve()
        self.lockfile = Path(lockfile)
        raw_locks = self._load_locks(self.lockfile)
        self.allowed_applications = set(allowed_applications or set())
        self.allowed_repositories = set(allowed_repositories or set())
        if require_scope and not self.allowed_applications:
            raise SourceROError(
                "missing_source_scope",
                f"{ALLOWED_APPLICATIONS_ENV} is required for source_ro runtime.",
                "Set exactly one episode application, for example RESBENCH_SOURCE_ALLOWED_APPLICATIONS=train-ticket.",
            )
        self.locks = self._apply_scope(raw_locks)
        self._lock_by_id = {lock.id: lock for lock in self.locks}

    @classmethod
    def from_environment(cls) -> "SourceROIndex":
        applications = parse_csv_allowlist(os.environ.get(ALLOWED_APPLICATIONS_ENV))
        if not applications:
            raise SourceROError(
                "missing_source_scope",
                f"{ALLOWED_APPLICATIONS_ENV} is required for source_ro runtime.",
                "Set exactly one episode application, for example RESBENCH_SOURCE_ALLOWED_APPLICATIONS=train-ticket.",
            )
        if len(applications) != 1:
            raise SourceROError(
                "invalid_source_scope",
                f"{ALLOWED_APPLICATIONS_ENV} must name exactly one episode application.",
                "Run one source_ro server per episode application; use RESBENCH_SOURCE_ALLOWED_REPOSITORIES to narrow repos.",
            )
        return cls(
            allowed_applications=applications,
            allowed_repositories=parse_csv_allowlist(os.environ.get(ALLOWED_REPOSITORIES_ENV)),
            require_scope=True,
        )

    def _load_locks(self, lockfile: Path) -> list[SourceLock]:
        if not lockfile.exists():
            raise SourceROError(
                "missing_lockfile",
                f"source lockfile does not exist: {lockfile}",
                "Run from the resiliencebenchmark repository root or pass a valid lockfile.",
            )
        with lockfile.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
        if not isinstance(data, dict):
            raise SourceROError(
                "invalid_lockfile",
                "source lockfile top-level document must be a mapping.",
                "Regenerate environment/shared/source-locks.yaml.",
            )
        raw_locks = data.get("spec", {}).get("locks")
        if not isinstance(raw_locks, list) or not raw_locks:
            raise SourceROError(
                "invalid_lockfile",
                "source lockfile must define a non-empty spec.locks list.",
                "Regenerate environment/shared/source-locks.yaml.",
            )
        locks = []
        for index, raw in enumerate(raw_locks):
            if not isinstance(raw, dict):
                raise SourceROError(
                    "invalid_lockfile",
                    f"source lock #{index} must be a mapping.",
                    "Fix environment/shared/source-locks.yaml before starting source_ro.",
                )
            locks.append(SourceLock.from_raw(raw, index))
        return locks

    def _apply_scope(self, locks: list[SourceLock]) -> list[SourceLock]:
        if not self.allowed_applications and not self.allowed_repositories:
            return locks

        scoped = [
            lock
            for lock in locks
            if (not self.allowed_applications or lock.application in self.allowed_applications)
            and (not self.allowed_repositories or lock.id in self.allowed_repositories)
        ]
        if self.allowed_applications and not any(lock.application in self.allowed_applications for lock in locks):
            raise SourceROError(
                "invalid_source_scope",
                f"allowed application scope matched no source locks: {', '.join(_format_scope(self.allowed_applications))}",
                "Use an application name from environment/shared/source-locks.yaml.",
            )
        if self.allowed_repositories:
            known_repo_ids = {lock.id for lock in locks}
            unknown = self.allowed_repositories - known_repo_ids
            if unknown:
                raise SourceROError(
                    "invalid_source_scope",
                    f"allowed repository scope contains unknown lock id(s): {', '.join(_format_scope(unknown))}",
                    "Use lock ids returned by source_list_repositories for this repository.",
                )
            out_of_application = {
                lock.id
                for lock in locks
                if lock.id in self.allowed_repositories
                and self.allowed_applications
                and lock.application not in self.allowed_applications
            }
            if out_of_application:
                raise SourceROError(
                    "invalid_source_scope",
                    (
                        "allowed repository scope includes repo(s) outside the allowed application: "
                        f"{', '.join(_format_scope(out_of_application))}"
                    ),
                    "Keep RESBENCH_SOURCE_ALLOWED_REPOSITORIES within the episode application.",
                )
        if not scoped:
            raise SourceROError(
                "invalid_source_scope",
                "source_ro allowlist matched no source repositories.",
                "Check RESBENCH_SOURCE_ALLOWED_APPLICATIONS and RESBENCH_SOURCE_ALLOWED_REPOSITORIES.",
            )
        return scoped

    def _lock(self, repo_id: str) -> SourceLock:
        lock = self._lock_by_id.get(repo_id)
        if lock is None:
            if self.allowed_applications or self.allowed_repositories:
                raise SourceROError(
                    "repository_out_of_scope",
                    f"source repository is outside this episode scope: {repo_id}",
                    "Call source_list_repositories and use only repositories returned for this episode.",
                )
            raise SourceROError(
                "unknown_repository",
                f"unknown source repository id: {repo_id}",
                "Call source_list_repositories and use one of the returned lock ids.",
            )
        return lock

    def _repo_root(self, repo_id: str) -> tuple[SourceLock, Path]:
        lock = self._lock(repo_id)
        root = (self.source_root / lock.id).resolve()
        if not _is_relative_to(root, self.source_root):
            raise SourceROError("unsafe_repository", "repository root escapes RESBENCH_SOURCE_ROOT.", "Check source locks.")
        if not root.is_dir():
            raise SourceROError(
                "missing_repository",
                f"materialized repository does not exist for lock id {repo_id}.",
                "Run scripts/materialize_sources.py with RESBENCH_SOURCE_ROOT before using source_ro.",
            )
        return lock, root

    def _resolve_path(self, repo_id: str, relative_path: str, *, require_file: bool = False) -> tuple[SourceLock, Path, str]:
        lock, root = self._repo_root(repo_id)
        safe_relative = _check_relative_path(relative_path)
        candidate = root / safe_relative
        if not candidate.exists():
            raise SourceROError(
                "missing_path",
                f"path does not exist in {repo_id}: {relative_path}",
                "Use source_list_files to find an existing relative path.",
            )
        resolved = candidate.resolve()
        if not _is_relative_to(resolved, root):
            raise SourceROError(
                "unsafe_symlink",
                f"path resolves outside the locked repository: {relative_path}",
                "Remove the symlink or select a path contained within the source snapshot.",
            )
        resolved_relative = resolved.relative_to(root).as_posix()
        _check_relative_path(resolved_relative)
        if require_file and not resolved.is_file():
            raise SourceROError("not_a_file", f"path is not a file: {relative_path}", "Select a readable source file.")
        return lock, resolved, resolved_relative or "."

    def list_repositories(
        self,
        *,
        application: str | None = None,
        component: str | None = None,
        limit: int | None = DEFAULT_PAGE_LIMIT,
        offset: int | None = 0,
        format: str = "json",
    ) -> dict[str, Any]:
        output_format = _validate_format(format)
        page_limit = _bounded_repository_limit(limit)
        page_offset = _bounded_offset(offset)
        repositories = []
        for lock in self.locks:
            if application is not None and lock.application != application:
                continue
            if component is not None and lock.component != component:
                continue
            materialized = (self.source_root / lock.id).is_dir()
            repositories.append(
                {
                    "id": lock.id,
                    "application": lock.application,
                    "component": lock.component,
                    "commit": lock.commit,
                    "tag": lock.tag,
                    "remote": _redacted_remote(lock.remote),
                    "archiveSha256": lock.archive_sha256,
                    "agentMountPath": lock.agent_mount_path,
                    "runtimeMappingStatus": lock.runtime_mapping_status,
                    "materialized": materialized,
                    "sourceRootRef": f"${{{SOURCE_ROOT_ENV}}}/{lock.id}",
                }
            )
        page = repositories[page_offset : page_offset + page_limit]
        next_offset = page_offset + len(page) if page_offset + len(page) < len(repositories) else None
        result = envelope(
            {
                "repositories": page,
                "count": len(page),
                "returned": len(page),
                "total": len(repositories),
                "limit": page_limit,
                "offset": page_offset,
                "hasMore": next_offset is not None,
                "nextOffset": next_offset,
                "filters": {"application": application, "component": component},
            }
        )
        if output_format == "markdown":
            result["markdown"] = _markdown_table(
                ["id", "application", "component", "commit", "materialized"],
                [
                    [repo["id"], repo["application"], repo["component"] or "", repo["commit"][:12], repo["materialized"]]
                    for repo in page
                ],
            )
        return result

    def _visible_child(self, root: Path, child: Path) -> tuple[Path, str] | None:
        try:
            resolved = child.resolve()
        except OSError:
            return None
        if not _is_relative_to(resolved, root):
            return None
        rel = resolved.relative_to(root).as_posix()
        try:
            _check_relative_path(rel)
        except SourceROError:
            return None
        return resolved, rel

    def _iter_files(self, root: Path, start: Path, recursive: bool) -> Iterable[tuple[Path, str]]:
        stack = [start]
        while stack:
            current = stack.pop()
            for child in sorted(current.iterdir(), key=lambda item: item.name):
                visible = self._visible_child(root, child)
                if visible is None:
                    continue
                resolved, rel = visible
                yield resolved, rel
                if recursive and resolved.is_dir() and not child.is_symlink():
                    stack.append(resolved)

    def list_files(
        self,
        repo_id: str,
        path: str = ".",
        *,
        recursive: bool = False,
        limit: int | None = DEFAULT_PAGE_LIMIT,
        offset: int | None = 0,
        format: str = "json",
    ) -> dict[str, Any]:
        output_format = _validate_format(format)
        _, root = self._repo_root(repo_id)
        _, start, resolved_path = self._resolve_path(repo_id, path)
        page_limit = _bounded_limit(limit)
        page_offset = _bounded_offset(offset)
        entries = []
        truncated = False
        scanned = 0
        entries_iter = [(start, resolved_path)] if start.is_file() else self._iter_files(root, start, recursive)
        for resolved, rel in entries_iter:
            if scanned >= MAX_LIST_SCAN_ENTRIES:
                truncated = True
                break
            scanned += 1
            stat = resolved.stat()
            if scanned > page_offset and len(entries) < page_limit:
                entries.append(
                    {
                        "path": rel,
                        "type": "directory" if resolved.is_dir() else "file",
                        "sizeBytes": stat.st_size if resolved.is_file() else None,
                        "isSymlink": (root / rel).is_symlink(),
                    }
                )
        has_next_in_scanned_window = scanned > page_offset + len(entries)
        next_offset = page_offset + len(entries) if has_next_in_scanned_window else None
        result = envelope(
            {
                "repository": repo_id,
                "path": resolved_path,
                "recursive": recursive,
                "limit": page_limit,
                "offset": page_offset,
                "returned": len(entries),
                "totalVisible": scanned,
                "totalVisibleIsLowerBound": truncated,
                "scannedEntries": scanned,
                "truncated": truncated,
                "entries": entries,
                "nextOffset": next_offset,
            }
        )
        if truncated:
            result["warning"] = scan_warning(
                "scan_limit_reached",
                f"file listing stopped after {MAX_LIST_SCAN_ENTRIES} visible entries.",
                "Use a narrower path, lower recursion depth by listing subdirectories first, or add a smaller fixture.",
            )
        if output_format == "markdown":
            result["markdown"] = _markdown_table(
                ["path", "type", "sizeBytes"],
                [[entry["path"], entry["type"], entry["sizeBytes"] or ""] for entry in entries],
            )
        return result

    def read_file(
        self,
        repo_id: str,
        path: str,
        *,
        start_line: int | None = None,
        max_chars: int = MAX_OUTPUT_CHARS,
        format: str = "json",
    ) -> dict[str, Any]:
        output_format = _validate_format(format)
        _, file_path, resolved_path = self._resolve_path(repo_id, path, require_file=True)
        if max_chars < 1:
            raise SourceROError("invalid_limit", "max_chars must be positive.", "Use max_chars >= 1.")
        char_limit = min(max_chars, MAX_OUTPUT_CHARS)
        text = _safe_read_text(file_path)
        line_start = 1
        if start_line is not None:
            if start_line < 1:
                raise SourceROError("invalid_line", "start_line must be at least 1.", "Use a one-based line number.")
            lines = text.splitlines(keepends=True)
            text = "".join(lines[start_line - 1 :])
            line_start = start_line
        content = text[:char_limit]
        result = envelope(
            {
                "repository": repo_id,
                "path": resolved_path,
                "startLine": line_start,
                "content": content,
                "charsReturned": len(content),
                "truncated": len(text) > len(content),
                "maxChars": char_limit,
            }
        )
        if output_format == "markdown":
            result["markdown"] = f"```text\n{content}\n```"
        return result

    def search_text(
        self,
        repo_id: str,
        query: str,
        path: str = ".",
        *,
        case_sensitive: bool = False,
        limit: int | None = DEFAULT_PAGE_LIMIT,
        offset: int | None = 0,
        format: str = "json",
    ) -> dict[str, Any]:
        output_format = _validate_format(format)
        if not query:
            raise SourceROError("invalid_query", "query must not be empty.", "Pass a literal text query.")
        _, root = self._repo_root(repo_id)
        _, start, resolved_path = self._resolve_path(repo_id, path)
        page_limit = _bounded_limit(limit)
        page_offset = _bounded_offset(offset)
        needle = query if case_sensitive else query.lower()
        matches: list[dict[str, Any]] = []
        scanned_files = 0
        scanned_bytes = 0
        total_matches = 0
        truncated = False
        candidates = [(start, resolved_path)] if start.is_file() else self._iter_search_candidates(root, start)
        for file_path, rel in candidates:
            if scanned_files >= MAX_SEARCH_SCAN_FILES or scanned_bytes >= MAX_SEARCH_SCAN_BYTES:
                truncated = True
                break
            try:
                file_size = file_path.stat().st_size
            except OSError:
                continue
            if scanned_bytes + file_size > MAX_SEARCH_SCAN_BYTES:
                truncated = True
                break
            scanned_files += 1
            scanned_bytes += file_size
            try:
                text = _safe_read_text(file_path)
            except SourceROError:
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                haystack = line if case_sensitive else line.lower()
                column = haystack.find(needle)
                if column == -1:
                    continue
                if total_matches >= page_offset and len(matches) < page_limit:
                    matches.append(
                        {
                            "path": rel,
                            "line": line_number,
                            "column": column + 1,
                            "excerpt": line[:240],
                        }
                    )
                total_matches += 1
        has_next_in_scanned_window = total_matches > page_offset + len(matches)
        next_offset = page_offset + len(matches) if has_next_in_scanned_window else None
        result = envelope(
            {
                "repository": repo_id,
                "path": resolved_path,
                "query": query,
                "caseSensitive": case_sensitive,
                "limit": page_limit,
                "offset": page_offset,
                "returned": len(matches),
                "totalMatches": total_matches,
                "totalMatchesIsLowerBound": truncated,
                "scannedFiles": scanned_files,
                "scannedBytes": scanned_bytes,
                "truncated": truncated,
                "matches": matches,
                "nextOffset": next_offset,
            }
        )
        if truncated:
            result["warning"] = scan_warning(
                "scan_limit_reached",
                (
                    f"text search stopped after {scanned_files} files and {scanned_bytes} bytes "
                    "within the configured scan budget."
                ),
                "Use a narrower path or a more specific query before retrying.",
            )
        if output_format == "markdown":
            result["markdown"] = _markdown_table(
                ["path", "line", "column", "excerpt"],
                [[match["path"], match["line"], match["column"], match["excerpt"]] for match in matches],
            )
        return result

    def _iter_search_candidates(self, root: Path, start: Path) -> Iterable[tuple[Path, str]]:
        for resolved, rel in self._iter_files(root, start, recursive=True):
            if resolved.is_file():
                yield resolved, rel

    def show_commit(self, repo_id: str, *, format: str = "json") -> dict[str, Any]:
        output_format = _validate_format(format)
        lock, root = self._repo_root(repo_id)
        commit = {
            "id": lock.id,
            "application": lock.application,
            "component": lock.component,
            "commit": lock.commit,
            "tag": lock.tag,
            "remote": _redacted_remote(lock.remote),
            "archiveSha256": lock.archive_sha256,
            "agentMountPath": lock.agent_mount_path,
            "runtimeMappingStatus": lock.runtime_mapping_status,
            "materialized": root.is_dir(),
            "sourceRootRef": f"${{{SOURCE_ROOT_ENV}}}/{lock.id}",
        }
        result = envelope({"commit": commit})
        if output_format == "markdown":
            result["markdown"] = _markdown_table(
                ["field", "value"],
                [[key, value if value is not None else ""] for key, value in commit.items()],
            )
        return result
