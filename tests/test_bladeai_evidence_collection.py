"""Supplementary receipt collection; tests are written but not run in this revision."""
import json

import pytest

from stage2_service.harness_runtime import _collect_bladeai_shim_evidence


def test_missing_shim_file_is_not_an_invented_success(tmp_path):
    assert _collect_bladeai_shim_evidence(tmp_path / "aliases.json", tmp_path) == ([], False)


def test_collects_exact_receipts_without_asserting_they_are_authoritative(tmp_path):
    row = {"shim_operation": "create", "mcp_calls": [{"controller_call_id": "call-1"}]}
    (tmp_path / "aliases.evidence.jsonl").write_text(json.dumps(row) + "\n")
    assert _collect_bladeai_shim_evidence(tmp_path / "aliases.json", tmp_path) == ([row], True)


def test_linked_or_non_object_receipts_are_rejected(tmp_path):
    outside = tmp_path / "outside.jsonl"
    outside.write_text('{}\n')
    evidence = tmp_path / "aliases.evidence.jsonl"
    evidence.symlink_to(outside)
    with pytest.raises(ValueError):
        _collect_bladeai_shim_evidence(tmp_path / "aliases.json", tmp_path)
    evidence.unlink()
    evidence.write_text('[]\n')
    with pytest.raises(ValueError):
        _collect_bladeai_shim_evidence(tmp_path / "aliases.json", tmp_path)
