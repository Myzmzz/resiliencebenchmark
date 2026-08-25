from __future__ import annotations

import json
import stat
from pathlib import Path

import yaml

from controller.scoped_kubeconfig import create_scoped_kubeconfig


def test_scoped_kubeconfig_contains_only_short_lived_service_account_identity(
    tmp_path: Path,
) -> None:
    admin = tmp_path / "admin.kubeconfig"
    admin.write_text("placeholder", encoding="utf-8")
    calls = []

    def runner(argv: list[str]) -> str:
        calls.append(argv)
        if "token" in argv:
            return "token-value-with-at-least-thirty-two-characters"
        return json.dumps(
            {
                "clusters": [
                    {
                        "cluster": {
                            "server": "https://cluster.invalid:6443",
                            "certificate-authority-data": "Y2E=",
                        }
                    }
                ]
            }
        )

    destination = tmp_path / "private" / "controller.kubeconfig"
    report = create_scoped_kubeconfig(
        admin_kubeconfig=admin,
        service_account="resbench-controller-otel-demo",
        output_path=destination,
        runner=runner,
    )
    document = yaml.safe_load(destination.read_text())

    assert report["mode"] == "0600"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert document["users"] == [
        {
            "name": "resbench-controller-otel-demo",
            "user": {"token": "token-value-with-at-least-thirty-two-characters"},
        }
    ]
    assert document["clusters"][0]["cluster"]["server"] == "https://cluster.invalid:6443"
    assert any("--duration=6h" in call for call in calls)
