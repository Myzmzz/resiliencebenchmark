from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOG = REPO_ROOT / "tasks/catalog/resilience-defect-patterns-risk-consequence.v1.yaml"
OTEL_DEMO_CANDIDATES = REPO_ROOT / "tasks/catalog/otel-demo-static-candidates.v1.yaml"
OTEL_DEMO_REPORT = REPO_ROOT / "docs/status/otel-demo-six-static-candidates-20260831.md"
QUALIFIED_FAULTS = {
    "network-delay",
    "network-loss",
    "cpu-load",
    "memory-load",
    "pod-delete",
    "pod-fail",
}


def test_risk_consequence_catalog_is_complete_and_source_free() -> None:
    value = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))

    assert value["schema_version"] == "resilience-defect-pattern-catalog.v1"
    assert value["public_evidence_included"] is False
    patterns = value["patterns"]
    assert [item["pattern_id"] for item in patterns] == [
        f"RDP-{index:02d}" for index in range(1, 21)
    ]
    assert len({item["risk_name"] for item in patterns}) == 20
    assert len({item["consequence_name"] for item in patterns}) == 20

    for item in patterns:
        assert item["defect_name"] == (
            f"风险：{item['risk_name']}；后果：{item['consequence_name']}"
        )
        assert set(item["applicable_fault_types"]) <= QUALIFIED_FAULTS
        assert item["mechanism_summary"]
        assert item["static_evidence_required"]
        assert item["runtime_evidence_required"]

    rendered = CATALOG.read_text(encoding="utf-8").lower()
    assert "http://" not in rendered
    assert "https://" not in rendered
    assert "public_basis" not in rendered
    assert "public_sources" not in rendered


def test_otel_demo_focus_is_six_plain_language_static_candidates() -> None:
    candidate_set = yaml.safe_load(OTEL_DEMO_CANDIDATES.read_text(encoding="utf-8"))

    assert candidate_set["schema_version"] == "otel-demo-static-candidate-set.v1"
    assert candidate_set["selection_status"] == "selected_for_testing"
    assert candidate_set["public_evidence_included"] is False
    assert candidate_set["source_snapshot"] == {
        "repository": "open-telemetry/opentelemetry-demo",
        "version": "2.2.0",
        "commit": "b74a7bc7bbe66099c61951f42b24dab8b6f02d18",
    }
    assert candidate_set["runtime_confirmation"]["performed"] is False

    candidates = candidate_set["candidates"]
    assert [item["candidate_id"] for item in candidates] == [
        f"OTEL-C{index:02d}" for index in range(1, 7)
    ]
    assert candidate_set["recommended_test_order"] == [
        "OTEL-C04", "OTEL-C06", "OTEL-C05", "OTEL-C01", "OTEL-C03", "OTEL-C02"
    ]
    assert all(set(item) == {"candidate_id", "title", "description", "test_note"} for item in candidates)
    assert all(len(item["description"]) >= 80 for item in candidates)
    assert all(len(item["test_note"]) >= 40 for item in candidates)

    rendered = OTEL_DEMO_CANDIDATES.read_text(encoding="utf-8").lower()
    assert "http://" not in rendered
    assert "https://" not in rendered
    assert "risk_name" not in rendered
    assert "consequence_name" not in rendered
    assert "defect_name" not in rendered
    assert "public_basis" not in rendered
    assert "public_sources" not in rendered

    report = OTEL_DEMO_REPORT.read_text(encoding="utf-8")
    assert report.count("\n## ") == 7
    assert "RDP-" not in report
    assert "风险：" not in report
    assert "后果：" not in report
