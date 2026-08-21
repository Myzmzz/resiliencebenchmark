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

Codex gets a rendered trial-local `CODEX_HOME/config.toml` with MCP URLs but
only the token environment variable name. Claude Code gets a trial-local MCP
JSON copied from the template so `${...}` expansion remains owned by Claude.
DeepSeek Harness uses the verified `@deepseek-ai/dsh@0.1.0-rc.7` headless
single-run contract. The runner renders `DSH_HOME/settings.yaml` from the pinned
template, including `agent-default-model`, copies `DSH_HOME/cordis.patch.yml`,
and launches:

```bash
dsh --profile headless "<public prompt>"
```

The verified headless contract guarantees final text and exit status, but a
complete machine-readable DSH tool transcript has not yet been qualified on the
target host. DSH execute mode is therefore suitable for installation/MCP smoke
only; it cannot enter scored cross-Harness results until the session tool trace
is exported, redacted, and validated before `DSH_HOME` deletion.

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
