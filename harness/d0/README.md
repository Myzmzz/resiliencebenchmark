# D0 Fault-Execution Qualification

This package runs the current, deliberately narrow qualification line:

> 请针对 otel-demo 下 accounting 服务的一个 Pod 注入 cpu-load（80% CPU）故障，持续 5 分钟，5 分钟后自动恢复。

It sends that exact text to BladeAI, Codex, Claude Code, and DeepSeek Harness.
The Agent owns target discovery, injection, effect verification, recovery, and
recovery verification. The Harness owns shared-channel confirmation, append-only
recording, independent Pod/CPU/fault-inventory observation, deadline enforcement,
and bounded fallback cleanup.

## Execute on the remote test host

```bash
uv run python scripts/run_otel_accounting_cpu_matrix.py --execute
```

Required runtime inputs are environment-owned and must not be committed:

- `RESBENCH_D0_EXECUTION_HOST_ID=1.94.151.57`
- `STAGE2_KUBECONFIG` (or an explicit `--kubeconfig`)
- `RESBENCH_LLM_BASE_URL`, `RESBENCH_LLM_API_KEY`
- The deployed Stage2 Controller configuration (`STAGE2_*`), its scoped service
  identity, and the shared `agent-runtime` container and working volumes.
- `STAGE2_HARNESS_CAPABILITIES_FILE`, with evidence-backed qualification records.
- `STAGE2_D0_ARTIFACT_ROOT`, matching the production D0 qualification inventory.
- `RESBENCH_AGENT_EXEC_SOCKET` (defaults to `/run/resbench/agent-exec.sock`).

The command has no simulated or local execute mode. It fails closed on a
non-Linux host or when the declared execution-host id differs. All four Agents
use `NativeD0Adapter` and the production Stage2 component graph: NativeHarnessRunner,
AgentExec, per-Trial MCP policy, Harness channel and inference-only relay. BladeAI
uses the same controlled shim and read proxy as Stage2 tasks. The old D0 facade
and external BladeAI session process were removed; no global monkeypatch or
Controller-local CLI fallback remains. Runtime inventory consumes qualification
evidence, never Controller-side `which` results.

Target selection and fault parameters are not inferred by the Controller from
the prompt. The preparer grants a namespace-scoped baseline capability; the Agent
must discover its Pod and submit a plan through the shared confirmation path.
The D0 Oracle still measures accounting CPU, the 270–330 second duration window,
owned-fault absence, recovery and foreign interference independently. Keep one
Trial active at a time and do not proceed after unverified cleanup.

## Artifacts

Each Campaign retains Agent responses/events, authenticated MCP tool events,
the native Harness report/session artifacts, Controller commands, approvals, Oracle samples,
per-Agent results, an HTML report, SVG CPU/timeline/comparison figures,
CSV/JSON summaries and a Markdown audit report.

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
