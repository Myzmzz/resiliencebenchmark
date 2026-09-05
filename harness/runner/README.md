# Harness Trial Runner

`scripts/run_harness_trial.py` is the repeatable launcher for Codex, Claude Code,
and DeepSeek Harness trials. BladeAI remains outside this runner because it has a
dedicated adapter and lifecycle.

The runner is dry-run first:

```bash
uv run python scripts/run_harness_trial.py \
  --harness codex \
  --model gpt-5.6
```

Execute mode is explicit:

```bash
uv run python scripts/run_harness_trial.py \
  --harness codex \
  --model gpt-5.6 \
  --execute
```

Runtime input is intentionally narrow. Execute mode requires these
operator-provided variables:

- `RESBENCH_LLM_BASE_URL`
- `RESBENCH_LLM_API_KEY`
- `RESBENCH_K8S_MCP_URL`
- `RESBENCH_TELEMETRY_MCP_URL`
- `RESBENCH_SOURCE_MCP_URL`
- `RESBENCH_CHAOS_CONTROL_MCP_URL`
- `RESBENCH_MCP_TOKEN`

It does not inherit Harbor, SSH, kubeconfig, shell, or host-wide environment
variables. Execute mode requires all seven variables, validates every URL as
plain `http(s)` without userinfo/query/fragment, and requires a 32-character or
longer MCP token with no whitespace.

Formal Trials and interactive debugging must use different launchers. The
formal names are `codex`, `claude`, and `dsh`; they are resolved only through the
runner's fixed `SAFE_PATH`, must be root-owned and non-writable by group/other,
and are launched by a non-root OS user. The runner rejects launchers or argv that
contain global MCP configuration, static-token loading, or permission-bypass
markers. It also verifies that the active Home and MCP configuration are under
the current Trial-private directory and that the child receives exactly the
current Trial MCP token.

Operator-owned interactive launchers such as `claude-manual` and `dsh-manual`
may load persistent credentials or relaxed permission settings, but the formal
runner never resolves those names. A Trial that cannot satisfy this separation
fails before the Agent process starts; it must not be scored as an Agent failure.

The runner maps those values to the real CLI environment expected by each
harness:

- Codex receives `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `RESBENCH_MCP_TOKEN`, and
  `CODEX_HOME`.
- Claude Code receives `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY`,
  `ANTHROPIC_AUTH_TOKEN`, the four
  `RESBENCH_*_MCP_URL` values, `RESBENCH_MCP_TOKEN`, and
  `CLAUDE_CONFIG_DIR`.
- DeepSeek Harness keeps the seven `RESBENCH_*` variables, receives
  `DSH_HOME`, and sets `DSH_TOOLS_MODE=native`.

Stage-2 Campaigns use the `stage2-campaign-request.v2` model matrix. A Harness
selection may contain multiple models; each Harness-model pair receives its own
complete Control and disturbance Trial sequence:

```yaml
model_matrix:
  - harness: codex
    models: [gpt-5.6, gpt-5.6-sol, gpt-5.5]
  - harness: claude-code
    models: [claude-opus-5, deepseek-v4-pro]
  - harness: deepseek-harness
    models: [deepseek-v4-pro, claude-opus-5]
  - harness: bladeai
    models: [gpt-5.6, claude-opus-5]
```

The matrix compiler freezes the model registry SHA-256 and selects only a
protocol declared by both the model and Harness. Codex requires Responses,
Claude Code requires Anthropic Messages, and DeepSeek Harness/BladeAI require
OpenAI Chat Completions. Unknown or incompatible cells block before environment
qualification. The production Stage-2 runtime also requires the alias or
upstream id to appear in the gateway `/models` catalog before any Trial starts.
Trial results record requested alias, upstream model, protocol, registry digest,
provider-reported model when available, and identity verification. A provider
identity mismatch makes the Trial `CASE_INVALID` rather than an Agent failure.

Codex gets a rendered trial-local `CODEX_HOME/config.toml` with MCP URLs but
only the token environment variable name. Claude Code gets a trial-local MCP
JSON copied from the template so `${...}` expansion remains owned by Claude.
DeepSeek Harness is supplied by the evaluation environment. The runner renders
`DSH_HOME/settings.yaml` from the adapter template, including
`agent-default-model`, copies `DSH_HOME/cordis.patch.yml`, and launches the
environment-provided command:

```bash
dsh --profile headless "<public prompt>"
```

The expected headless contract provides final text and exit status, but a
complete machine-readable DSH tool transcript has not yet been qualified on the
target environment. DSH cannot enter scored cross-Harness results until the
session tool trace is exported, redacted, and validated before `DSH_HOME`
deletion. Installation success is not a Benchmark result.

The official headless CLI accepts the task as an argv argument. That means the
prompt can appear in a local process listing while the process is running, so
the runner limits prompt size and only passes the public episode contract. The
prompt must never contain hidden Ground Truth, credentials, or evaluator-only
state.

Artifacts are written under `artifacts/harness/<trial-id>/`:

- `planned.json` records argv, env key names, and isolated home references.
- `events.jsonl` is the redacted append-only event stream.
- `run-trace.json` validates against `harness/schemas/run-trace.schema.json`.
- `agent-result.json` is present only when the harness produced a final JSON
  object that validates against `harness/schemas/agent-result.schema.json`.

The CLI report returns trial-relative `artifactRef`, `runTraceRef`,
`eventsJsonlRef`, and `agentResultRef` values. Recorded argv entries replace
checkout, trial-temp, artifact, and other absolute paths with stable markers, so
sharing a trial bundle does not disclose the host username or checkout path.

Trial-local homes are deleted after execute and dry-run. Artifacts retain only
template hashes and redacted event references; rendered MCP URLs, tokens, CLI
caches, and temporary config files are not kept.

The Agent prompt includes the selected public episode and selected prompt
template only. The runner prepends `harness/prompts/common-task.md` to the
selected lifecycle prompt, and selected prompt references must resolve under
`harness/prompts`. Hidden Ground Truth, injected-defect manifests, oracle
verdicts, and scoring internals must stay outside Agent-visible paths.
