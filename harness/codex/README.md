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

- `__RESBENCH_K8S_MCP_URL__`
- `__RESBENCH_TELEMETRY_MCP_URL__`
- `__RESBENCH_SOURCE_MCP_URL__`
- `__RESBENCH_CHAOS_CONTROL_MCP_URL__`

Do not render the token into the file. Keep:

```toml
bearer_token_env_var = "RESBENCH_MCP_TOKEN"
```

Run qualification should verify the installed Codex version accepts HTTP MCP
servers with `bearer_token_env_var`, and that `codex exec` is launched in
read-only, ephemeral mode for benchmark trials. A typical runner-owned launch
shape is:

```bash
CODEX_HOME="$trial_codex_home" RESBENCH_MCP_TOKEN="$runtime_token" \
  codex exec --sandbox read-only --ephemeral ...
```

The runner owns model/API-key configuration separately. This template only wires
the MCP client surface and must not contain endpoint values, bearer tokens,
kubeconfig contents, source paths, or Oracle data.
