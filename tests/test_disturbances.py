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
from disturbances.types import (
    DisturbancePhase,
    DisturbanceType,
    LifecycleEvent,
    derive_replay_seed,
    load_disturbance_library,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
TARGET = {
    "application": "otel-demo",
    "namespace": "otel-demo",
    "kind": "Pod",
    "name": "checkout-abc",
    "uid": "uid-old",
}


class FakeKubernetes:
    def __init__(self):
        self.calls = []

    def restart_exact_pod(self, **kwargs):
        self.calls.append(("restart", kwargs))
        return {"name": kwargs["name"], "uid": "uid-new"}

    def read_resource_quota(self, **kwargs):
        self.calls.append(("read", kwargs))
        return {"hard": {"requests.cpu": "4"}}

    def patch_resource_quota(self, **kwargs):
        self.calls.append(("patch", kwargs))
        return {"hard": {"requests.cpu": "2.8"}}

    def restore_resource_quota(self, **kwargs):
        self.calls.append(("restore", kwargs))
        return kwargs["previous"]


class FakeInterceptor:
    def __init__(self):
        self.rules = []

    def register_rule(self, **kwargs):
        self.rules.append(kwargs)
        return f"rule-{len(self.rules)}"

    def remove_rule(self, rule_id):
        self.rules.append({"removed": rule_id})
        return True

    def events(self):
        return [
            {"rule_id": "rule-1", "status": "matched", "detail": {"call_slot": 2}}
        ]


def make_gate(namespace="otel-demo"):
    return ControllerDisturbanceSafetyGate(
        default_policy({namespace}),
        allowed_types={item.value for item in DisturbanceType},
    )


def test_library_defines_all_eight_reproducible_disturbances():
    library = load_disturbance_library()

    assert set(library) == set(DisturbanceType)
    assert all(item.reproducible for item in library.values())
    assert all(item.expected_behaviors and item.verification for item in library.values())


def test_replay_seed_is_stable_and_input_sensitive():
    first = derive_replay_seed("EPI-1", "L2", "target_drift")

    assert first == derive_replay_seed("EPI-1", "L2", "target_drift")
    assert first != derive_replay_seed("EPI-1", "L3", "target_drift")


def test_target_drift_triggers_once_and_records_distinct_uid():
    definition = load_disturbance_library()[DisturbanceType.TARGET_DRIFT]
    spec = definition.instantiate(disturbance_id="L2-D1-target", replay_seed=42)
    client = FakeKubernetes()
    sink = InMemoryControllerRecordSink()
    injector = DisturbanceInjector(
        run_id="run-001",
        level_id="L2",
        specs=[spec],
        target=TARGET,
        safety_gate=make_gate(),
        adapters={"kubernetes": KubernetesDisturbanceAdapter(client)},
        record_sink=sink,
    )
    event = LifecycleEvent(
        run_id="run-001",
        level_id="L2",
        phase=DisturbancePhase.EXECUTION,
        kind="main_fault_applied",
    )

    records = injector.process_event(event)
    duplicate = injector.process_event(event)

    assert duplicate == []
    assert [item["status"] for item in records] == ["triggered", "completed"]
    assert records[-1]["outcome"]["old_uid"] == "uid-old"
    assert records[-1]["outcome"]["replacement_uid"] == "uid-new"
    assert len(client.calls) == 1
    schema = json.loads(
        (REPO_ROOT / "disturbances/schemas/disturbance-event.schema.json").read_text()
    )
    for item in sink.records:
        jsonschema.validate(item, schema)


def test_telemetry_schedule_is_deterministic_and_cleanup_removes_rule():
    definition = load_disturbance_library()[DisturbanceType.TELEMETRY_INSTABILITY]
    spec = definition.instantiate(disturbance_id="L3-D2-telemetry", replay_seed=91)
    client = FakeInterceptor()
    sink = InMemoryControllerRecordSink()
    injector = DisturbanceInjector(
        run_id="run-002",
        level_id="L3",
        specs=[spec],
        target=TARGET,
        safety_gate=make_gate(),
        adapters={"telemetry_interceptor": TelemetryInterceptorAdapter(client)},
        record_sink=sink,
    )

    for _ in range(2):
        injector.process_event(
            LifecycleEvent(
                run_id="run-002",
                level_id="L3",
                phase=DisturbancePhase.OBSERVATION,
                kind="tool_call",
                tool="telemetry_ro.telemetry_prom_metric_range",
            )
        )
    cleanup = injector.cleanup_all()

    schedule = client.rules[0]["rule"]["slots"]
    assert schedule == sorted(schedule)
    assert len(schedule) == 3
    assert cleanup[-1]["status"] == "cleaned"
    assert client.rules[-1] == {"removed": "rule-1"}
    assert cleanup[-1]["outcome"]["interceptor_events"][0]["status"] == "matched"


def test_safety_gate_rejects_target_outside_namespace_without_calling_backend():
    definition = load_disturbance_library()[DisturbanceType.TARGET_DRIFT]
    client = FakeKubernetes()
    injector = DisturbanceInjector(
        run_id="run-003",
        level_id="L2",
        specs=[definition.instantiate(disturbance_id="L2-D1", replay_seed=1)],
        target={**TARGET, "namespace": "production"},
        safety_gate=make_gate(),
        adapters={"kubernetes": KubernetesDisturbanceAdapter(client)},
        record_sink=InMemoryControllerRecordSink(),
    )

    records = injector.process_event(
        LifecycleEvent(
            run_id="run-003",
            level_id="L2",
            phase=DisturbancePhase.EXECUTION,
            kind="main_fault_applied",
        )
    )

    assert records[0]["status"] == "rejected"
    assert "NAMESPACE_NOT_ALLOWED" in records[0]["outcome"]["safety_reasons"]
    assert client.calls == []
