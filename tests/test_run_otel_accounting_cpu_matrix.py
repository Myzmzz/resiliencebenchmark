from __future__ import annotations

from pathlib import Path

import pytest

from stage2_service.runtime_lock import RuntimeLock, RuntimeLockBusy


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_d0_cli_holds_runtime_lock_across_real_run_entrypoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import run_otel_accounting_cpu_matrix as cli

    kubeconfig = tmp_path / "controller.kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")
    monkeypatch.setenv("RESBENCH_AGENT_EXEC_SOCKET", str(tmp_path / "agent-exec" / "agent.sock"))

    class FakeD0Campaign:
        def __init__(self, config, *, environment):
            self.config = config
            self.environment = environment

        def run(self, campaign_id):
            with pytest.raises(RuntimeLockBusy):
                RuntimeLock.from_environment().acquire(owner="nested-api")
            return {
                "status": "QUALIFIED",
                "campaign_id": campaign_id or "campaign-test",
                "artifact_dir": str(tmp_path / "artifacts" / "campaign-test"),
                "visualization": {},
            }

    monkeypatch.setattr(cli, "D0Campaign", FakeD0Campaign)

    rc = cli.main(
        [
            "--execute",
            "--repo-root",
            str(REPO_ROOT),
            "--artifact-root",
            str(tmp_path / "artifacts"),
            "--kubeconfig",
            str(kubeconfig),
            "--agents",
            "codex",
            "--model",
            "gpt-5.5",
            "--campaign-id",
            "campaign-test",
        ]
    )

    assert rc == 0
    with RuntimeLock.from_environment().acquire(owner="after-d0-cli"):
        pass
