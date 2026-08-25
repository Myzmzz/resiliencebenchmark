from __future__ import annotations

import json

from scripts.qualify_chaos_control_live import main


def test_live_chaos_qualification_is_dry_run_first(capsys) -> None:
    result = main(
        [
            "--controller-kubeconfig",
            "/missing/controller",
            "--chaos-kubeconfig",
            "/missing/chaos",
            "--baseline-report",
            "/missing/baseline",
            "--runtime-root",
            "/missing/runtime",
        ]
    )
    report = json.loads(capsys.readouterr().out)

    assert result == 0
    assert report["status"] == "planned"
    assert report["chaos_writes"] is False
    assert report["planned_fault"] == {
        "type": "network-delay",
        "delay_ms": 10,
        "duration_seconds": 5,
    }
