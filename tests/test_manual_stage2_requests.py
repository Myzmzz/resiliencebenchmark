"""Copy-ready requests preserve the existing L-level and single-task contracts."""
import json
from pathlib import Path

from stage2_service.contracts import (
    AutonomyLevel,
    HarnessKind,
    STAGE2_BLADEAI_DEFAULT_MODEL,
    STAGE2_DEFAULT_MODEL,
)
from stage2_service.task_service import Stage2TaskCreateRequest, Stage2TaskService

ROOT = Path(__file__).resolve().parents[1]
CASES = ("L0", "L1", "L2", "L3", "L4", "C0", "D1", "D2", "D3", "D4", "D5",
         "D6-A", "D6-B", "D7-A", "D7-B", "D8-A", "D8-B")


def test_manual_request_catalog_has_exactly_four_by_seventeen_single_tasks():
    index = json.loads((ROOT / "docs/manual-tests/requests/index.json").read_text())
    assert index["count"] == len(index["requests"]) == 68
    expected = {(h.value, case) for h in HarnessKind for case in CASES}
    observed = {(item["harness"], item["case"]) for item in index["requests"]}
    assert observed == expected
    forbidden = {"episode", "schema_version", "request_id", "permission_profile", "bladeai_native"}
    for item in index["requests"]:
        body = json.loads((ROOT / item["path"]).read_text())
        parsed = Stage2TaskCreateRequest.model_validate(body)
        assert parsed.harness.value == item["harness"]
        expected_model = (
            STAGE2_BLADEAI_DEFAULT_MODEL
            if parsed.harness is HarnessKind.BLADEAI
            else STAGE2_DEFAULT_MODEL
        )
        assert parsed.model == expected_model
        assert not forbidden.intersection(body)
        assert body.get("cases") == ["C0"] or body.get("disturbance") in {"none", *CASES[6:]}


def test_l0_l4_requests_do_not_modify_existing_prompt_or_evaluation_semantics():
    for harness in HarnessKind:
        for level in AutonomyLevel:
            name = level.value.split("_", 1)[0]
            body = json.loads((ROOT / f"docs/manual-tests/requests/{harness.value}/{name}.json").read_text())
            expected = {
                **Stage2TaskService._autonomy_case(level)["recommended_post_body"],
                "harness": harness.value,
                "model": (
                    STAGE2_BLADEAI_DEFAULT_MODEL
                    if harness is HarnessKind.BLADEAI
                    else STAGE2_DEFAULT_MODEL
                ),
            }
            assert body == expected


def test_disturbances_change_only_case_selection_and_harness_from_first_c0():
    control = json.loads((ROOT / "docs/manual-tests/codex-c0-first-20260906.request.json").read_text())
    for harness in HarnessKind:
        for case in CASES[5:]:
            body = json.loads((ROOT / f"docs/manual-tests/requests/{harness.value}/{case}.json").read_text())
            expected = {
                **control,
                "harness": harness.value,
                "model": (
                    STAGE2_BLADEAI_DEFAULT_MODEL
                    if harness is HarnessKind.BLADEAI
                    else STAGE2_DEFAULT_MODEL
                ),
                "disturbance": "none" if case == "C0" else case,
            }
            assert body == expected
