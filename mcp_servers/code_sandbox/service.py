"""Trial-scoped, audited execution service behind the ``code_sandbox`` MCP."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from datetime import UTC, datetime
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Protocol

from stage2_service.platform_ledger import PlatformLedger


MAX_CODE_BYTES = 65_536
MAX_OUTPUT_BYTES = 65_536
MAX_TIMEOUT_SECONDS = 60


class CodeSandboxError(RuntimeError):
    """The caller violated the public sandbox contract or it is unavailable."""


@dataclass(frozen=True)
class SandboxRunResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    truncated: bool


class SandboxExecutor(Protocol):
    """Trusted bridge to the Linux agent-runtime execution sidecar."""

    def run(self, code: str, timeout_seconds: int) -> SandboxRunResult: ...


@dataclass(frozen=True)
class CodeSandboxConfig:
    trial_id: str
    ledger_root: Path
    artifact_root: Path | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "CodeSandboxConfig":
        values = os.environ if env is None else env
        trial_id = _required_env(values, "RESBENCH_AUTHORIZED_RUN_ID")
        ledger_root = Path(_required_env(values, "RESBENCH_PLATFORM_LEDGER_ROOT"))
        artifact_root = Path(_required_env(values, "RESBENCH_CODE_SANDBOX_ARTIFACT_ROOT"))
        if not ledger_root.is_absolute() or not artifact_root.is_absolute():
            raise CodeSandboxError("sandbox private roots must be absolute")
        return cls(trial_id=trial_id, ledger_root=ledger_root, artifact_root=artifact_root)


class CodeSandboxService:
    """Validate requests, delegate isolated execution, and write non-secret evidence."""

    def __init__(
        self,
        config: CodeSandboxConfig,
        *,
        executor: SandboxExecutor,
        ledger: PlatformLedger | None = None,
    ) -> None:
        self.config = config
        self.executor = executor
        self.ledger = ledger or PlatformLedger(config.ledger_root)
        self.artifact_root = (config.artifact_root or self.ledger.root / "sandbox-artifacts").resolve()
        self.artifact_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.artifact_root, 0o700)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "CodeSandboxService":
        """Construct the production-only agent-exec and broker chain."""
        config = CodeSandboxConfig.from_env(env)
        ledger = PlatformLedger(config.ledger_root)
        from .executor import ControlledSandboxExecutor

        return cls(
            config,
            executor=ControlledSandboxExecutor.from_env(ledger=ledger, env=env),
            ledger=ledger,
        )

    def run_python(self, code: str, timeout_seconds: int = 60) -> dict[str, Any]:
        """Execute bounded Python through the isolated sidecar, never locally."""
        if not isinstance(code, str):
            raise CodeSandboxError("code must be text")
        encoded = code.encode("utf-8")
        if not encoded:
            raise CodeSandboxError("code must not be empty")
        if len(encoded) > MAX_CODE_BYTES:
            raise CodeSandboxError("code exceeds 64 KiB")
        if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) or not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
            raise CodeSandboxError("timeout_seconds must be between 1 and 60")
        code_sha256 = hashlib.sha256(encoded).hexdigest()
        code_ref, output_ref, output_path = self._artifacts_for_run()
        _write_private(output_path.parent / "code.py", encoded)
        try:
            result = self.executor.run(code, timeout_seconds)
        except Exception as exc:
            _write_private(
                output_path,
                _artifact_json({
                    "status": "execution_unavailable",
                    "error_type": type(exc).__name__,
                }),
            )
            self.ledger.append(
                trial_id=self.config.trial_id,
                event_type="SANDBOX_RUN",
                occurred_at=_utc_now(),
                payload={
                    "code_sha256": code_sha256,
                    "exit_code": None,
                    "duration_ms": None,
                    "truncated": False,
                    "status": "execution_unavailable",
                    "error_type": type(exc).__name__,
                    "code_artifact_ref": code_ref,
                    "output_artifact_ref": output_ref,
                },
            )
            raise CodeSandboxError("isolated sandbox execution is unavailable") from exc
        stdout, stderr, truncated = _limit_output(result.stdout, result.stderr, result.truncated)
        _write_private(
            output_path,
            _artifact_json({
                "exit_code": result.exit_code,
                "stdout": stdout,
                "stderr": stderr,
                "duration_ms": result.duration_ms,
                "truncated": truncated,
            }),
        )
        self.ledger.append(
            trial_id=self.config.trial_id,
            event_type="SANDBOX_RUN",
            occurred_at=_utc_now(),
            payload={
                "code_sha256": code_sha256,
                "exit_code": result.exit_code,
                "duration_ms": result.duration_ms,
                "truncated": truncated,
                "status": "completed",
                "code_artifact_ref": code_ref,
                "output_artifact_ref": output_ref,
            },
        )
        return {
            "ok": True,
            "exit_code": result.exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "duration_ms": result.duration_ms,
            "truncated": truncated,
            "artifact_refs": [code_ref, output_ref],
        }

    def _artifacts_for_run(self) -> tuple[str, str, Path]:
        run_id = secrets.token_hex(16)
        relative_root = Path("sandbox") / run_id
        directory = self.artifact_root / relative_root
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        os.chmod(directory, 0o700)
        return str(relative_root / "code.py"), str(relative_root / "output.json"), directory / "output.json"


def _limit_output(stdout: str, stderr: str, already_truncated: bool) -> tuple[str, str, bool]:
    """Preserve a deterministic prefix while enforcing one total 64 KiB budget."""
    out = stdout.encode("utf-8", errors="replace")
    err = stderr.encode("utf-8", errors="replace")
    combined = out + err
    if len(combined) <= MAX_OUTPUT_BYTES:
        return stdout, stderr, already_truncated
    allowed_out = min(len(out), MAX_OUTPUT_BYTES)
    limited_out = out[:allowed_out]
    limited_err = err[: MAX_OUTPUT_BYTES - len(limited_out)]
    return (
        limited_out.decode("utf-8", errors="replace"),
        limited_err.decode("utf-8", errors="replace"),
        True,
    )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _required_env(values: Mapping[str, str], name: str) -> str:
    value = values.get(name)
    if not isinstance(value, str) or not value.strip():
        raise CodeSandboxError(f"{name} is required")
    return value


def _artifact_json(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(dict(value), ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _write_private(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
