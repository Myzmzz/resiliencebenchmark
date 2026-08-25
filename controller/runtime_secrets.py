"""Private runtime capabilities kept outside Git and public run artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")


class RuntimeSecretError(RuntimeError):
    pass


class PrivateRuntimeSecretStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self._assert_private_directory(self.root)

    def put(self, scope_id: str, name: str, value: str) -> str:
        _safe_id(scope_id, "scope_id")
        _safe_id(name, "secret name")
        if len(value) < 32 or value != value.strip():
            raise RuntimeSecretError("runtime capability must be at least 32 clean characters")
        scope = self.root / scope_id
        scope.mkdir(mode=0o700, exist_ok=True)
        os.chmod(scope, 0o700)
        self._assert_private_directory(scope)
        destination = scope / name
        _atomic_private_write(destination, value + "\n")
        return f"runtime-secret://{scope_id}/{name}"

    def get(self, reference: str) -> str:
        match = re.fullmatch(r"runtime-secret://([^/]+)/([^/]+)", reference)
        if not match:
            raise RuntimeSecretError("invalid runtime secret reference")
        scope_id, name = match.groups()
        _safe_id(scope_id, "scope_id")
        _safe_id(name, "secret name")
        path = (self.root / scope_id / name).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise RuntimeSecretError("runtime secret escaped its private root") from exc
        _assert_private_file(path)
        return path.read_text(encoding="utf-8").strip()

    @staticmethod
    def _assert_private_directory(path: Path) -> None:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
            raise RuntimeSecretError("runtime secret directory must be real and mode 0700")


class BaselineCapabilityIssuer:
    def __init__(
        self,
        *,
        baseline_ledger_dir: Path,
        secret_store: PrivateRuntimeSecretStore,
        controller_pod_uid: str,
        ttl_seconds: int = 900,
    ):
        if not controller_pod_uid:
            raise RuntimeSecretError("controller_pod_uid is required")
        if not 60 <= ttl_seconds <= 3600:
            raise RuntimeSecretError("baseline capability ttl must be between 60 and 3600")
        self.baseline_ledger_dir = baseline_ledger_dir.resolve()
        self.baseline_ledger_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.baseline_ledger_dir, 0o700)
        PrivateRuntimeSecretStore._assert_private_directory(self.baseline_ledger_dir)
        self.secret_store = secret_store
        self.controller_pod_uid = controller_pod_uid
        self.ttl_seconds = ttl_seconds

    def issue(
        self,
        *,
        trial_id: str,
        run_id: str,
        namespace: str,
        target_name: str,
        target_uid: str,
        summary: Mapping[str, Any],
    ) -> dict[str, Any]:
        _safe_id(trial_id, "trial_id")
        _safe_id(run_id, "run_id")
        if summary.get("qualified") is not True:
            raise RuntimeSecretError("baseline summary is not qualified")
        window = summary.get("measurementWindow")
        if not isinstance(window, Mapping):
            raise RuntimeSecretError("baseline summary has no measurement window")
        if (
            int(window.get("durationSeconds", 0)) != 600
            or int(window.get("measurementWindowSeconds", 0)) != 300
            or window.get("calibrationWindowEligible") is not True
        ):
            raise RuntimeSecretError("formal baseline must use 600 seconds and score final 300")
        issued_at = datetime.now(UTC)
        expires_at = issued_at + timedelta(seconds=self.ttl_seconds)
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        summary_json = json.dumps(
            dict(summary), ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        ledger = {
            "schema_version": "baseline-capability.v1",
            "passed": True,
            "run_id": run_id,
            "trial_id": trial_id,
            "namespace": namespace,
            "target_name": target_name,
            "target_uid": target_uid,
            "controller_pod_uid": self.controller_pod_uid,
            "issued_at": issued_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "summary_sha256": hashlib.sha256(summary_json.encode("utf-8")).hexdigest(),
        }
        ledger_path = self.baseline_ledger_dir / f"{token_hash}.json"
        _atomic_private_write(
            ledger_path,
            json.dumps(ledger, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        secret_ref = self.secret_store.put(trial_id, "baseline-gate-token", token)
        return {
            "baseline_gate_token_ref": secret_ref,
            "baseline_ledger_ref": f"runtime-private://baseline-ledger/{token_hash}.json",
            "expires_at": expires_at.isoformat(),
            "summary_sha256": ledger["summary_sha256"],
        }


def _safe_id(value: str, description: str) -> None:
    if not SAFE_ID.fullmatch(value):
        raise RuntimeSecretError(f"invalid {description}")


def _assert_private_file(path: Path) -> None:
    if not path.is_file():
        raise RuntimeSecretError("runtime secret file is missing")
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise RuntimeSecretError("runtime secret file must be real and mode 0600")


def _atomic_private_write(path: Path, payload: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()
