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
    "effect_condition": {
        "metric": "target_latency_ms", "operator": "increase_by_at_least",
        "threshold": 100,
    },
    "recovery_condition": {
        "metric": "target_latency_ms", "operator": "within_baseline_delta",
        "threshold": 50,
    },
    "stop_conditions": ["效果条件成立后主动恢复"],
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
    if scenario == "approval_repair":
        say("请确认上述完整计划：仅对 otel-demo/cart-a 注入300ms网络延迟，效果持续成立后主动清理，并使用安全 TTL 兜底。")
        print(json.dumps({"type": "turn.completed"}), flush=True)
        sys.exit(0)
    if scenario == "model_timeout":
        say("目标已找到，但这条回答需要 Harness 解释。")
        print(json.dumps({"type": "turn.completed"}), flush=True)
        sys.exit(0)
    for delay in ([100, 300] if scenario == "latest" else [300]):
        plan["intensity"]["delay_ms"] = delay
        say({
            "status": "blocked", "decision": "clarification_required",
            "effect_assessment": "not_attempted", "recovery_assessment": "not_applicable",
            "clarification_request": {
                "topic": "experiment_plan", "question": "请提供一个目标 Pod，其他参数我来定。" if scenario == "advice" else "请告诉我选哪个 Pod" if scenario == "custom" else f"是否同意 {delay}ms 方案？",
                "request_kind": "decision_help" if scenario in {"custom", "advice"} else "confirmation",
                "recommendation": None if scenario in {"custom", "advice"} else plan,
                "required_decisions": ["target_pod", "intensity"], "risk_boundary": "otel-demo 单 Pod",
            },
        })
else:
    feedback = json.loads(text.split("```json\n", 1)[1].split("```", 1)[0])
    payload = feedback["payload"]
    if scenario == "advice" and payload.get("approved") is None:
        assert payload["answer_mode"] == "custom"
        assert payload["supplied_plan"]["target"]["uid"] == "uid-a"
        say({"status": "blocked", "decision": "clarification_required", "clarification_request": {
            "topic": "experiment_plan", "question": "目标已收到。是否确认我补齐的300ms条件恢复方案？",
            "recommendation": plan, "required_decisions": ["intensity", "stop_conditions"],
        }})
        print(json.dumps({"type": "turn.completed"}), flush=True)
        sys.exit(0)
    if payload.get("event_type") != "OUTPUT_REPAIR":
        plan = payload["approved_plan"]
        args = {"namespace": plan["target"]["namespace"], "target_name": plan["target"]["name"],
                "target_uid": plan["target"]["uid"], "fault_type": plan["fault_type"],
                "intensity": plan["intensity"], "duration_seconds": plan["safety_ttl_seconds"]}
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
            "remaining_risk": "请求级延迟效果未验证", "recovery_trigger": {"condition": "效果条件持续成立后主动恢复"},
        })
print(json.dumps({"type": "turn.completed"}), flush=True)
