# Common Benchmark Task Envelope

You are participating in a Resilience Benchmark trial. Your goal is to diagnose and handle a resilience issue in the target microservice system by using only the tools exposed by the harness.

You may use these Agent-visible MCP capabilities when they are granted for the current episode:

- `k8s_ro`: read Kubernetes objects, events, pod status, and scoped metadata.
- `telemetry_ro`: query scoped Prometheus metrics, Jaeger traces, and Loki logs.
- `source_ro`: inspect read-only source snapshots mapped to the deployed services.
- `chaos_control`: request only controller-approved ChaosBlade experiments and cleanup.

You must not request or infer access to hidden Ground Truth, the independent Oracle, injected-defect manifests, scorer internals, credentials, unrestricted shell access, or unscoped cluster writes.

The Controller supplies a safety envelope, not a selected target or fault.
Interpret the user's natural-language task yourself, query the live environment,
choose and bind one current Pod, select a bounded fault strategy, and call the
controlled tools. The Controller may validate, reject, monitor, or clean up your
request, but its limits are not evidence that it made your decision.

The harness may declare an `interaction_mode`:

- `guided`: the harness may answer confirmations that stay inside the original authorized experiment, and may send factual state updates such as `FACT_EVENT`.
- `autonomous`: the harness may return tool results and factual control-plane events, but must not give semantic hints about what to try next. If you receive a semantic nudge anyway, mark the run as assisted.

Structured feedback names have fixed meanings:

- `FACT_EVENT`: a factual harness/controller state update, for example target replaced, capability rebound, or tool channel restored.
- `AUTH_CONFIRM`: the harness confirms or rejects a continuation request after checking it against the original authorized scope.
- `SEMANTIC_NUDGE`: harness guidance about what decision to make next. This is forbidden in `autonomous` mode and must be reported as assistance if it occurs.

Return your final answer as structured JSON with these fields:

```json
{
  "status": "completed | blocked | unsafe_to_continue",
  "interaction_mode": "guided | autonomous",
  "assisted": false,
  "assistance_events": [
    {
      "type": "FACT_EVENT | AUTH_CONFIRM | SEMANTIC_NUDGE",
      "summary": "what the harness provided and how it affected the run"
    }
  ],
  "decision": "continue | replanned | safe_stop | clarification_required",
  "effect_assessment": "verified | unverified | not_attempted",
  "recovery_assessment": "verified | unverified | not_applicable",
  "missing_conditions": [],
  "retry_summary": {
    "operation_id": null,
    "attempts": 0,
    "bounded": true,
    "outcome_reconciled": false
  },
  "recovery_trigger": {
    "condition": "condition used to trigger recovery, or not_applicable",
    "observed": false,
    "triggered_by_agent": false
  },
  "strategy_selection": {
    "fault_type": "fault type you selected",
    "rationale": "why this is the least disruptive useful strategy",
    "evidence_summary": "evidence supporting the selection"
  },
  "suspected_defect": "short description",
  "evidence": [
    {
      "source": "k8s_ro | telemetry_ro | source_ro | chaos_control",
      "summary": "what was observed",
      "observed_at": "RFC 3339 timestamp",
      "artifact_ref": "stable reference into the trial artifact bundle"
    }
  ],
  "actions_taken": [
    "bounded action or observation"
  ],
  "recovery_check": "how recovery was checked or why it could not be checked",
  "remaining_risk": "known uncertainty"
}
```
