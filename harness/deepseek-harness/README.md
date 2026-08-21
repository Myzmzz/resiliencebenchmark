# DeepSeek Harness deployment

This directory prepares the official DeepSeek Harness for isolated headless benchmark trials. It does not store provider credentials or MCP bearer tokens.

Pinned upstream:

- npm package: `@deepseek-ai/dsh@0.1.0-rc.7`
- Git tag: `dsh-v0.1.0-rc.7`
- Git commit: `99f6f02fecdb7dff40c3fbc9470f5907c29f74ca`
- npm integrity: recorded and checked by `install.sh`

The package is a developer preview. Do not replace the exact version with `latest` or `next` during a benchmark campaign.

## Host install

Run `install.sh` as root on the authorized benchmark host. The script creates a dedicated `resbench` system user and installs the package under `/opt/resiliencebenchmark/deepseek-harness`; it does not start a shared Web Host and does not persist provider keys.

Every trial must receive a fresh `DSH_HOME`. MCP and model configuration is rendered into that trial directory from the templates here plus runtime environment references. A single long-lived DSH profile must not be shared across applications or trials.

## Runtime secret inputs

- `RESBENCH_LLM_BASE_URL`
- `RESBENCH_LLM_API_KEY`
- `RESBENCH_K8S_MCP_URL`
- `RESBENCH_PROMETHEUS_MCP_URL`
- `RESBENCH_JAEGER_MCP_URL`
- `RESBENCH_LOKI_MCP_URL`
- `RESBENCH_SOURCE_MCP_URL`
- `RESBENCH_CHAOS_CONTROL_MCP_URL`
- `RESBENCH_MCP_TOKEN`

Secrets must be injected by the process supervisor or one-shot runner. They must not be substituted into a file that is archived with trial artifacts.
