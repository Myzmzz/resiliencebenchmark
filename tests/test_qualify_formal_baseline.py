from __future__ import annotations

import json
from pathlib import Path

from scripts.qualify_formal_baseline import main


def test_formal_baseline_is_dry_run_first_and_freezes_600_300_contract(
    tmp_path: Path,
    capsys,
) -> None:
    kubeconfig = tmp_path / "controller.kubeconfig"
    kubeconfig.write_text("placeholder", encoding="utf-8")
    image = "registry.invalid/otel-demo:load@sha256:" + "a" * 64

    result = main(["--kubeconfig", str(kubeconfig), "--image", image])
    report = json.loads(capsys.readouterr().out)

    assert result == 0
    assert report["status"] == "planned"
    assert report["duration_seconds"] == 600
    assert report["measurement_window_seconds"] == 300
