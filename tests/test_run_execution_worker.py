from __future__ import annotations

import pytest

from scripts.run_execution_worker import REQUIRED_ENV, _required_environment


def test_execution_worker_fails_before_claiming_when_runtime_is_incomplete(monkeypatch) -> None:
    for name in REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(RuntimeError, match="execution worker is disabled") as exc:
        _required_environment()

    assert "RESBENCH_KUBECONFIG" in str(exc.value)
    assert "RESBENCH_MCP_TOKEN" in str(exc.value)


def test_execution_worker_accepts_private_codex_oauth_as_gateway_alternative(
    monkeypatch,
    tmp_path,
) -> None:
    for name in REQUIRED_ENV:
        monkeypatch.setenv(name, "configured")
    monkeypatch.delenv("RESBENCH_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("RESBENCH_LLM_API_KEY", raising=False)
    auth = tmp_path / "auth.json"
    auth.write_text("{}", encoding="utf-8")
    auth.chmod(0o600)
    monkeypatch.setenv("RESBENCH_CODEX_AUTH_FILE", str(auth))

    runtime = _required_environment()

    assert runtime["RESBENCH_CODEX_AUTH_FILE"] == str(auth)
    assert "RESBENCH_LLM_API_KEY" not in runtime
