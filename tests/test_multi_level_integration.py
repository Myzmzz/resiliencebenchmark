import json
from pathlib import Path

import jsonschema

from controller.disturbance import ControllerDisturbanceSafetyGate
from controller.safety import default_policy
from disturbances.injector import (
    DisturbanceInjector,
    InMemoryControllerRecordSink,
    KubernetesDisturbanceAdapter,
    TelemetryInterceptorAdapter,
)
from disturbances.types import DisturbancePhase, DisturbanceSpec, DisturbanceType, LifecycleEvent
from evaluator.evaluator import evaluate_level, simplified_level_contract
from progression.builder import build_multi_level_episode
from scoring.calculator import calculate_episode_score
from scripts.run_harness_trial import run_multi_level_episode


REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeKubernetes:
    def __init__(self):
        self.restarts = 0

    def restart_exact_pod(self, **kwargs):
        self.restarts += 1
        return {"name": kwargs["name"], "uid": f"replacement-{self.restarts}"}


class FakeTelemetryInterceptor:
    def __init__(self):
        self.rules = []

    def register_rule(self, **kwargs):
        self.rules.append(kwargs)
        return f"rule-{len(self.rules)}"

    def remove_rule(self, rule_id):
        return bool(rule_id)


def test_three_level_episode_retries_then_passes_with_disturbance_evidence(tmp_path):
    plan = {
        "episode_id": "EPI-INTEGRATION-001",
        "defect_ref": "RBD-001",
        "application_snapshot": {
            "application": "otel-demo",
            "namespace": "otel-demo",
            "runtime_target": {
                "kind": "Pod",
                "name": "checkout-abc",
                "uid": "uid-original",
            },
        },
        "action_space": {
            "allowed_trigger_classes": ["pod-kill"],
            "selected_actuator": "pod-kill",
            "parameters": [{"name": "pod_count", "value": 1}],
        },
        "budget": {"max_experiments": 6},
    }
    episode = build_multi_level_episode(plan)
    episode_path = tmp_path / "episode.yaml"
    import yaml

    episode_path.write_text(yaml.safe_dump(episode, sort_keys=False), encoding="utf-8")
    k8s = FakeKubernetes()
    telemetry = FakeTelemetryInterceptor()
    gate = ControllerDisturbanceSafetyGate(
        default_policy({"otel-demo"}),
        allowed_types={item.value for item in DisturbanceType},
    )
    target = episode["base_task"]["target"]

    def injector_factory(ticket, level):
        specs = [DisturbanceSpec.from_mapping(item) for item in level["disturbances"]]
        return DisturbanceInjector(
            run_id=ticket.run_id,
            level_id=ticket.level_id,
            trial_id=ticket.trial_id,
            attempt=ticket.attempt,
            specs=specs,
            target=target,
            safety_gate=gate,
            adapters={
                "kubernetes": KubernetesDisturbanceAdapter(k8s),
                "telemetry_interceptor": TelemetryInterceptorAdapter(telemetry),
            },
            record_sink=InMemoryControllerRecordSink(),
        )

    def runner(ticket, level, emit):
        emit(
            LifecycleEvent(
                run_id=ticket.run_id,
                level_id=ticket.level_id,
                phase=DisturbancePhase.EXECUTION,
                kind="main_fault_applied",
            )
        )
        for _ in range(2):
            emit(
                LifecycleEvent(
                    run_id=ticket.run_id,
                    level_id=ticket.level_id,
                    phase=DisturbancePhase.OBSERVATION,
                    kind="tool_call",
                    tool="telemetry_ro.telemetry_prom_metric_range",
                )
            )
        return {"status": "completed", "trace_ref": f"harness://{ticket.trial_id}"}

    l2_failed_once = False
    record_counts = {}

    def level_evaluator(ticket, level, trial_report, controller_records):
        nonlocal l2_failed_once
        record_counts[ticket.trial_id] = len(controller_records)
        statuses = {
            "precondition": "PASS",
            "fault_effect": "PASS",
            "diagnosis": "PASS",
            "recovery": "PASS",
            "safety": "PASS",
        }
        if ticket.level_id == "L2" and not l2_failed_once:
            statuses["diagnosis"] = "FAIL"
            l2_failed_once = True
        behaviors = [
            {
                "behavior_id": behavior,
                "status": "PASS",
                "evidence_sources": [
                    {"kind": "runtime_system", "ref": f"harness://{ticket.trial_id}/{behavior}"}
                ],
            }
            for disturbance in level["disturbances"]
            for behavior in disturbance["expected_behaviors"]
        ]
        observation = {
            "episode_id": episode["episode_id"],
            "gate_results": [
                {
                    "gate_id": gate_id,
                    "status": status,
                    "evidence_sources": [
                        {"kind": "independent_observer", "ref": f"oracle://{ticket.trial_id}/{gate_id}"}
                    ],
                }
                for gate_id, status in statuses.items()
            ],
            "disturbance_behaviors": behaviors,
        }
        result = evaluate_level(
            simplified_level_contract(episode["episode_id"]),
            observation,
            run_id=ticket.run_id,
            level=level,
            attempt=ticket.attempt,
            metrics={"duration_seconds": 10, "tool_calls": 5, "tokens_used": 100},
        )
        return result

    report = run_multi_level_episode(
        REPO_ROOT,
        episode_file=episode_path,
        run_id="run-integration",
        agent_id="agent-a",
        trial_runner=runner,
        level_evaluator=level_evaluator,
        injector_factory=injector_factory,
    )

    assert report["status"] == "PASS"
    assert [(item["level_id"], item["attempt"], item["primary_status"]) for item in report["level_results"]] == [
        ("L1", 1, "PASS"),
        ("L2", 1, "FAIL"),
        ("L2", 2, "PASS"),
        ("L3", 1, "PASS"),
    ]
    assert k8s.restarts == 3
    assert len(telemetry.rules) == 1
    assert record_counts["run-integration-L3-a1"] == 5
    score = calculate_episode_score(
        episode_id=episode["episode_id"],
        agent_id="agent-a",
        level_results=report["level_results"],
        total_levels=3,
        efficiency_score=1.0,
    )
    jsonschema.validate(
        score,
        json.loads((REPO_ROOT / "scoring/schemas/episode-score.schema.json").read_text()),
    )
    assert score["breakdown"]["levels_passed"] == 3
