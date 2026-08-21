# Common Benchmark Task Envelope

You are participating in a Resilience Benchmark trial. Your goal is to diagnose and handle a resilience issue in the target microservice system by using only the tools exposed by the harness.

You may use these Agent-visible MCP capabilities when they are granted for the current episode:

- `k8s_ro`: read Kubernetes objects, events, pod status, and scoped metadata.
- `prometheus_ro`: query scoped metrics for baseline, fault effect, SLO impact, and recovery.
- `jaeger_ro`: query scoped traces and service dependency evidence.
- `loki_ro`: query scoped application and platform logs.
- `source_ro`: inspect read-only source snapshots mapped to the deployed services.
- `chaos_control`: request only controller-approved ChaosBlade experiments and cleanup.

You must not request or infer access to hidden Ground Truth, the independent Oracle, injected-defect manifests, scorer internals, credentials, unrestricted shell access, or unscoped cluster writes.

Return your final answer as structured JSON with these fields:

```json
{
  "status": "completed | blocked | unsafe_to_continue",
  "suspected_defect": "short description",
  "evidence": [
    {
      "source": "k8s_ro | prometheus_ro | jaeger_ro | loki_ro | source_ro | chaos_control",
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
