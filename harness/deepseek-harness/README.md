# DeepSeek Harness deployment

This directory prepares the official DeepSeek Harness for isolated headless benchmark trials. It does not store provider credentials or MCP bearer tokens.

Pinned upstream:

- npm package: `@deepseek-ai/dsh@0.1.0-rc.7`
- Git tag: `dsh-v0.1.0-rc.7`
- Git commit: `99f6f02fecdb7dff40c3fbc9470f5907c29f74ca`
- npm integrity: recorded and checked by `install.sh`

The package is a developer preview. Do not replace the exact version with `latest` or `next` during a benchmark campaign.

The published package declares many transitive dependencies with semver ranges,
which otherwise resolve rc.7 modules to newer release candidates. The committed
`runtime-lock/package.json` overrides every observed `@deepseek-ai/dsh-*`
module to `0.1.0-rc.7`; `runtime-lock/package-lock.json` freezes every resolved
package and integrity. The installer verifies the lock SHA-256, installs with
`npm ci`, rejects any non-rc.7 DSH module, and records the actual
`npm ls --all --json` tree for post-install comparison.

The installer copies the qualified Node binary into the dedicated install root
and creates `/opt/resiliencebenchmark/deepseek-harness/bin/dsh` with absolute
Node and package-entry paths. Trial execution as `resbench` therefore does not
depend on root's NVM directory, the host's older system Node, or ambient `PATH`.

The pinned package contract has been inspected directly: `dsh --profile
headless "<task>"` runs one non-interactive session, prints the final assistant
text, and exits nonzero when the run does not complete. Trial preparation writes
the selected gateway model to `$DSH_HOME/settings.yaml` and the four MCP clients
to `$DSH_HOME/cordis.patch.yml`. The benchmark patch disables the base bundle's
shell, filesystem mutation, web, editor, workflow, and subagent tools so the
tested Agent receives only the explicit MCP surface.

## Host install

Run `install.sh` as root on the authorized benchmark host. The script creates a dedicated `resbench` system user and installs the package under `/opt/resiliencebenchmark/deepseek-harness`; it does not start a shared Web Host and does not persist provider keys.

Every trial must receive a fresh `DSH_HOME`. MCP and model configuration is rendered into that trial directory from the templates here plus runtime environment references. A single long-lived DSH profile must not be shared across applications or trials.

## Runtime secret inputs

- `RESBENCH_LLM_BASE_URL`
- `RESBENCH_LLM_API_KEY`
- `RESBENCH_K8S_MCP_URL`
- `RESBENCH_TELEMETRY_MCP_URL`
- `RESBENCH_SOURCE_MCP_URL`
- `RESBENCH_CHAOS_CONTROL_MCP_URL`
- `RESBENCH_MCP_TOKEN`

Secrets must be injected by the process supervisor or one-shot runner. They must not be substituted into a file that is archived with trial artifacts.
