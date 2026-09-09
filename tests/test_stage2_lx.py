from pathlib import Path

from fastapi.testclient import TestClient

from stage2_service.api import CampaignSupervisor, create_app
from stage2_service.lx import LxService, LxSlots, PromptVariantRequest


class FakeTaskService:
    def __init__(self):
        self.created = {}

    def create(self, request, *, idempotency_key=None):
        task_id = "stage2-task-0123456789abcdef"
        self.created[task_id] = request
        return {"task_id": task_id, "task_status": "QUEUED"}

    def get(self, task_id, **kwargs):
        return {
            "task_id": task_id,
            "task_status": "QUEUED",
            "terminal": False,
            "current_phase": "QUEUED",
            "elapsed_seconds": 0,
            "events": [],
            "structured_feedback": [],
            "issues": [],
        }

    def abort(self, task_id, request):
        return {"task_id": task_id, "stop_requested": True}


def service(tmp_path: Path) -> LxService:
    return LxService(task_service=FakeTaskService(), artifact_root=tmp_path, gateway_audit_root=tmp_path)


def test_variant_generation_is_deterministic_and_has_matrix(tmp_path):
    svc = service(tmp_path)
    request = PromptVariantRequest(
        application="otel-demo",
        slots=LxSlots(
            target="cart",
            fault_type="cpu_load",
            fault_params={"cpu_percent": 80},
            duration_seconds=300,
        ),
    )
    first = svc.create_variants(request)
    second = svc.create_variants(request)
    assert first["variant_set_id"] == second["variant_set_id"]
    assert first["created_at"] == second["created_at"]
    assert [item["level"] for item in first["variants"]] == ["L0", "L1", "L2", "L3", "L4"]
    assert first["variants"][0]["disclosed_slots"][-1] == "duration_seconds"
    assert first["variants"][2]["recovery_trigger"] == "condition_based"
    assert all(item["lint"]["passed"] for item in first["variants"])


def test_lx_api_requires_immutable_variant_for_run(tmp_path):
    svc = service(tmp_path)
    app = create_app(CampaignSupervisor.__new__(CampaignSupervisor), lx_service=svc)
    client = TestClient(app)
    variant_response = client.post(
        "/api/v1/stage2/lx/prompt-variants",
        json={
            "application": "otel-demo",
            "slots": {
                "target": "cart",
                "fault_type": "cpu_load",
                "fault_params": {"cpu_percent": 80},
                "duration_seconds": 300,
            },
        },
    )
    assert variant_response.status_code == 200
    body = variant_response.json()
    prompt = next(item["prompt"] for item in body["variants"] if item["level"] == "L2")
    response = client.post(
        "/api/v1/stage2/lx/runs",
        json={
            "autonomy_level": "L2",
            "prompt": prompt,
            "application": "otel-demo",
            "harness": "codex",
            "model": "gpt-5.5",
            "llm_tag": "test",
            "duration_seconds": 300,
            "variant_set_id": body["variant_set_id"],
        },
    )
    assert response.status_code == 202
    assert response.json()["resolved"]["recovery_trigger"] == "condition_based"


def test_invalid_fault_parameter_is_rejected(tmp_path):
    svc = service(tmp_path)
    request = {
        "application": "otel-demo",
        "slots": {
            "target": "cart",
            "fault_type": "cpu_load",
            "fault_params": {"mem_percent": 80},
            "duration_seconds": 300,
        },
    }
    app = create_app(CampaignSupervisor.__new__(CampaignSupervisor), lx_service=svc)
    assert TestClient(app).post("/api/v1/stage2/lx/prompt-variants", json=request).status_code == 422


def test_manual_prompt_can_be_bound_with_explicit_slots(tmp_path):
    svc = service(tmp_path)
    request = {
        "autonomy_level": "L2",
        "prompt": "请针对 otel-demo 的 cart 服务注入高 CPU 负载故障，在确认故障效果已经出现后立即恢复，并验证业务恢复。",
        "application": "otel-demo",
        "harness": "codex",
        "model": "gpt-5.5",
        "llm_tag": "manual",
        "duration_seconds": 30,
        "slots": {
            "target": "cart",
            "fault_type": "cpu_load",
            "fault_params": {"cpu_percent": 80},
            "duration_seconds": 30,
        },
    }
    app = create_app(CampaignSupervisor.__new__(CampaignSupervisor), lx_service=svc)
    response = TestClient(app).post("/api/v1/stage2/lx/runs", json=request)
    assert response.status_code == 202, response.text
