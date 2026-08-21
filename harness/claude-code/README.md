# Claude Code Harness MCP Template

This template is for Claude Code print-mode benchmark trials. The runner must
create a fresh `CLAUDE_CONFIG_DIR` for every trial and provide MCP endpoint URLs
and the bearer token through environment variables only.

Claude Code `.mcp.json` supports HTTP servers with environment expansion in
string fields, so `mcp.json.template` intentionally keeps these references:

- `${RESBENCH_K8S_MCP_URL}`
- `${RESBENCH_TELEMETRY_MCP_URL}`
- `${RESBENCH_SOURCE_MCP_URL}`
- `${RESBENCH_CHAOS_CONTROL_MCP_URL}`
- `${RESBENCH_MCP_TOKEN}`

The four logical endpoints are `k8s_ro`, `telemetry_ro`, `source_ro`, and
`chaos_control`. Do not write resolved URLs, bearer tokens, kubeconfigs, source
paths, or Oracle data into the template or the committed repository.

Run qualification should verify the installed Claude Code version accepts
`type: "http"` MCP servers, expands environment variables in `.mcp.json`, and
honors strict MCP configuration. A typical runner-owned launch shape is:

```bash
CLAUDE_CONFIG_DIR="$trial_claude_config_dir" RESBENCH_MCP_TOKEN="$runtime_token" \
  claude --print --strict-mcp-config --mcp-config "$trial_mcp_json" ...
```

Use print mode for trials and keep the config directory trial-local. The runner
owns model/API-key configuration separately from this MCP template.
