# MCP Host Deployment

This directory packages the four BenchmarkFactory MCP servers for a single
Linux host managed by systemd. The services are intentionally loopback-only and
must be exposed to harnesses through an operator-owned tunnel, reverse proxy, or
SSH port forward that injects the per-episode bearer token.

## Service Map

| MCP server | Unit | Loopback URL |
| --- | --- | --- |
| `k8s_ro` | `resbench-mcp-k8s-ro.service` | `http://127.0.0.1:18081/mcp` |
| `telemetry_ro` | `resbench-mcp-telemetry-ro.service` | `http://127.0.0.1:18082/mcp` |
| `source_ro` | `resbench-mcp-source-ro.service` | `http://127.0.0.1:18083/mcp` |
| `chaos_control` | `resbench-mcp-chaos-control.service` | `http://127.0.0.1:18084/mcp` |
| `k8s_ro_sse` | `resbench-mcp-k8s-ro-sse.service` | `http://127.0.0.1:18181/sse` |
| `telemetry_ro_sse` | `resbench-mcp-telemetry-ro-sse.service` | `http://127.0.0.1:18182/sse` |
| `source_ro_sse` | `resbench-mcp-source-ro-sse.service` | `http://127.0.0.1:18183/sse` |

Each logical server runs under a separate identity: `resbench-k8s-ro`,
`resbench-telemetry-ro`, `resbench-source-ro`, or
`resbench-chaos-control`. Its HTTP and optional SSE unit share only that
server's identity and Episode env file. Common unit properties are:

- `WorkingDirectory=/opt/resiliencebenchmark/repo`
- `EnvironmentFile=/etc/resiliencebenchmark/mcp/<service>.env`
- `Restart=on-failure`
- `RESBENCH_MCP_TRANSPORT=streamable-http`
- `RESBENCH_MCP_HTTP_HOST=127.0.0.1`
- `RESBENCH_MCP_HTTP_PATH=/mcp`

The three `*_sse` units are read-only compatibility endpoints for BladeAI
v0.6.2 verifier usage only. They reuse the same per-Episode env files, token,
namespace, application, and telemetry scope as the corresponding Streamable HTTP
read-only service. There is intentionally no `chaos_control` SSE unit.

The read-only services use strict systemd hardening and do not receive writable
runtime paths. `chaos_control` is also hardened, but it is allowed to write only
its active cleanup ledger under `/var/lib/resiliencebenchmark/chaos-control/active`.
The baseline ledger path is read-only to the service. During preparation, the
controller must run as `resbench-chaos-control` when atomically writing baseline ledger files and must write those files with mode `0600`.

## Episode Scope

Do not share one token or environment file across cases. For every Episode,
prepare a separate namespace/application/token contract:

- Kubernetes scope: exactly one Episode namespace in
  `RESBENCH_K8S_RO_NAMESPACE_ALLOWLIST`.
- Telemetry scope: `RESBENCH_TELEMETRY_ALLOWED_NAMESPACES` must be strict. The
  telemetry MCP post-filters responses, but upstream Prometheus, Jaeger, and
  Loki query boundaries still need to be configured by the operator.
- Source scope: `RESBENCH_SOURCE_ALLOWED_APPLICATIONS` should name only the
  current application, with repository subsets added when the Episode narrows
  the code search space.
- Chaos scope: `RESBENCH_CHAOS_NAMESPACE_ALLOWLIST`,
  `RESBENCH_CHAOS_CONTROLLER_TOKEN_REF`, and current controller Pod identity
  must describe one controller and one active experiment domain.

## Chaos Runtime Boundary

`chaos_control` remains disabled until `RESBENCH_CHAOS_EXECUTE_ENABLED=true` is
set in the runtime env file. It enforces a singleton active experiment contract
and requires the service-managed ledger plus its duration watchdog so cleanup is
durable across process restarts. Historical ChaosBlade resources that are not
owned by this controller must be reconciled outside the MCP service.

## Install

Run `install.sh` as root on the target host after the repository has been
materialized as a pinned release and `/opt/resiliencebenchmark/repo` points to
that release:

```bash
environment/mcp/host/install.sh --repo /opt/resiliencebenchmark/repo --head <expected-git-head>
```

The deploy driver materializes the local committed `HEAD` with `git archive`
under `/opt/resiliencebenchmark/releases/<fullsha>`, writes `.resbench-head`,
and switches `/opt/resiliencebenchmark/repo` only when it is absent or already a
managed symlink to that releases root. A pre-existing ordinary directory or unmanaged symlink fails closed. The installer validates Python 3.10+, `uv`,
systemd, the repository path, and `.resbench-head`. It then runs
`uv sync --locked --extra test`. With `--materialize-sources`, it creates
`/opt/resiliencebenchmark/sources` as
`resbench-source-ro:resbench-source-ro` with mode `0750`. If
that directory is empty, it materializes the locked source repositories. If it is
non-empty, it runs `scripts/materialize_sources.py --verify-existing` only and
does not overwrite source checkouts. The redacted manifest is written to
`/var/lib/resiliencebenchmark/source/source-materialization.json`. The deploy driver
passes `--materialize-sources` by default; `--skip-source-materialization` leaves
the Source MCP not ready until the sources are prepared out of band. Finally the
installer installs the unit files and calls `systemctl daemon-reload`.

It does not create real env files, tokens, kubeconfigs, observability endpoints,
or service enablement. Existing `/etc/resiliencebenchmark/mcp/*.env` files are
left untouched. If an env file is missing, the installer places only the
matching `.env.example` next to the expected runtime location.

The installer creates `/etc/resiliencebenchmark/kubeconfigs` as a root-owned
traversable directory, but never copies a cluster credential into it. Provision a
dedicated namespace-scoped read-only kubeconfig for `k8s_ro` and a separately
controlled Chaos kubeconfig there. Use `0640 root:resbench-k8s-ro` for the first
and `0640 root:resbench-chaos-control` for the second. Do not point these units
at an administrator home-directory kubeconfig: the services have
`ProtectHome=yes` and should not inherit cluster-admin access.

## Per-Episode Activation

Use `scripts/activate_mcp_episode.py` on the MCP host to render the four runtime
env files for one public Episode. The script reads only `application.name` and
`application.namespace` from the Episode and reads all runtime values from
environment variables or explicit safe file paths. It validates a single
application, a single namespace, absolute existing kubeconfig paths, bearer token
length, and URL shape before writing anything.
Both kubeconfigs must be regular files below
`/etc/resiliencebenchmark/kubeconfigs`, owned by root and the matching dedicated
service group, group-readable, not group-writable, and inaccessible to other
users.

Dry-run is the default:

```bash
scripts/activate_mcp_episode.py --episode tasks/examples/public/episode.timeout-missing.v0.1.yaml
```

Execute mode requires root and all four dedicated service identities. It atomically
writes `/etc/resiliencebenchmark/mcp/{k8s_ro,telemetry_ro,source_ro,chaos_control}.env`
as `0640 root:<matching-service-group>` using safe quoted values. A read-only
service therefore cannot read the Chaos env or kubeconfig. `chaos_control` is
always rendered with `RESBENCH_CHAOS_EXECUTE_ENABLED=false`; enabling a fault
still requires a later controller-approved action.

The activator does not enable or restart services by default. Pass `--restart`
only when the four env files should be written and all seven MCP units should be
restarted immediately. Any validation, write, or restart failure fails closed and
does not report credential, endpoint, or kubeconfig values.
