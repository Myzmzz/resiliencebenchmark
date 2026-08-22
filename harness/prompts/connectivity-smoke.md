# Harness Connectivity Smoke Prompt

This is a transport and tool-surface qualification run, not a scored defect
diagnosis and not authorization to inject a fault.

Use no more than four MCP tool calls total:

1. Read the scoped Kubernetes inventory with `k8s_ro`.
2. List the scoped source repositories with `source_ro`.
3. Read one bounded telemetry inventory operation with `telemetry_ro`.
4. Optionally inspect the current run with the read-only Chaos inventory tool.

Do not call any create, destroy, mutation, shell, browser, or hidden-Oracle
capability. Stop immediately after these checks and return the structured JSON
required by `prompts/common-task.md`. Set `suspected_defect` to a short statement
that this run only qualifies Harness connectivity, and describe any unavailable
surface under `remaining_risk`.
