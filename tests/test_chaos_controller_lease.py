from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mcp_servers.chaos_control.service import (
    ChaosControlError,
    ChaosControlService,
    InMemoryChaosBackend,
    RuntimeConfig,
)


def _lease(path: Path, *, controller_id: str, expires_at: datetime, pid: int) -> None:
    path.parent.mkdir(mode=0o700)
    path.write_text(
        json.dumps(
            {
                "controller_id": controller_id,
                "pid": pid,
                "expires_at": expires_at.isoformat(),
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


def test_live_local_controller_process_lease_passes_identity_gate(tmp_path: Path) -> None:
    path = tmp_path / "private" / "controller-lease.json"
    _lease(
        path,
        controller_id="local-controller-supervisor",
        expires_at=datetime.now(UTC) + timedelta(minutes=2),
        pid=os.getpid(),
    )
    service = ChaosControlService(
        RuntimeConfig(
            execute_enabled=True,
            controller_pod_uid="local-controller-supervisor",
            controller_lease_file=path,
        ),
        InMemoryChaosBackend(),
    )

    asyncio.run(service._verify_controller_identity("unused"))


def test_expired_local_controller_lease_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "private" / "controller-lease.json"
    _lease(
        path,
        controller_id="local-controller-supervisor",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
        pid=os.getpid(),
    )
    service = ChaosControlService(
        RuntimeConfig(
            execute_enabled=True,
            controller_pod_uid="local-controller-supervisor",
            controller_lease_file=path,
        ),
        InMemoryChaosBackend(),
    )

    with pytest.raises(ChaosControlError) as exc:
        asyncio.run(service._verify_controller_identity("unused"))
    assert exc.value.code == "CONTROLLER_LEASE_INVALID"
