from __future__ import annotations

from pathlib import Path

import pytest

from scripts.smoke_mcp_data_path import load_private_env, point_count


def test_private_stack_env_requires_mode_0600(tmp_path: Path) -> None:
    path = tmp_path / "stack.env"
    path.write_text('RESBENCH_MCP_TOKEN="test-token"\n', encoding="utf-8")
    path.chmod(0o644)

    with pytest.raises(RuntimeError, match="0600"):
        load_private_env(path)


def test_point_count_counts_only_matrix_values() -> None:
    assert point_count(
        {
            "result": [
                {"values": [[1, "1"], [2, "1"]]},
                {"values": [[1, "0"]]},
            ]
        }
    ) == 3
