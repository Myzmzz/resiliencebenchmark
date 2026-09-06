from __future__ import annotations

import json
from pathlib import Path

import pytest

from stage2_service.gateway_evidence import read_gateway_artifact, verify_gateway_requests


@pytest.mark.parametrize("mutation", [None, "wrong_model", "duplicate", "body", "not_array", "symlink", "empty"])
def test_durable_receipts_are_revalidated(tmp_path, mutation):
    row = {"trial_id": "trial-1", "harness": "codex", "model_alias": "gpt-5.5",
           "request_id": "req-1", "gateway_config_sha256": "cfg-1", "outcome": "received"}
    rows = [row]
    if mutation == "wrong_model":
        row["model_alias"] = "another-model"
    elif mutation == "duplicate":
        rows.append(row)
    elif mutation == "body":
        row["body"] = "private contents"
    elif mutation == "not_array":
        rows = row
    elif mutation == "empty":
        rows = []
    path = tmp_path / "gateway-requests.json"
    path.write_text(json.dumps(rows))
    if mutation == "symlink":
        link = tmp_path / "receipt-link.json"
        link.symlink_to(path)
        path = link
    result = read_gateway_artifact(
        path, trial_id="trial-1", harness="codex", model_alias="gpt-5.5",
        config_sha256="cfg-1", request_ids={"req-1"},
    )
    assert (result is not None) is (mutation is None)


def test_same_directory_symlink_duplicate_rows_and_missing_receipt_are_rejected(tmp_path):
    row = {"trial_id": "trial-1", "harness": "codex", "model_alias": "gpt-5.5",
           "request_id": "req-1", "gateway_config_sha256": "cfg-1", "outcome": "received"}
    arguments = dict(trial_id="trial-1", harness="codex", model_alias="gpt-5.5",
                     config_sha256="cfg-1", request_ids={"req-1"})
    target = tmp_path / "actual.jsonl"
    _write_rows(target, [row])
    link = tmp_path / "trial-1.jsonl"
    link.symlink_to(target)
    assert not verify_gateway_requests(tmp_path, **arguments)
    link.unlink()
    _write_rows(link, [row, row])
    assert not verify_gateway_requests(tmp_path, **arguments)
    _write_rows(link, [{k: v for k, v in row.items() if k != "outcome"}])
    assert not verify_gateway_requests(tmp_path, **arguments)
    _write_rows(link, [{**row, "body": "must never be copied"}])
    assert not verify_gateway_requests(tmp_path, **arguments)


def _write_rows(path: Path, rows: list[object]) -> None:
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_gateway_evidence_accepts_valid_trial_and_qualification_ids(tmp_path: Path):
    root = tmp_path / "audit"
    root.mkdir()

    for trial_id in ("trial-20260905-01", "qualification-20260905-01"):
        _write_rows(
            root / f"{trial_id}.jsonl",
            [
                {
                    "trial_id": trial_id,
                    "harness": "codex",
                    "model_alias": "gpt-5.5",
                    "request_id": f"{trial_id}-req-1",
                    "gateway_config_sha256": "cfg-1",
                    "outcome": "received",
                },
                {
                    "trial_id": trial_id,
                    "harness": "codex",
                    "model_alias": "gpt-5.5",
                    "request_id": f"{trial_id}-req-2",
                    "gateway_config_sha256": "cfg-1",
                    "outcome": "received",
                },
            ],
        )
        assert verify_gateway_requests(
            root,
            trial_id=trial_id,
            harness="codex",
            model_alias="gpt-5.5",
            config_sha256="cfg-1",
            request_ids={f"{trial_id}-req-1", f"{trial_id}-req-2"},
        )


def test_gateway_evidence_rejects_empty_unknown_duplicate_and_mismatched_rows(tmp_path: Path):
    root = tmp_path / "audit"
    root.mkdir()

    _write_rows(
        root / "trial-20260905-02.jsonl",
        [
            {
                "trial_id": "trial-20260905-02",
                "harness": "codex",
                "model_alias": "gpt-5.5",
                "request_id": "req-1",
                "gateway_config_sha256": "cfg-1",
                "outcome": "received",
            },
            {
                "trial_id": "trial-20260905-02",
                "harness": "claude-code",
                "model_alias": "gpt-5.5",
                "request_id": "req-2",
                "gateway_config_sha256": "cfg-1",
                "outcome": "received",
            },
        ],
    )

    assert not verify_gateway_requests(
        root,
        trial_id="trial-20260905-02",
        harness="codex",
        model_alias="gpt-5.5",
        config_sha256="cfg-1",
        request_ids=set(),
    )
    assert not verify_gateway_requests(
        root,
        trial_id="trial-20260905-02",
        harness="codex",
        model_alias="gpt-5.5",
        config_sha256="cfg-1",
        request_ids={"missing"},
    )
    assert not verify_gateway_requests(
        root,
        trial_id="trial-20260905-02",
        harness="codex",
        model_alias="gpt-5.5",
        config_sha256="cfg-1",
        request_ids={"req-1", "req-1"},
    )
    assert not verify_gateway_requests(
        root,
        trial_id="trial-20260905-02",
        harness="codex",
        model_alias="gpt-4o",
        config_sha256="cfg-1",
        request_ids={"req-1", "req-2"},
    )
    assert not verify_gateway_requests(
        root,
        trial_id="trial-20260905-02",
        harness="codex",
        model_alias="gpt-5.5",
        config_sha256="cfg-x",
        request_ids={"req-1", "req-2"},
    )


def test_gateway_evidence_rejects_bad_json_non_dict_oversized_symlink_and_path_traversal(tmp_path: Path):
    root = tmp_path / "audit"
    root.mkdir()

    bad_json = root / "trial-bad-json.jsonl"
    bad_json.write_text('{"trial_id":"trial-bad-json"}\nnot-json\n', encoding="utf-8")
    assert not verify_gateway_requests(
        root,
        trial_id="trial-bad-json",
        harness="codex",
        model_alias="gpt-5.5",
        config_sha256="cfg-1",
        request_ids={"req-1"},
    )

    non_dict = root / "trial-non-dict.jsonl"
    _write_rows(non_dict, [["not", "a", "dict"]])
    assert not verify_gateway_requests(
        root,
        trial_id="trial-non-dict",
        harness="codex",
        model_alias="gpt-5.5",
        config_sha256="cfg-1",
        request_ids={"req-1"},
    )

    oversized = root / "trial-oversized.jsonl"
    oversized.write_text(
        json.dumps(
            {
                "trial_id": "trial-oversized",
                "harness": "codex",
                "model_alias": "gpt-5.5",
                "request_id": "req-1",
                "gateway_config_sha256": "cfg-1",
                "outcome": "received",
                "padding": "x" * 1_100_000,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert not verify_gateway_requests(
        root,
        trial_id="trial-oversized",
        harness="codex",
        model_alias="gpt-5.5",
        config_sha256="cfg-1",
        request_ids={"req-1"},
    )

    target = root / "trial-symlink-target.jsonl"
    _write_rows(
        target,
        [
            {
                "trial_id": "trial-symlink-target",
                "harness": "codex",
                "model_alias": "gpt-5.5",
                "request_id": "req-1",
                "gateway_config_sha256": "cfg-1",
                "outcome": "received",
            }
        ],
    )
    (root / "trial-symlink.jsonl").symlink_to(target)
    assert not verify_gateway_requests(
        root,
        trial_id="trial-symlink",
        harness="codex",
        model_alias="gpt-5.5",
        config_sha256="cfg-1",
        request_ids={"req-1"},
    )

    assert not verify_gateway_requests(
        root,
        trial_id="../escape",
        harness="codex",
        model_alias="gpt-5.5",
        config_sha256="cfg-1",
        request_ids={"req-1"},
    )
