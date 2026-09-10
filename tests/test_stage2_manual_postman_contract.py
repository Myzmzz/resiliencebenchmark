"""Static validation for the manual, one-request-at-a-time Postman asset."""

from __future__ import annotations

import json
from pathlib import Path

from stage2_service.task_service import Stage2TaskCreateRequest


ROOT = Path(__file__).resolve().parents[1]
COLLECTION = ROOT / "docs/postman/stage2-manual-single-case.collection.json"
ENVIRONMENT = ROOT / "docs/postman/stage2-manual-single-case.environment.example.json"
EXPECTED = ("L0", "L1", "L2", "L3", "L4", "C0", "D1", "D2", "D3", "D4", "D5", "D6-A", "D6-B", "D7-A", "D7-B", "D8-A", "D8-B")
FORBIDDEN = {"schema_version", "request_id", "episode", "permission_profile", "bladeai_native"}


def _variables(items: list[dict]) -> dict[str, str]:
    return {str(item["key"]): str(item.get("value", "")) for item in items}


def test_manual_postman_collection_has_68_single_case_current_contract_requests() -> None:
    collection = json.loads(COLLECTION.read_text(encoding="utf-8"))
    environment = _variables(json.loads(ENVIRONMENT.read_text(encoding="utf-8"))["values"])
    assert set(environment) == {"stage2_base_url"}
    assert not collection.get("event")
    folders = collection["item"]
    assert len(folders) == 4
    assert sum(len(folder["item"]) for folder in folders) == 68

    for folder in folders:
        assert "variable" not in folder
        assert tuple(item["name"].split("（", 1)[0] for item in folder["item"]) == EXPECTED
        for item in folder["item"]:
            request = item["request"]
            assert request["method"] == "POST"
            assert request["url"] == "{{stage2_base_url}}/api/v1/stage2/tasks"
            raw = request["body"]["raw"]
            assert raw != "{}"
            body = json.loads(raw)
            assert all("{{" not in str(value) for key, value in body.items() if key != "model")
            assert "{{" not in body["model"]
            assert not (set(body) & FORBIDDEN)
            parsed = Stage2TaskCreateRequest.model_validate(body)
            assert parsed.disturbance is not None
            if parsed.disturbance in {"D7-A", "D7-B", "D8-A", "D8-B"}:
                assert parsed.tool_substitution_variant is not None
