from __future__ import annotations

import json
import os
import stat
from datetime import datetime

import pytest

from scripts.run_manual_chaos_controller import write_lease


def test_write_lease_is_private_and_live(tmp_path) -> None:
    path = tmp_path / "private" / "controller-lease.json"

    write_lease(
        path,
        controller_id="manual-codex-controller",
        pid=os.getpid(),
        ttl_seconds=90,
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["controller_id"] == "manual-codex-controller"
    assert payload["pid"] == os.getpid()
    assert datetime.fromisoformat(payload["expires_at"]) > datetime.fromisoformat(
        payload["issued_at"]
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize("controller_id", ["", "bad id", "../escape"])
def test_write_lease_rejects_invalid_controller_id(tmp_path, controller_id) -> None:
    with pytest.raises(ValueError, match="controller id"):
        write_lease(
            tmp_path / "controller-lease.json",
            controller_id=controller_id,
            pid=os.getpid(),
            ttl_seconds=90,
        )
