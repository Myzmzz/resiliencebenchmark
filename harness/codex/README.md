# Codex Harness MCP Template

This template is for trial-local Codex execution. The runner must create a fresh
`CODEX_HOME` for every trial, render `config.toml.template` into that directory,
and pass the MCP bearer token only through `RESBENCH_MCP_TOKEN`.

Codex project or trial configuration uses `[mcp_servers.<name>]`. The four
logical endpoints are `k8s_ro`, `telemetry_ro`, `source_ro`, and
`chaos_control`.

Important boundary: TOML `url` values are plain strings. Codex does not expand
`${ENV}` in those fields, so the runner must replace these placeholders before
launch:

- `__RESBENCH_LLM_BASE_URL__`
- `__RESBENCH_K8S_MCP_URL__`
- `__RESBENCH_TELEMETRY_MCP_URL__`
- `__RESBENCH_SOURCE_MCP_URL__`
- `__RESBENCH_CHAOS_CONTROL_MCP_URL__`

Do not render the token into the file. Keep:

```toml
bearer_token_env_var = "RESBENCH_MCP_TOKEN"
```

The trial-local user config also selects a custom Responses provider whose
`base_url` is rendered from `RESBENCH_LLM_BASE_URL` and whose credential comes
only from `OPENAI_API_KEY`. `supports_websockets = false` keeps compatible
third-party gateways on the qualified SSE transport.
`stream_idle_timeout_ms` is rendered as 180000, giving each Codex provider
stream a three-minute idle deadline. It is separate from the 30-minute
Harness session deadline. Provider or reverse-proxy deadlines upstream of
Codex may still end a request earlier and must be reported separately.

Run qualification should verify the installed Codex version accepts HTTP MCP
servers with `bearer_token_env_var`, and that `codex exec` is launched in
read-only, ephemeral mode for benchmark trials. A typical runner-owned launch
shape is:

```bash
CODEX_HOME="$trial_codex_home" RESBENCH_MCP_TOKEN="$runtime_token" \
  codex exec --sandbox read-only --ephemeral ...
```

The trial template disables shell, workspace-dependency, app, browser,
computer-use, image, goal, hook, plugin-sharing, and multi-agent features. The
read-only sandbox remains a second boundary, but it is not treated as a
substitute for removing non-MCP tools. Qualification must confirm those feature
flags are effective in the exact installed Codex version before its results
enter the comparison matrix.

The rendered config is deleted with the isolated trial home. The repository
template must not contain concrete endpoint values, bearer tokens, kubeconfig
contents, source paths, or Oracle data.
