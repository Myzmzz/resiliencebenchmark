# DeepSeek Harness evaluation adapter

This directory contains only the Benchmark-side adapter material for isolated
headless DeepSeek Harness trials. Installing, pinning, upgrading, or deploying
the `dsh` executable belongs to environment preparation and is outside this
repository's responsibility.

The environment must expose a qualified `dsh` command on the runner's restricted
`PATH`. The Benchmark creates a fresh `DSH_HOME` for every trial, writes the
selected gateway model to `$DSH_HOME/settings.yaml`, copies the four MCP clients
to `$DSH_HOME/cordis.patch.yml`, and launches:

```bash
dsh --profile headless "<task>"
```

The benchmark patch disables shell, filesystem mutation, web, editor, workflow,
and subagent tools so the evaluated Agent receives only the explicit MCP surface.
A long-lived DSH profile must not be shared across applications or trials.

## Runtime secret inputs

- `RESBENCH_LLM_BASE_URL`
- `RESBENCH_LLM_API_KEY`
- `RESBENCH_K8S_MCP_URL`
- `RESBENCH_TELEMETRY_MCP_URL`
- `RESBENCH_SOURCE_MCP_URL`
- `RESBENCH_CHAOS_CONTROL_MCP_URL`
- `RESBENCH_MCP_TOKEN`

Secrets must be injected by the process supervisor or one-shot runner. They must
not be substituted into a file that is archived with trial artifacts. Runtime
identity and version evidence may be recorded as evaluation provenance, but the
Benchmark does not provision that runtime.
