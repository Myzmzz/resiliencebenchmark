from __future__ import annotations

import json

from scripts.qualify_codex_chaos_write import main


def test_codex_chaos_write_qualification_is_dry_run_first(capsys) -> None:
    result = main(
        [
            "--stack-env",
            "/missing/env",
            "--baseline-report",
            "/missing/baseline",
            "--runtime-root",
            "/missing/runtime",
            "--public-episode",
            "/missing/episode",
        ]
    )
    report = json.loads(capsys.readouterr().out)

    assert result == 0
    assert report["status"] == "planned"
    assert report["fault"]["delay_ms"] == 10
