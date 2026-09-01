# D0 Fault-Execution Qualification

This package runs the current, deliberately narrow qualification line:

> 请针对otel-demo下的accounting服务的一个 pod 注入高 cpu 故障，持续 5 分钟，5 分钟后需要自动恢复

It sends that exact text to BladeAI, Codex, Claude Code, and DeepSeek Harness.
The Agent owns target discovery, injection, effect verification, recovery, and
recovery verification. The Harness owns native-protocol approval, append-only
recording, independent Pod/CPU/ChaosBlade observation, deadline enforcement,
and bounded fallback cleanup.

## Execute on the remote test host

```bash
uv run python scripts/run_otel_accounting_cpu_matrix.py --execute
```

Required runtime inputs are environment-owned and must not be committed:

- `RESBENCH_D0_EXECUTION_HOST_ID=1.94.151.57`
- `RESBENCH_CONTROLLER_KUBECONFIG`
- `RESBENCH_LLM_BASE_URL`, `RESBENCH_LLM_API_KEY`
- `RESBENCH_K8S_MCP_URL`, `RESBENCH_TELEMETRY_MCP_URL`
- `RESBENCH_SOURCE_MCP_URL`, `RESBENCH_CHAOS_CONTROL_MCP_URL`
- `RESBENCH_MCP_TOKEN`

The command has no simulated or local execute mode. It fails closed on a
non-Linux host or when the declared execution-host id differs. For Codex,
Claude Code, and DeepSeek Harness, a Trial-bound `d0_chaos_control` facade keeps
controller secrets outside the fixed Agent prompt while requiring the Agent to
discover and submit the live Pod name and UID itself. BladeAI is exercised
through its native Session/Turn/SSE and internal chaos path; it is bounded by
the independent observer deadline and fallback, so its tool boundary is not
identical to the other three Agents.

## Artifacts

Each Campaign retains Agent responses/events, MCP tool events when the native
Harness exports them, Controller commands, automatic approvals, Oracle samples,
per-Agent results, an HTML report, SVG CPU/timeline/comparison figures,
CSV/JSON summaries, a Markdown audit report, and a SHA-256 manifest.

`FALLBACK_RECOVERED` never becomes Agent PASS. `TIMEOUT_RECOVERED` means the
Agent configured the bounded timeout and the independent Oracle verified timely
recovery without an explicit destroy call. `RECOVERY_UNVERIFIED` is reserved for
insufficient recovery evidence. A native
Harness that exports only a final response is explicitly marked with
`tool_trace_complete=false`.

The first historical four-Agent campaign is documented in
`docs/status/d0-multi-agent-real-fault-qualification-20260901.md`. The final
implementation/compliance audit and selected evidence set are documented in
`docs/status/d0-multi-agent-real-fault-qualification-20260901-final.md`.
