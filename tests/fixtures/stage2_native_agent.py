"""Native JSONL process fixture; never connects to Kubernetes or model services."""
import json
import os
from pathlib import Path
import sys

scenario = json.loads(Path(__file__).with_suffix(".scenario.json").read_text())["scenario"]
state = Path(os.environ["CODEX_HOME"]) / "fixture-attempts"
attempt = int(state.read_text()) + 1 if state.exists() else 1
state.write_text(str(attempt))


def emit(kind, item):
    print(json.dumps({"type": kind, "item": item}), flush=True)


def say(value):
    emit("item.completed", {"type": "agent_message", "text": value if isinstance(value, str) else json.dumps(value)})


def tool(name, args, result):
    call = {"type": "mcp_tool_call", "id": name, "server": "chaos_control" if name.startswith("chaos") else "k8s_ro",
            "tool": name, "arguments": args, "status": "in_progress", "result": None}
    emit("item.started", call)
    emit("item.completed", {**call, "status": "completed", "result": {"structured_content": result}})


plan = {
    "target": {"namespace": "otel-demo", "name": "cart-a", "uid": "uid-a"},
    "fault_type": "network-delay", "intensity": {"delay_ms": 300},
    "duration_seconds": 45, "maximum_observation_seconds": 45,
    "effect_criterion": "目标请求延迟明显增加", "stop_conditions": ["到期自动恢复"],
}
if scenario in {"startup", "exhausted"} and (attempt == 1 or scenario == "exhausted"):
    print(json.dumps({"type": "error", "message": "service unavailable"}), flush=True)
    sys.exit(1)

print(json.dumps({"type": "thread.started", "thread_id": "fixture-same-session"}), flush=True)
print(json.dumps({"type": "turn.started"}), flush=True)
text = sys.stdin.read()
if "resume" not in sys.argv:
    tool("k8s_list_resources", {"namespace": "otel-demo", "resource": "pods"}, {
        "ok": True, "items": [{"kind": "Pod", "metadata": plan["target"], "status": {"phase": "Running"}}],
    })
    if scenario == "plain_question":
        say("我还没选定具体的 cart Pod，请问下一步应该先做什么？")
        print(json.dumps({"type": "turn.completed"}), flush=True)
        sys.exit(0)
    for delay in ([100, 300] if scenario == "latest" else [300]):
        plan["intensity"]["delay_ms"] = delay
        say({
            "status": "blocked", "decision": "clarification_required",
            "effect_assessment": "not_attempted", "recovery_assessment": "not_applicable",
            "clarification_request": {
                "topic": "experiment_plan", "question": "请告诉我选哪个 Pod" if scenario == "custom" else f"是否同意 {delay}ms 方案？",
                "request_kind": "decision_help" if scenario == "custom" else "confirmation",
                "recommendation": None if scenario == "custom" else plan,
                "required_decisions": ["target_pod", "intensity"], "risk_boundary": "otel-demo 单 Pod",
            },
        })
else:
    feedback = json.loads(text.split("```json\n", 1)[1].split("```", 1)[0])
    payload = feedback["payload"]
    if payload.get("event_type") != "OUTPUT_REPAIR":
        plan = payload["approved_plan"]
        args = {"namespace": plan["target"]["namespace"], "target_name": plan["target"]["name"],
                "target_uid": plan["target"]["uid"], "fault_type": plan["fault_type"],
                "intensity": plan["intensity"], "duration_seconds": plan["duration_seconds"]}
        tool("chaos_validate_plan", args, {"ok": True, "findings": []})
        tool("chaos_create_experiment", args, {"ok": True, "created": {"phase": "Initialized"}})
        tool("chaos_operation_status", {}, {"ok": True, "operation_outcome": "applied", "live": {"phase": "Running"}, "target_uid": "uid-a"})
        tool("chaos_recovery_status", {}, {"ok": True, "resource_absent": True, "phase": "Absent"})
    if scenario == "repair" and payload.get("event_type") != "OUTPUT_REPAIR":
        say("[unreadable final answer]")
    elif scenario == "plain":
        say("故障对象已清除，但没有取得请求级效果证据，效果未验证。")
    else:
        say({
            "status": "completed", "decision": "safe_stop", "effect_assessment": "verified",
            "recovery_assessment": "unverified", "missing_conditions": ["没有取得请求级效果证据"],
            "remaining_risk": "请求级延迟效果未验证", "recovery_trigger": {"condition": "到期自动恢复"},
        })
print(json.dumps({"type": "turn.completed"}), flush=True)
