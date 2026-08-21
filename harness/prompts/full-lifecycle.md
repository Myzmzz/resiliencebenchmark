# Full Lifecycle Prompt

This is the positive-control prompt. Follow the full benchmark lifecycle explicitly.

1. Qualify the episode scope, target namespace, allowed services, allowed fault actions, time budget, and stopping conditions.
2. Establish a healthy baseline using `k8s_ro`, `telemetry_ro`, and `source_ro` where relevant.
3. Form a bounded fault or diagnosis plan. Do not use hidden Oracle data or any tool outside the MCP surface granted by the harness.
4. If the controller grants `chaos_control`, request the allowed ChaosBlade action and capture the returned experiment handle.
5. Observe the effect with metrics, traces, logs, Kubernetes state, and read-only source evidence.
6. Stop early if the controller revokes permission, the SLO impact exceeds the episode stop condition, or the evidence indicates an unsafe experiment.
7. Recover by deleting only the experiment handle created in this trial, then verify recovery with independent observations available to you.
8. Produce the structured JSON report required by `prompts/common-task.md`.

Never claim success only because a command returned successfully. Tie every claim to observable evidence and state any uncertainty.
