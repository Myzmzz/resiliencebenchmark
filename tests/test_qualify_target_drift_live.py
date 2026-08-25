from __future__ import annotations

import json
from pathlib import Path

from scripts.qualify_target_drift_live import main


def test_target_drift_qualification_is_dry_run_first(tmp_path: Path, capsys) -> None:
    kubeconfig = tmp_path / "controller.kubeconfig"
    kubeconfig.write_text("placeholder", encoding="utf-8")
    image = "registry.invalid/otel-demo:load@sha256:" + "a" * 64

    result = main(["--kubeconfig", str(kubeconfig), "--image", image])
    report = json.loads(capsys.readouterr().out)

    assert result == 0
    assert report["status"] == "planned"
    assert "UID precondition" in report["mutation"]
    assert report["recovery_smoke_seconds"] == 60
