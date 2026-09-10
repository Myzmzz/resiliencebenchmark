"""CLI wiring only; no model calls, cluster writes, or real campaigns."""
from types import SimpleNamespace

from scripts import run_stage2_matrix as cli


def test_explicit_matrix_execution_waits_for_gateway_check(tmp_path, monkeypatch):
    calls = []
    config = SimpleNamespace(repo_root=tmp_path, artifact_root=tmp_path)

    class System:
        def __init__(self, value):
            assert value is config

        def refresh_gateway_readiness(self):
            calls.append("probe_completed")

        def preflight(self):
            assert calls == ["probe_completed"]
            calls.append("preflight")
            return {"status": "READY"}

    def run_matrix(**kwargs):
        assert calls == ["probe_completed", "preflight"]
        assert kwargs["preflight"] == {"status": "READY"}
        calls.append("matrix")
        return {"matrix_id": "test-cli", "completed_trial_count": 0, "expected_trial_count": 0}

    monkeypatch.setattr(cli.Stage2RuntimeConfig, "from_env", lambda: config)
    monkeypatch.setattr(cli, "Stage2System", System)
    monkeypatch.setattr(cli, "load_qualification_matrix", lambda _: {})
    monkeypatch.setattr(cli, "build_matrix_requests", lambda **_: ())
    monkeypatch.setattr(cli, "run_matrix", run_matrix)

    assert cli.main([
        "--execute", "--matrix-id", "test-cli", "--qualification-file", str(tmp_path / "qualified.json")
    ]) == 0
    assert calls == ["probe_completed", "preflight", "matrix"]
