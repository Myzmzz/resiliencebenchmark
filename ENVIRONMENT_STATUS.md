# Environment Preparation Status

> Snapshot date: 2026-08-21
> This is environment-preparation evidence, not a benchmark result.

## Repository contracts

- 30 defect classes are present and schema-validated.
- Public Episode, private Evaluator input, Controller plan, Agent result, run trace, Oracle contract, and Oracle observation schemas are present.
- Controller safety checks and independent Evaluator logic have unit tests.
- Repository validation, secret/path scanning, dry-run, and read-only cluster qualification are available.
- Seven model aliases are registered; transport capability probes are implemented but have not been executed with a rotated runtime gateway credential. Long-context, tool-error recovery, Oracle-refusal, and OpenAI Responses behavior remain mandatory pre-matrix checks.
- DeepSeek Harness is pinned and has an idempotent host installer, but the target host has not authorized the deployment SSH key.
- Prometheus MCP Chart objects are installed with a pinned chart/image and restricted ServiceAccount, but its Deployment is scaled to zero because cluster nodes cannot pull the GHCR image; Harbor mirroring is required before qualification.
- Repository-local MCP implementations now exist for `k8s_ro`, `telemetry_ro`, `source_ro`, and `chaos_control`, with authenticated HTTP runtime templates and unit tests. These are not yet deployed or qualified as remote endpoints on the target host.

## Source evidence

- Train-Ticket public upstream, OTel Demo 2.2.0, canonical Sock Shop deployment, and eight Sock Shop business component repositories are locked by commit and archive SHA-256.
- Local preparation materialization and offline re-verification passed for all locks.
- The Train-Ticket public upstream is not yet proven identical to the currently deployed Harbor images; a rebuild and digest map remain required.
- Sock Shop database and third-party infrastructure images intentionally have no application-source mapping.

## Live application state

### Train-Ticket

- All 48 Deployments are currently Ready.
- `ts-order-service` was restored from 0/2 to 2/2 Ready by a scoped rolling restart.
- The original runtime failure included a full accept queue, thousands of file descriptors, and accumulated `CLOSE_WAIT` connections; recurrence under sustained load is not yet excluded.
- A controller-owned repeatable workload and full business SLO baseline remain pending.
- A controller-owned Train-Ticket workload renderer now exists with fixed profiles, host allowlist validation, Kubernetes Secret references, and PVC-backed result artifacts. The JMeter image still needs to be built, mirrored, and digest-pinned before a real workload run.

### OTel Demo

- The standard load generator is 1/1 Ready.
- Application metrics are visible in shared Prometheus under `otel-demo/*` jobs.
- Recent application traces are visible in shared Jaeger.
- `namespace=otel-demo` log streams are visible in Loki.
- Kubeletstats is incomplete because node serving certificates are expired; node-exporter, cAdvisor, and kube-state-metrics remain available.

### Sock Shop

- Canonical objects render safely and server-side dry-run passes with every image pinned by digest.
- The Deployments and Services were created in the target namespace.
- Cluster nodes cannot currently pull the pinned Docker Hub images; all 14 Deployments are intentionally scaled to zero to stop repeated external pulls.
- A Harbor Robot Account supplied through a runtime secret is required to mirror the 14 digests before scale-up and application qualification.

## Shared observability

- Prometheus, Jaeger, Loki, OTel Collector, Promtail, kube-state-metrics, and both node-exporter instances are Ready.
- Mutable Harbor tags caused different image contents on different nodes for node-exporter and Prometheus. Both workloads now use explicit upstream digest references; runtime versions must be recorded independently of tags.
- Prometheus now uses a protected 20 GiB PVC with 15-day / 15 GiB retention. A one-shot OTLP sentinel remained queryable after a controlled restart.
- Promtail collects `train-ticket`, `sock-shop`, and `otel-demo` pod logs and drops entries outside the Loki retention window.

## Blocking safety state

- 157 pre-existing cluster-scoped ChaosBlade resources are owned by the existing fault platform, not this benchmark. They have no Kubernetes owner references or benchmark run IDs and must not be deleted automatically.
- Formal fault execution remains blocked until their owner confirms the supported recovery path and target-side residue is checked.
- The local `chaos_control` MCP service treats ChaosBlade as cluster-scoped, verifies live target Pod UID before create, requires a baseline ledger token, blocks on unsafe unowned global ChaosBlade state, and deletes only ledger-owned experiments. Because the 157 historical resources are still present, real fault execution remains blocked.
- Previously supplied access credentials were removed from the workspace preparation document and must be rotated before remote deployment or model/Harbor qualification.

## Next completion gates

1. Authorize the dedicated deployment SSH public key and install/qualify DeepSeek Harness on the target host.
2. Supply rotated Harbor Robot credentials at runtime, mirror Sock Shop digests, scale up, and qualify its business paths and signals.
3. Renew kubelet serving certificates using the cluster distribution runbook.
4. Reconcile the historical ChaosBlade resources with their owning platform.
5. Execute the seven-model capability probe and freeze the comparable Harness × Model matrix.
6. Deploy and qualify the real MCP endpoints and then unlock the first 6–10 Episodes.
