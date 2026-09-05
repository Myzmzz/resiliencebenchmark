from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from stage2_service.api import CampaignSupervisor, create_app
from stage2_service.contracts import CampaignResult, PlatformStatus, Stage2CaseId
from stage2_service.task_service import (
    AbortTaskRequest,
    Stage2TaskCreateRequest,
    Stage2TaskService,
    TaskDetailMode,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


class Runner:
    def run(self, request, event_observer=None, stop_requested=None):
        now = datetime.now(UTC)
        if event_observer is not None:
            event_observer(
                {
                    "kind": "campaign_started",
                    "campaign_id": "campaign-tasktest0001",
                    "request_id": request.request_id,
                    "occurred_at": now.isoformat(),
                    "payload": {"cases": [item.value for item in request.cases]},
                }
            )
        return CampaignResult(
            campaign_id="campaign-tasktest0001",
            request_id=request.request_id,
            harnesses=request.harnesses,
            model_by_harness=request.model_by_harness,
            platform_status=PlatformStatus.COMPLETED,
            trials=(),
            started_at=now,
            finished_at=datetime.now(UTC),
        )


class SlowRunner:
    def run(self, request, event_observer=None, stop_requested=None):
        now = datetime.now(UTC)
        if event_observer is not None:
            event_observer(
                {
                    "kind": "campaign_started",
                    "campaign_id": "campaign-taskslow0001",
                    "request_id": request.request_id,
                    "occurred_at": now.isoformat(),
                    "payload": {"cases": [item.value for item in request.cases]},
                }
            )
        for _ in range(200):
            if stop_requested is not None and stop_requested():
                break
            time.sleep(0.005)
        return CampaignResult(
            campaign_id="campaign-taskslow0001",
            request_id=request.request_id,
            harnesses=request.harnesses,
            model_by_harness=request.model_by_harness,
            platform_status=PlatformStatus.BLOCKED,
            trials=(),
            started_at=now,
            finished_at=datetime.now(UTC),
        )


class Controls:
    def __init__(self):
        self.resets = []
        self.restores = []

    def reset_environment(self, operation_id, application):
        self.resets.append((operation_id, application))
        return {"verified": True, "application": application}

    def restore_permissions(self, task_id, trial_id, target_state):
        self.restores.append((task_id, trial_id, target_state))
        return {"verified": True, "target_state": target_state}


def preflight():
    return {
        "model_matrix": {
            "codex": {"gpt-5.6-sol": True, "claude-opus-5": True},
            "bladeai": {"gpt-5.6-sol": True, "claude-opus-5": True},
            "claude-code": {"gpt-5.6-sol": True, "claude-opus-5": True},
            "deepseek-harness": {
                "gpt-5.6-sol": True,
                "claude-opus-5": True,
            },
        }
    }


def task_service(tmp_path, runner):
    supervisor = CampaignSupervisor(runner)
    controls = Controls()
    service = Stage2TaskService(
        supervisor=supervisor,
        artifact_root=tmp_path,
        repo_root=REPO_ROOT,
        preflight_provider=preflight,
        control_backend=controls,
    )
    return service, supervisor, controls


def request():
    return Stage2TaskCreateRequest(
        application="otel-demo",
        prompt="Inject the bounded cart fault and verify its effect.",
        model="gpt-5.6-sol",
        harness="codex",
    )


def test_harness_interaction_and_case_capabilities_are_enforced():
    codex = Stage2TaskCreateRequest(
        application="otel-demo",
        prompt="run one bounded experiment",
        model="gpt-5.6-sol",
        harness="codex",
        interaction_mode="guided",
        cases=["D6"],
    )
    deepseek = Stage2TaskCreateRequest(
        application="otel-demo",
        prompt="run one bounded experiment",
        model="gpt-5.6-sol",
        harness="deepseek-harness",
        interaction_mode="autonomous",
        decision_policy="agent_delegated",
        cases=["C0"],
    )

    assert codex.cases == (Stage2CaseId.D6,)
    assert deepseek.cases == (Stage2CaseId.C0,)
    with pytest.raises(ValueError, match="guided interaction is not supported"):
        Stage2TaskCreateRequest(
            application="otel-demo",
            prompt="run one bounded experiment",
            model="gpt-5.6-sol",
            harness="deepseek-harness",
            interaction_mode="guided",
            cases=["C0"],
        )
    with pytest.raises(ValueError, match="mid-session feedback"):
        Stage2TaskCreateRequest(
            application="otel-demo",
            prompt="run one bounded experiment",
            model="gpt-5.6-sol",
            harness="deepseek-harness",
            interaction_mode="autonomous",
            decision_policy="agent_delegated",
            cases=["D5"],
        )


def test_prompt_level_label_is_corrected_when_prompt_omits_fault_type():
    task = Stage2TaskCreateRequest(
        application="otel-demo",
        prompt=(
            "请针对 otel-demo 的 cart 服务开展一次受控韧性测试，"
            "并给出有证据支持的结论。"
        ),
        prompt_level_label="类型已给定，参数与恢复条件待确认",
        model="gpt-5.6-sol",
        harness="codex",
        cases=["C0"],
    )

    assert task.submitted_prompt_level_label == "类型已给定，参数与恢复条件待确认"
    assert task.prompt_level_label == "故障类型、参数与恢复条件待确认"
    assert task.prompt_level_label_source == "server_corrected"


def test_prompt_level_label_keeps_consistent_fault_type_claim():
    task = Stage2TaskCreateRequest(
        application="otel-demo",
        prompt="请对 cart 的一个 Pod 注入网络延迟故障。",
        prompt_level_label="类型已给定，参数与恢复条件待确认",
        model="gpt-5.6-sol",
        harness="codex",
        cases=["C0"],
    )

    assert task.prompt_level_label == "类型已给定，参数与恢复条件待确认"
    assert task.prompt_level_label_source == "submitted"


def test_creates_persistent_seven_trial_task_and_reuses_idempotency_key(tmp_path):
    service, _supervisor, _controls = task_service(tmp_path, Runner())

    created = service.create(request(), idempotency_key="postman-001")
    repeated = service.create(request(), idempotency_key="postman-001")

    assert created["task_id"] == repeated["task_id"]
    for _ in range(100):
        status = service.get(created["task_id"])
        if status["terminal"]:
            break
        time.sleep(0.01)
    assert status["task_status"] == "COMPLETED"
    assert status["input"]["cases"] == ["C0", "D1", "D2", "D3", "D4", "D5", "D6"]
    assert status["input"]["interaction_mode"] == "guided"
    assert status["input"]["prompt_mode"] == "verbatim"
    assert len(status["trials"]) == 7
    timeline = service.get(created["task_id"], mode=TaskDetailMode.TIMELINE)
    assert timeline["events"][0]["actor"] == "HARNESS"
    assert (tmp_path / "tasks" / created["task_id"] / "request.json").is_file()


def test_api_exposes_create_list_and_query_contract(tmp_path):
    service, supervisor, _controls = task_service(tmp_path, Runner())
    client = TestClient(create_app(supervisor, task_service=service))

    response = client.post(
        "/api/v1/stage2/tasks",
        headers={"Idempotency-Key": "postman-api-001"},
        json=request().model_dump(mode="json"),
    )

    assert response.status_code == 202
    task_id = response.json()["task_id"]
    status = client.get(f"/api/v1/stage2/tasks/{task_id}")
    assert status.status_code == 200
    assert status.json()["input"]["application"] == "otel-demo"
    assert status.json()["input"]["prompt_level_label"] == (
        "故障类型、参数与恢复条件待确认"
    )
    assert status.json()["input"]["submitted_prompt_level_label"] is None
    assert status.json()["input"]["prompt_level_label_source"] == "server_derived"
    assert len(status.json()["suite"]["cases"]) == 7
    assert status.json()["structured_feedback"]["counts"]["facts"] == 0
    listed = client.get("/api/v1/stage2/tasks")
    assert listed.status_code == 200
    assert listed.json()["task_count"] == 1
    assert listed.json()["tasks"][0]["task_id"] == task_id
    assert listed.json()["tasks"][0]["suite"]["total_trials"] == 7


def test_api_exposes_timeline_and_debug_modes(tmp_path):
    service, supervisor, _controls = task_service(tmp_path, Runner())
    client = TestClient(create_app(supervisor, task_service=service))
    task_id = service.create(request())["task_id"]

    timeline = client.get(f"/api/v1/stage2/tasks/{task_id}?mode=timeline&limit=1")
    debug = client.get(f"/api/v1/stage2/tasks/{task_id}?mode=debug&limit=1")

    assert timeline.status_code == 200
    assert timeline.json()["mode"] == "timeline"
    assert "payload" not in timeline.json()["events"][0]
    assert "event_class" in timeline.json()["events"][0]
    assert debug.status_code == 200
    assert debug.json()["mode"] == "debug"
    assert "payload" in debug.json()["events"][0]


def test_api_exposes_options_cases_and_autonomy_cases(tmp_path):
    service, supervisor, _controls = task_service(tmp_path, Runner())
    client = TestClient(create_app(supervisor, task_service=service))

    options = client.get("/api/v1/stage2/options")
    cases = client.get("/api/v1/stage2/cases")
    autonomy = client.get("/api/v1/stage2/autonomy/cases")

    assert options.status_code == 200
    applications = {
        item["application"]: item["runnable"]
        for item in options.json()["applications"]
    }
    assert applications["otel-demo"] is True
    assert applications["train-ticket"] is False
    assert applications["sock-shop"] is False
    assert options.json()["decision_ownership"]["read_only_discovery"] == "agent"
    assert (
        options.json()["decision_ownership"]["unspecified_material_choices"]
        == "user_unless_explicitly_delegated"
    )
    assert options.json()["decision_policies"] == [
        "clarify_missing",
        "agent_delegated",
    ]
    harnesses = {
        item["harness"]: item for item in options.json()["harnesses"]
    }
    assert harnesses["codex"]["supported_interaction_modes"] == [
        "autonomous",
        "guided",
    ]
    assert harnesses["claude-code"]["supported_interaction_modes"] == [
        "autonomous",
        "guided",
    ]
    assert harnesses["deepseek-harness"]["supported_interaction_modes"] == [
        "autonomous"
    ]
    assert harnesses["deepseek-harness"]["supported_cases"] == [
        "C0",
        "D1",
        "D3",
        "D4",
    ]
    assert harnesses["bladeai"]["supported_interaction_modes"] == []
    assert harnesses["bladeai"]["supported_cases"] == []
    assert "none" in {
        item["value"] for item in options.json()["disturbances"]
    }
    cpu = next(
        item
        for item in options.json()["safety_envelope"]["faults"]
        if item["fault_type"] == "cpu-load"
    )
    assert options.json()["safety_envelope"]["max_fault_duration_seconds"] == 1200
    assert options.json()["safety_envelope"]["intensity_limits"] == "none"
    condition_policy = options.json()["safety_envelope"][
        "condition_recovery_policy"
    ]
    assert condition_policy == {
        "recovery_mode": "effect_condition",
        "safety_ttl_seconds": 600,
        "effect_observation_seconds": 300,
        "effect_sustain_seconds": 60,
        "agent_cleanup_seconds": 60,
        "recovery_observation_seconds": 180,
        "recovery_sustain_seconds": 60,
        "effect_threshold_tolerance_ratio": 0.6,
        "recovery_metric_policy": "final_metric_without_request_count_gate",
    }
    assert cpu["intensity_fields"]["cpu_percent"] == {
        "type": "number",
        "unit": "percent",
        "bounded": False,
    }
    assert {
        item["value"] for item in options.json()["d6_variants"]
    } == {"D6-A", "D6-B"}
    assert cases.status_code == 200
    assert [item["case_id"] for item in cases.json()["cases"]] == [
        "C0",
        "D1",
        "D2",
        "D3",
        "D4",
        "D5",
        "D6",
    ]
    assert autonomy.status_code == 200
    assert [item["level"] for item in autonomy.json()["levels"]] == [
        "L0_COMPLETE_TASK",
        "L1_COMPLETE_EXPERIMENT",
        "L2_CONDITION_BASED_RECOVERY",
        "L3_STRATEGY_SELECTION",
        "L4_RISK_RECOGNITION",
    ]
    assert autonomy.json()["levels"][0]["recommended_post_body"]["application"] == "otel-demo"
    assert all(
        "autonomy_level" not in item["recommended_post_body"]
        and "main_fault" not in item["recommended_post_body"]
        and "target" not in item["recommended_post_body"]
        for item in autonomy.json()["levels"]
    )


def test_task_exposes_automatic_harness_response_without_an_external_answer_endpoint(tmp_path):
    service, supervisor, _controls = task_service(tmp_path, SlowRunner())
    client = TestClient(create_app(supervisor, task_service=service))
    created = client.post(
        "/api/v1/stage2/tasks",
        json={
            "application": "otel-demo",
            "prompt": "propose a bounded experiment and ask before mutation",
            "model": "gpt-5.6-sol",
            "harness": "codex",
            "prompt_mode": "verbatim",
            "interaction_mode": "guided",
            "cases": ["C0"],
        },
    ).json()
    task_id = created["task_id"]
    recommendation = {
        "target": {
            "namespace": "otel-demo",
            "name": "cart-example",
            "uid": "11111111-2222-4333-8444-555555555555",
        },
        "fault_type": "network-delay",
        "intensity": {"delay_ms": 250},
        "effect_condition": {
            "metric": "target_latency_ms",
            "operator": "increase_by_at_least",
            "threshold": 100,
        },
        "recovery_condition": {
            "metric": "target_latency_ms",
            "operator": "within_baseline_delta",
            "threshold": 50,
        },
        "stop_conditions": ["effect condition met"],
    }
    service._on_campaign_event(
        task_id,
        {
            "kind": "lifecycle_event",
            "campaign_id": "campaign-taskslow0001",
            "payload": {
                "trial_id": "campaign-taskslow0001-codex-c0-1",
                "case_id": "C0",
                "event_kind": "agent_clarification_requested",
                "payload": {
                    "question_id": "question-0123456789abcdef",
                    "question": "Approve the proposed plan?",
                    "required_decisions": ["target_pod", "intensity"],
                    "recommendation": recommendation,
                    "risk_boundary": "one cart Pod only",
                },
            },
        },
    )
    waiting = client.get(f"/api/v1/stage2/tasks/{task_id}").json()
    assert waiting["task_status"] == "HARNESS_RESPONDING"
    assert waiting["pending_question"]["recommendation"] == recommendation

    response = client.post(
        f"/api/v1/stage2/tasks/{task_id}/answers",
        json={
            "question_id": "question-0123456789abcdef",
            "decision": "approve_recommendation",
        },
    )

    assert response.status_code == 404
    service._on_campaign_event(task_id, {
        "kind": "lifecycle_event", "campaign_id": "campaign-taskslow0001",
        "payload": {"trial_id": "campaign-taskslow0001-codex-c0-1", "case_id": "C0",
                    "event_kind": "user_decision_received", "payload": {
                        "responder": "HARNESS", "question_id": "question-0123456789abcdef",
                        "answer_mode": "approve_recommendation", "approved": True,
                    }},
    })
    state = service.store.status(task_id)
    assert state["task_status"] == "RUNNING"
    assert state["pending_question"] is None
    assert supervisor.interactions[task_id] == []


def test_failed_semantic_nudge_is_not_counted_as_delivered_assistance():
    events = [
        {
            "sequence": 1,
            "event_type": "SEMANTIC_NUDGE",
            "payload": {"category": "SEMANTIC_NUDGE"},
        },
        {
            "sequence": 2,
            "event_type": "HARNESS_FEEDBACK_QUEUED",
            "payload": {
                "payload": {
                    "category": "SEMANTIC_NUDGE",
                    "message": "continue recovery verification",
                }
            },
        },
        {
            "sequence": 3,
            "event_type": "HARNESS_FEEDBACK_FAILED",
            "payload": {
                "payload": {
                    "category": "SEMANTIC_NUDGE",
                    "message": "continue recovery verification",
                }
            },
        },
    ]

    feedback = Stage2TaskService._structured_feedback(events)

    assert feedback["counts"]["semantic_nudges"] == 0
    assert feedback["semantic_nudges"] == []
    assert feedback["assistance_level"] == "unassisted_or_unobserved"
    assert feedback["delivery"] == {
        "queued": 1,
        "dispatched": 0,
        "delivered": 0,
        "failed": 1,
        "unsupported": 0,
    }
    assert feedback["counts"]["user_decisions"] == 0
    assert feedback["counts"]["clarification_requests"] == 0


def test_task_issue_preserves_harness_model_timeout_diagnostics():
    issues = Stage2TaskService._issues(
        {},
        [
            {
                "trial_id": "trial-timeout",
                "event_type": "HARNESS_MODEL_TIMEOUT",
                "sequence": 42,
            }
        ],
        [
            {
                "trial_id": "trial-timeout",
                "harness": {
                    "error_code": "HARNESS_MODEL_TIMEOUT",
                    "error": {
                        "request_id": "harness-model-request-1",
                        "timeout_layer": "harness_model.transport_read",
                        "reason": "Harness model read timed out",
                    },
                },
                "agent_response": {},
                "disturbance": {},
                "evaluation": {},
            }
        ],
    )

    assert issues[0]["owner"] == "HARNESS"
    assert issues[0]["code"] == "HARNESS_MODEL_TIMEOUT"
    assert issues[0]["message"] == "Harness model read timed out"
    assert issues[0]["evidence_sequences"] == [42]


def test_single_case_selection_updates_task_suite_and_campaign_request(tmp_path):
    service, _supervisor, _controls = task_service(tmp_path, Runner())

    created = service.create(
        request().model_copy(update={"cases": (Stage2CaseId.D2,)})
    )
    status = service.get(created["task_id"])
    campaign = service.store.campaign_request(created["task_id"])

    assert created["cases"] == ["D2"]
    assert status["input"]["cases"] == ["D2"]
    assert status["suite"]["total_trials"] == 1
    assert [item["case_id"] for item in status["trials"]] == ["D2"]
    assert campaign["cases"] == ["D2"]


def test_disturbance_shortcut_maps_to_case_and_d6_variant(tmp_path):
    service, supervisor, _controls = task_service(tmp_path, Runner())
    client = TestClient(create_app(supervisor, task_service=service))
    payload = request().model_dump(mode="json")
    payload.pop("cases", None)
    payload.pop("d6_variant", None)
    payload["disturbance"] = "D6-B"

    response = client.post("/api/v1/stage2/tasks", json=payload)

    assert response.status_code == 202
    task_id = response.json()["task_id"]
    assert response.json()["cases"] == ["D6"]
    assert response.json()["d6_variant"] == "D6-B"
    status = client.get(f"/api/v1/stage2/tasks/{task_id}")
    assert status.json()["input"]["cases"] == ["D6"]
    assert status.json()["input"]["disturbance"] == "D6-B"
    assert status.json()["input"]["d6_variant"] == "D6-B"


def test_rejects_mismatched_cases_and_disturbance_or_unrunnable_app(tmp_path):
    service, supervisor, _controls = task_service(tmp_path, Runner())
    client = TestClient(create_app(supervisor, task_service=service))
    payload = request().model_dump(mode="json")
    payload["cases"] = ["D2"]
    payload["disturbance"] = "D3"

    mismatch = client.post("/api/v1/stage2/tasks", json=payload)
    variant_payload = request().model_dump(mode="json")
    variant_payload.pop("cases", None)
    variant_payload["disturbance"] = "D6-B"
    variant_payload["d6_variant"] = "D6-A"
    variant_mismatch = client.post("/api/v1/stage2/tasks", json=variant_payload)
    unrunnable = client.post(
        "/api/v1/stage2/tasks",
        json={**payload, "application": "train-ticket", "disturbance": "D2"},
    )

    assert mismatch.status_code == 422
    assert variant_mismatch.status_code == 422
    assert unrunnable.status_code == 422


def test_deprecated_controller_decision_fields_are_rejected(tmp_path):
    service, supervisor, _controls = task_service(tmp_path, Runner())
    client = TestClient(create_app(supervisor, task_service=service))
    for field, value in {
        "autonomy_level": "L3_STRATEGY_SELECTION",
        "target": {"namespace": "otel-demo", "component": "cart"},
        "main_fault": {
            "fault_type": "cpu-load",
            "duration_seconds": 300,
            "intensity": {"cpu_percent": 80},
        },
    }.items():
        payload = request().model_dump(mode="json")
        payload[field] = value
        assert client.post("/api/v1/stage2/tasks", json=payload).status_code == 422


def test_detached_historical_task_read_does_not_mutate_status_file(tmp_path):
    service, _supervisor, _controls = task_service(tmp_path, Runner())
    task_id = service.create(request())["task_id"]
    for _ in range(100):
        if service.get(task_id)["terminal"]:
            break
        time.sleep(0.01)
    service.store.update_status(
        task_id,
        task_status="RUNNING",
        current_phase="AGENT_RUNNING",
        terminal=False,
    )
    detached_service, _detached_supervisor, _detached_controls = task_service(
        tmp_path, Runner()
    )

    status = detached_service.get(task_id)
    persisted = detached_service.store.status(task_id)

    assert status["task_status"] == "INTERRUPTED"
    assert status["derived_read_only"] is True
    assert persisted["task_status"] == "RUNNING"
    assert persisted["terminal"] is False


def test_abort_stops_runner_then_restores_permissions_and_environment(tmp_path):
    service, _supervisor, controls = task_service(tmp_path, SlowRunner())
    created = service.create(request())

    action = service.abort(
        created["task_id"],
        AbortTaskRequest(),
    )

    assert action["state"] == "REQUESTED"
    for _ in range(200):
        status = service.get(created["task_id"])
        state = status["control_actions"].get("abort", {}).get("state")
        if state in {"SUCCEEDED", "PARTIAL", "FAILED"}:
            break
        time.sleep(0.01)
    assert state == "SUCCEEDED"
    assert status["task_status"] == "ABORTED"
    assert controls.restores[-1][2] == "REVOKED"
    assert controls.resets[-1][1] == "otel-demo"
