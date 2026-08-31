from __future__ import annotations

from pathlib import Path

from scripts.local_e2e import parse_bashrc_assignments, sanitize, selected_context


def test_parse_bashrc_assignments_reads_only_acu_values(tmp_path: Path) -> None:
    bashrc = tmp_path / ".bashrc"
    bashrc.write_text(
        "\n".join(
            [
                "export acuurl='https://console.acucompute.com/v1'",
                'acukey="sk-test-secret"',
                "OTHER_SECRET=do-not-read",
            ]
        ),
        encoding="utf-8",
    )

    values = parse_bashrc_assignments(bashrc)

    assert values == {
        "acuurl": "https://console.acucompute.com/v1",
        "acukey": "sk-test-secret",
    }


def test_sanitize_redacts_secret_like_values() -> None:
    raw = "api key sk-secret-value-123456 and token"

    assert "sk-secret" not in sanitize(raw)


def test_selected_context_requires_explicit_target(monkeypatch) -> None:
    monkeypatch.delenv("RESBENCH_E2E_CONTEXT", raising=False)

    try:
        selected_context(None)
    except RuntimeError as exc:
        assert "explicit --context" in str(exc)
    else:
        raise AssertionError("selected_context should fail closed without context")


def test_local_e2e_script_has_no_kind_cluster_lifecycle() -> None:
    script = Path("scripts/local_e2e.py").read_text(encoding="utf-8")

    assert "kind create" not in script
    assert "kind delete" not in script
    assert "delete cluster" not in script
