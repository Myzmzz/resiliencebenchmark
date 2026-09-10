"""Trial-scoped MCP tool capability policy files."""

from __future__ import annotations

import fcntl
import json
import os
import stat
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal
from uuid import uuid4

from pydantic import Field, model_validator

from .contracts import ContractModel, PermissionProfile, ServerPolicy, ToolPolicy
from .platform_ledger import PlatformLedger


MCP_POLICY_FILE_ENV = "RESBENCH_MCP_POLICY_FILE"
PLATFORM_LEDGER_ROOT_ENV = "RESBENCH_PLATFORM_LEDGER_ROOT"
POLICY_SCHEMA_VERSION = "stage2-mcp-tool-policy.v1"
DEFAULT_POLICY_FILE_NAME = "tools.policy.json"
LOCK_FILE_NAME = "tools.policy.lock"
PROTECTED_HARNESS_CHANNEL = "harness_channel"


class CapabilityPolicyError(RuntimeError):
    """Raised when a configured policy cannot be read safely."""


class CapabilityPolicyDocument(ContractModel):
    schema_version: Literal["stage2-mcp-tool-policy.v1"] = POLICY_SCHEMA_VERSION
    trial_id: str = Field(min_length=1, max_length=160)
    sequence: int = Field(ge=1)
    source: str = Field(min_length=1, max_length=120)
    since: datetime
    servers: dict[str, ServerPolicy] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_server_keys(self) -> CapabilityPolicyDocument:
        for key, policy in self.servers.items():
            if key != policy.server_name:
                raise ValueError("server policy key must match server_name")
        return self

    def server_policy(self, server_name: str) -> ServerPolicy | None:
        return self.servers.get(server_name)

    def tool_policy(self, server_name: str, tool_name: str) -> ToolPolicy | None:
        server = self.server_policy(server_name)
        if server is None:
            return None
        return server.tools.get(tool_name)


class CapabilityPolicyRegistry:
    """Controller-side mutator for one trial policy file."""

    def __init__(self, root: Path, ledger: PlatformLedger | Path | None = None):
        self.root = _prepare_private_root(root)
        self.policy_path = _policy_path(self.root)
        self.lock_path = self.root / LOCK_FILE_NAME
        self.ledger = (
            ledger
            if isinstance(ledger, PlatformLedger) or ledger is None
            else PlatformLedger(Path(ledger))
        )

    def initialize(
        self,
        trial_id: str,
        profile: PermissionProfile,
        *,
        source: str = "controller",
    ) -> CapabilityPolicyDocument:
        if PROTECTED_HARNESS_CHANNEL in profile.mcp_servers:
            raise CapabilityPolicyError("harness_channel policy is not mutable")
        servers = {name: ServerPolicy(server_name=name) for name in profile.mcp_servers}
        document = CapabilityPolicyDocument(
            trial_id=trial_id,
            sequence=1,
            source=source,
            since=_utc_now_datetime(),
            servers=servers,
        )
        with self._locked():
            _write_policy_document(self.policy_path, document)
        self._record(document, "POLICY_APPLIED", {"source": source})
        return document

    def set_server(
        self,
        server_name: str,
        *,
        state: Literal["enabled", "disabled", "decoy"] | None = None,
        channel_unavailable_until: datetime | None = None,
        chaos_create_uncertainty_variant: Any | None = None,
        reason: str | None = None,
        source: str = "controller",
    ) -> CapabilityPolicyDocument:
        _reject_harness_channel(server_name)
        with self._locked():
            current = _read_policy_document(self.policy_path)
            existing = current.server_policy(server_name) or ServerPolicy(server_name=server_name)
            update: dict[str, Any] = existing.model_dump(mode="python")
            if state is not None:
                update["state"] = state
            if reason is not None:
                update["reason"] = reason
            if channel_unavailable_until is not None:
                update["channel_unavailable_until"] = channel_unavailable_until
            if chaos_create_uncertainty_variant is not None:
                update["chaos_create_uncertainty_variant"] = chaos_create_uncertainty_variant
            servers = dict(current.servers)
            servers[server_name] = ServerPolicy(**update)
            updated = _next_document(current, servers=servers, source=source)
            _write_policy_document(self.policy_path, updated)
        self._record(
            updated,
            "POLICY_APPLIED",
            {
                "source": source,
                "server": server_name,
                "state": updated.servers[server_name].state,
            },
        )
        return updated

    def set_tool(
        self,
        server_name: str,
        tool_name: str,
        *,
        state: Literal["enabled", "disabled", "decoy"] | None,
        reason: str | None = None,
        source: str = "controller",
    ) -> CapabilityPolicyDocument:
        _reject_harness_channel(server_name)
        with self._locked():
            current = _read_policy_document(self.policy_path)
            existing_server = current.server_policy(server_name) or ServerPolicy(server_name=server_name)
            tools = dict(existing_server.tools)
            existing_tool = tools.get(tool_name)
            update = (
                existing_tool.model_dump(mode="python")
                if existing_tool is not None
                else ToolPolicy().model_dump(mode="python")
            )
            update["state"] = state
            if reason is not None:
                update["reason"] = reason
            tools[tool_name] = ToolPolicy(**update)
            server_update = existing_server.model_dump(mode="python")
            server_update["tools"] = tools
            servers = dict(current.servers)
            servers[server_name] = ServerPolicy(**server_update)
            updated = _next_document(current, servers=servers, source=source)
            _write_policy_document(self.policy_path, updated)
        self._record(
            updated,
            "POLICY_APPLIED",
            {
                "source": source,
                "server": server_name,
                "tool": tool_name,
                "state": state,
            },
        )
        return updated

    def restore(
        self,
        snapshot: CapabilityPolicyDocument,
        *,
        source: str = "controller",
    ) -> CapabilityPolicyDocument:
        if PROTECTED_HARNESS_CHANNEL in snapshot.servers:
            raise CapabilityPolicyError("harness_channel policy is not mutable")
        with self._locked():
            current = _read_policy_document(self.policy_path)
            restored = CapabilityPolicyDocument(
                trial_id=current.trial_id,
                sequence=current.sequence + 1,
                source=source,
                since=_utc_now_datetime(),
                servers=snapshot.servers,
            )
            _write_policy_document(self.policy_path, restored)
        self._record(restored, "POLICY_RESTORED", {"source": source})
        return restored

    def snapshot(self) -> CapabilityPolicyDocument:
        return read_policy_file(self.policy_path)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "r+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                yield
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            _chmod_if_exists(self.lock_path, 0o600)

    def _record(
        self,
        document: CapabilityPolicyDocument,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> None:
        if self.ledger is None:
            return
        self.ledger.append(
            trial_id=document.trial_id,
            event_type=event_type,
            occurred_at=document.since,
            payload={"sequence": document.sequence, **dict(payload)},
        )


def write_policy_file(
    path: Path,
    *,
    trial_id: str,
    servers: Mapping[str, ServerPolicy | Mapping[str, Any]],
    sequence: int = 1,
    source: str = "controller",
    since: datetime | None = None,
) -> CapabilityPolicyDocument:
    """Atomically write a 0600 policy file and return the validated document."""

    document = CapabilityPolicyDocument(
        trial_id=trial_id,
        sequence=sequence,
        source=source,
        since=since or _utc_now_datetime(),
        servers={
            name: value if isinstance(value, ServerPolicy) else ServerPolicy(**dict(value))
            for name, value in servers.items()
        },
    )
    _write_policy_document(_validated_target_path(path), document)
    return document


def read_policy_file(path: Path) -> CapabilityPolicyDocument:
    """Read and validate a configured policy file without mtime caching."""

    return _read_policy_document(_validated_existing_path(path))


def policy_file_from_env(env: Mapping[str, str] | None = None) -> Path | None:
    values = os.environ if env is None else env
    raw = values.get(MCP_POLICY_FILE_ENV)
    if raw is None or not raw.strip():
        return None
    path = Path(raw)
    if not path.is_absolute():
        raise CapabilityPolicyError(f"{MCP_POLICY_FILE_ENV} must be absolute")
    return path


def platform_ledger_root_from_env(env: Mapping[str, str] | None = None) -> Path | None:
    values = os.environ if env is None else env
    raw = values.get(PLATFORM_LEDGER_ROOT_ENV)
    if raw is None or not raw.strip():
        return None
    path = Path(raw)
    if not path.is_absolute():
        raise CapabilityPolicyError(f"{PLATFORM_LEDGER_ROOT_ENV} must be absolute")
    return path


def is_channel_unavailable(policy: ServerPolicy, now: datetime | None = None) -> bool:
    if policy.channel_unavailable_until is None:
        return False
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    unavailable_until = policy.channel_unavailable_until
    if unavailable_until.tzinfo is None:
        unavailable_until = unavailable_until.replace(tzinfo=timezone.utc)
    return current < unavailable_until


def effective_tool_state(
    server_policy: ServerPolicy | None,
    tool_name: str,
) -> Literal["enabled", "disabled", "decoy"]:
    if server_policy is None:
        return "disabled"
    tool_policy = server_policy.tools.get(tool_name)
    if tool_policy is None or tool_policy.state is None:
        return server_policy.state
    return tool_policy.state


def _next_document(
    current: CapabilityPolicyDocument,
    *,
    servers: Mapping[str, ServerPolicy],
    source: str,
) -> CapabilityPolicyDocument:
    return CapabilityPolicyDocument(
        trial_id=current.trial_id,
        sequence=current.sequence + 1,
        source=source,
        since=_utc_now_datetime(),
        servers=dict(servers),
    )


def _write_policy_document(path: Path, document: CapabilityPolicyDocument) -> None:
    final_path = _validated_target_path(path)
    final_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(final_path.parent, 0o700)
    payload = (
        json.dumps(
            document.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    temp_path = final_path.with_name(f".{final_path.name}.{uuid4().hex}.tmp")
    fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, final_path)
        os.chmod(final_path, 0o600)
        _fsync_directory(final_path.parent)
    except BaseException:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _read_policy_document(path: Path) -> CapabilityPolicyDocument:
    final_path = _validated_existing_path(path)
    try:
        payload = json.loads(final_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("policy root must be a JSON object")
        return CapabilityPolicyDocument(**payload)
    except CapabilityPolicyError:
        raise
    except Exception as exc:
        raise CapabilityPolicyError("configured MCP policy file is invalid") from exc


def _policy_path(root: Path) -> Path:
    path = (root / DEFAULT_POLICY_FILE_NAME).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise CapabilityPolicyError("MCP policy file escaped registry root") from exc
    return path


def _prepare_private_root(root: Path) -> Path:
    if root.exists() and root.is_symlink():
        raise CapabilityPolicyError("MCP policy root must not be a symlink")
    resolved = root.resolve()
    resolved.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(resolved, 0o700)
    return resolved


def _validated_target_path(path: Path) -> Path:
    _reject_path_symlink(path)
    final_path = path.resolve()
    if final_path.name != path.name or final_path.name.startswith("."):
        raise CapabilityPolicyError("MCP policy file name is invalid")
    return final_path


def _validated_existing_path(path: Path) -> Path:
    _reject_path_symlink(path)
    final_path = path.resolve()
    if not final_path.is_file():
        raise CapabilityPolicyError("configured MCP policy file is missing")
    mode = stat.S_IMODE(final_path.stat().st_mode)
    if mode != 0o600:
        raise CapabilityPolicyError("configured MCP policy file must be 0600")
    parent_mode = stat.S_IMODE(final_path.parent.stat().st_mode)
    if parent_mode & 0o077:
        raise CapabilityPolicyError("configured MCP policy directory must not be group/world accessible")
    return final_path


def _reject_path_symlink(path: Path) -> None:
    current = path
    while True:
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISLNK(mode):
                raise CapabilityPolicyError("MCP policy path must not contain symlinks")
        if current.parent == current:
            break
        current = current.parent


def _reject_harness_channel(server_name: str) -> None:
    if server_name == PROTECTED_HARNESS_CHANNEL:
        raise CapabilityPolicyError("harness_channel policy is not mutable")


def _utc_now_datetime() -> datetime:
    return datetime.now(timezone.utc)


def _chmod_if_exists(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except FileNotFoundError:
        pass


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
