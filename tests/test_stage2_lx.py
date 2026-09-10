from pathlib import Path

from fastapi.testclient import TestClient

from stage2_service.api import CampaignSupervisor, create_app
from stage2_service.lx import LxRunRequest, LxService, LxSlots, PromptVariantRequest


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
    # L4 deliberately fails lint while its target-disclosure conflict is
    # unresolved; see test_l4_target_disclosure_conflict_is_reported_not_hidden.
    assert all(item["lint"]["passed"] for item in first["variants"] if item["level"] != "L4")


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


class RealisticTaskService:
    """Task service double shaped like the real projection.

    The original fixture returned ``structured_feedback`` as a list, which no
    task projection ever does; that mismatch is what let the summary crash in
    production while the suite stayed green.
    """

    def __init__(self, **overrides):
        self.overrides = overrides
        self.created_count = 0
        self.task_id = "stage2-task-0123456789abcdef"

    def create(self, request, *, idempotency_key=None):
        self.created_count += 1
        return {"task_id": self.task_id, "task_status": "QUEUED"}

    def get(self, task_id, **kwargs):
        task = {
            "task_id": task_id,
            "task_status": "COMPLETED",
            "terminal": True,
            "current_phase": "DONE",
            "elapsed_seconds": 2,
            "event_count": 0,
            "issues": [],
            "error": None,
            # The real projection returns an aggregate mapping here.
            "structured_feedback": {
                "counts": {"facts": 0, "clarification_requests": 0},
                "latest": {"facts": None},
                "assistance_level": "unassisted_or_unobserved",
            },
            "result": {},
        }
        task.update(self.overrides)
        return task

    def abort(self, task_id, request):
        return {"task_id": task_id, "stop_requested": True}


def _run(svc, level="L0"):
    request = PromptVariantRequest(
        application="otel-demo",
        slots=LxSlots(
            target="cart",
            fault_type="cpu_load",
            fault_params={"cpu_percent": 80},
            duration_seconds=300,
        ),
    )
    variants = svc.create_variants(request)
    prompt = next(item["prompt"] for item in variants["variants"] if item["level"] == level)
    return LxRunRequest(
        autonomy_level=level,
        prompt=prompt,
        application="otel-demo",
        harness="bladeai",
        model="gpt-5.5",
        llm_tag="regression",
        duration_seconds=300,
        variant_set_id=variants["variant_set_id"],
    )


def test_summary_reads_aggregate_structured_feedback_without_crashing(tmp_path):
    """Regression: `_counters` iterated the aggregate mapping as a list."""
    fake = RealisticTaskService(
        result={"platform_status": "SUCCEEDED", "trial_count": 1},
        trials=[{"evaluation": {"interaction_ledger": [
            {"initiator": "AGENT", "question": "which target?"},
            {"initiator": "PLATFORM", "answer": "cart"},
        ]}}],
    )
    svc = LxService(task_service=fake, artifact_root=tmp_path, gateway_audit_root=tmp_path)
    summary = svc.create_run(_run(svc))
    assert summary["counters"]["interactions"] == 2
    assert summary["counters"]["questions_asked_by_agent"] == 1


def test_failed_platform_status_is_not_reported_as_success(tmp_path):
    """Regression: a campaign that produced no trial was shown as COMPLETED."""
    fake = RealisticTaskService(result={
        "platform_status": "FAILED",
        "trial_count": 0,
        "error": "PreparationError: logical component cart resolved to 2 Ready Pods",
    })
    svc = LxService(task_service=fake, artifact_root=tmp_path, gateway_audit_root=tmp_path)
    summary = svc.create_run(_run(svc))
    assert summary["status"] == "FAILED"
    assert summary["task_status"] == "COMPLETED"
    assert summary["platform_status"] == "FAILED"
    assert summary["failure"]["code"] == "STAGE2_PLATFORM_FAILED"
    assert "PreparationError" in summary["failure"]["reason"]
    assert summary["failure"]["trial_count"] == 0


def test_replayed_idempotency_key_reuses_the_same_run(tmp_path):
    """Regression: a replay minted a second run id for one task."""
    fake = RealisticTaskService(result={"platform_status": "SUCCEEDED", "trial_count": 1})
    svc = LxService(task_service=fake, artifact_root=tmp_path, gateway_audit_root=tmp_path)
    request = _run(svc)
    first = svc.create_run(request, idempotency_key="replay-1")
    second = svc.create_run(request, idempotency_key="replay-1")
    assert first["run_id"] == second["run_id"]
    assert len([item for item in svc.store.list("lxr") if item.get("run_id")]) == 1


def test_usage_without_gateway_evidence_is_incomplete(tmp_path):
    """Regression: a run that never executed reported complete=True."""
    fake = RealisticTaskService(result={"platform_status": "FAILED", "trial_count": 0})
    svc = LxService(task_service=fake, artifact_root=tmp_path, gateway_audit_root=tmp_path)
    summary = svc.create_run(_run(svc))
    usage = svc.usage(summary["run_id"])
    assert usage["summary"]["total_calls"] == 0
    assert usage["summary"]["complete"] is False
    assert usage["summary"]["coverage"]["reason"] == "no_gateway_usage_evidence"


def test_l4_target_disclosure_conflict_is_reported_not_hidden(tmp_path):
    """L4 names the target while declaring it withheld.

    The conflict is unresolved by decision (2026-09-09): rather than pick a
    reading, the lint reports it so L4 variants fail and L4 runs are blocked,
    instead of scoring an agent for "deriving" a target it was handed. L0--L3
    must stay consistent and lint-clean throughout.
    """
    svc = service(tmp_path)
    variants = {item["level"]: item for item in svc.create_variants(PromptVariantRequest(
        application="otel-demo",
        slots=LxSlots(
            target="cart",
            fault_type="cpu_load",
            fault_params={"cpu_percent": 80},
            duration_seconds=300,
        ),
    ))["variants"]}
    for level in ("L0", "L1", "L2", "L3"):
        item = variants[level]
        named = "cart" in item["prompt"].casefold()
        assert named == ("target" in item["disclosed_slots"]), level
        assert item["lint"]["passed"], (level, item["lint"]["violations"])
    l4 = variants["L4"]
    assert "cart" in l4["prompt"].casefold()
    assert l4["disclosed_slots"] == []
    assert l4["lint"]["passed"] is False
    assert "withheld_target_visible" in l4["lint"]["violations"]


def test_l4_run_is_blocked_while_the_disclosure_conflict_stands(tmp_path):
    """A variant that fails lint must not be runnable."""
    import pytest

    from stage2_service.task_service import TaskValidationError

    fake = RealisticTaskService(result={"platform_status": "SUCCEEDED", "trial_count": 1})
    svc = LxService(task_service=fake, artifact_root=tmp_path, gateway_audit_root=tmp_path)
    with pytest.raises(TaskValidationError, match="lint"):
        svc.create_run(_run(svc, level="L4"))


def test_lint_flags_a_withheld_target_that_leaks_into_the_prompt():
    """The lint had no withheld-target rule, so the one leak that existed passed."""
    from stage2_service.lx import LEVEL_MATRIX, _lint

    slots = LxSlots(
        target="cart",
        fault_type="cpu_load",
        fault_params={"cpu_percent": 80},
        duration_seconds=300,
    )
    # Every level currently discloses the target, so exercise the rule against
    # a level configured to withhold it. This is the guard that was missing:
    # the other three withheld_* rules existed, this one did not.
    monkeypatched = dict(LEVEL_MATRIX["L4"], disclosed_slots=())
    original = LEVEL_MATRIX["L4"]
    LEVEL_MATRIX["L4"] = monkeypatched
    try:
        assert "withheld_target_visible" in _lint("L4", "把 cart 弄慢一点", slots)
    finally:
        LEVEL_MATRIX["L4"] = original
    # A level that discloses the target must not be flagged for naming it.
    assert "withheld_target_visible" not in _lint("L3", "对 cart 做一次受控测试", slots)


def test_percent_intensity_rejects_out_of_range_values():
    """Regression: `accepts` only checked that the value was a finite number."""
    import pytest

    for bad in (999, -50, 0, 100000000):
        with pytest.raises(ValueError):
            LxSlots(
                target="cart",
                fault_type="cpu_load",
                fault_params={"cpu_percent": bad},
                duration_seconds=300,
            )
    ok = LxSlots(
        target="cart",
        fault_type="cpu_load",
        fault_params={"cpu_percent": 100},
        duration_seconds=300,
    )
    assert ok.fault_params["cpu_percent"] == 100
