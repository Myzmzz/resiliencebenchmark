"""Historical trace redaction must preserve nested protocol evidence."""

import json

from scripts.import_stage2_harness_fixtures import sanitize_value


def test_nested_mcp_content_is_redacted_without_losing_call_correlation():
    raw = {"tool_use_id": "call-stable", "content": [{"text": json.dumps({
        "ok": True, "baseline_gate_token": "private-baseline-value",
        "cleanup_handle": "private-cleanup-value", "pod_uid": "pod-identity",
    })}], "thinking": "private reasoning"}
    result = sanitize_value(raw)
    assert result["tool_use_id"] == "call-stable"
    payload = json.loads(result["content"][0]["text"])
    assert payload == {"ok": True, "baseline_gate_token": "<redacted>",
                       "cleanup_handle": "<redacted>", "pod_uid": "pod-identity"}
    assert "thinking" not in result
    assert "private-baseline-value" not in json.dumps(result)
