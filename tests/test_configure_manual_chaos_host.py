from __future__ import annotations

from scripts.configure_manual_chaos_host import update_environment


def test_update_environment_preserves_token_and_enables_lease(tmp_path, monkeypatch) -> None:
    path = tmp_path / "chaos_control.env"
    path.write_text(
        'RESBENCH_MCP_TOKEN="keep-secret"\n'
        'RESBENCH_CHAOS_EXECUTE_ENABLED="false"\n'
        'RESBENCH_CHAOS_CONTROLLER_TOKEN_REF="disabled://controller"\n'
        'RESBENCH_CHAOS_CONTROLLER_POD_UID="controller-not-deployed"\n'
        'RESBENCH_CHAOS_CONTROLLER_POD_NAMESPACE="system"\n'
        'RESBENCH_CHAOS_CONTROLLER_POD_NAME="controller"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("scripts.configure_manual_chaos_host.os.chown", lambda *_: None)
    monkeypatch.setattr(
        "scripts.configure_manual_chaos_host.grp.getgrnam",
        lambda *_: type("Group", (), {"gr_gid": 1})(),
    )

    backup = update_environment(
        path,
        controller_id="manual-codex-controller",
        controller_token_ref="runtime://manual-codex-controller/token",
        lease_file=tmp_path / "controller-lease.json",
    )

    text = path.read_text(encoding="utf-8")
    assert 'RESBENCH_MCP_TOKEN="keep-secret"' in text
    assert 'RESBENCH_CHAOS_EXECUTE_ENABLED="true"' in text
    assert 'RESBENCH_CHAOS_CONTROLLER_POD_UID="manual-codex-controller"' in text
    assert 'RESBENCH_CHAOS_CONTROLLER_LEASE_FILE="' in text
    assert backup.read_text(encoding="utf-8").startswith("RESBENCH_MCP_TOKEN")
