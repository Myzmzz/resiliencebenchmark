# Environment Preparation Status

> Snapshot date: 2026-08-21
> This is environment-preparation evidence, not a benchmark result.

## Repository contracts

- 30 defect classes are present and schema-validated.
- Public Episode, private Evaluator input, Controller plan, Agent result, run trace, Oracle contract, and Oracle observation schemas are present.
- Controller safety checks and independent Evaluator logic have unit tests.
- Repository validation, secret/path scanning, dry-run, and read-only cluster qualification are available.
- Seven model aliases are registered; transport capability probes are implemented but have not been executed with a rotated runtime gateway credential. Long-context, tool-error recovery, Oracle-refusal, and OpenAI Responses behavior remain mandatory pre-matrix checks.
- DeepSeek Harness has an exact top-level package/integrity pin, a complete integrity-bearing npm lock that forces all observed DSH modules to rc.7, an idempotent `npm ci` host installer, and post-install dependency-tree validation. Remote installation and a complete tool-trace export must still be qualified before matrix freeze.
- Prometheus MCP Chart objects are installed with a pinned chart/image and restricted ServiceAccount, but its Deployment is scaled to zero because cluster nodes cannot pull the GHCR image; Harbor mirroring is required before qualification.
- Repository-local MCP implementations now exist for `k8s_ro`, `telemetry_ro`, `source_ro`, and `chaos_control`. Pinned-release host deployment, seven hardened loopback systemd units (four Streamable HTTP plus three read-only BladeAI SSE listeners), per-service Unix identities and credential groups, strict endpoint qualification, and per-Episode scope configuration are repository-prepared. Local authenticated HTTP qualification passed for all four tool surfaces and a BladeAI-compatible Source SSE smoke passed; none of these endpoints is yet deployed or qualified on the target host.
- The pre-incident read-only cluster qualification was intentionally failing on Sock Shop scale-zero and historical ChaosBlade state, plus workload warnings. Its healthy node/observability observations are superseded by the current host recovery incident below.

## Current host recovery incident

- At `2026-08-21T08:50:30Z`, worker `tcse-v100-03` stopped renewing its Node Lease after a recorded `SystemOOM`. The kernel selected an existing `all-in-one-linu` process as the OOM victim; the initial unlocked npm dependency resolution also consumed about 3.4 GiB and increased host pressure.
- SSH and kubelet are currently unreachable, so the node remains `NotReady`. No benchmark fault was injected, and `RESBENCH_CHAOS_EXECUTE_ENABLED` remained false.
- Before the node loss, the MCP host units, 11 verified source snapshots, Episode-scoped RBAC/kubeconfigs, all four authenticated HTTP endpoints, all three read-only SSE listeners, and the locked DeepSeek Harness runtime were installed. Post-incident qualification must be repeated after console-level host recovery.
- The DeepSeek installer now uses a complete npm lock, rejects non-rc.7 DSH packages, requires 4 GiB available memory, caps the Node heap at 2 GiB, and lowers npm CPU/I/O priority.

## Source evidence

- Train-Ticket public upstream, OTel Demo 2.2.0, canonical Sock Shop deployment, and eight Sock Shop business component repositories are locked by commit and archive SHA-256.
- Local preparation materialization and offline re-verification passed for all locks.
- The Train-Ticket public upstream is not yet proven identical to the currently deployed Harbor images; a rebuild and digest map remain required.
- Sock Shop database and third-party infrastructure images intentionally have no application-source mapping.

## Live application state

### Train-Ticket

- All 48 Deployments were Ready before the current worker outage. The latest control-plane snapshot reports 23 Train-Ticket Deployments below desired readiness; this is incident state, not a healthy benchmark baseline.
- `ts-order-service` was restored from 0/2 to 2/2 Ready by a scoped rolling restart.
- The original runtime failure included a full accept queue, thousands of file descriptors, and accumulated `CLOSE_WAIT` connections; recurrence under sustained load is not yet excluded.
- A controller-owned repeatable workload image source and renderer now exist with fixed flow profiles, application-status validation, host allowlisting, Kubernetes Secret references, created-order cleanup, and PVC-backed JTL artifacts. The image still needs to be built, pushed to Harbor, digest-pinned, and exercised against a healthy baseline before the workload is qualified.
- The latest read-only runtime image inventory observed 85 application/init containers; all were Ready or successfully completed and had runtime SHA-256 digests. This does not yet prove the deployed image-to-source mapping.

### OTel Demo

- The standard load generator was 1/1 Ready before the current worker outage.
- The latest control-plane snapshot reports 8 OTel Demo Deployments below desired readiness after the worker outage; the previous healthy statement must not be used as a current baseline until recovery qualification passes.
- Application metrics are visible in shared Prometheus under `otel-demo/*` jobs.
- Recent application traces are visible in shared Jaeger.
- `namespace=otel-demo` log streams are visible in Loki.
- Kubeletstats is incomplete because node serving certificates are expired; node-exporter, cAdvisor, and kube-state-metrics remain available.
- The latest read-only runtime image inventory observed 30 application/init containers; all were Ready or successfully completed and had runtime SHA-256 digests.

### Sock Shop

- Canonical objects render safely and server-side dry-run passes with every image pinned by digest.
- The Deployments and Services were created in the target namespace.
- Cluster nodes cannot currently pull the pinned Docker Hub images; all 14 Deployments are intentionally scaled to zero to stop repeated external pulls.
- A Harbor Robot Account supplied through a runtime secret is required to mirror the 14 digests before scale-up and application qualification.
- The runtime image inventory has zero active Sock Shop containers, so the three-application image qualification remains failed by design.

## Shared observability

- Prometheus, Jaeger, Loki, OTel Collector, Promtail, kube-state-metrics, and both node-exporter instances were Ready before the current worker outage.
- The latest control-plane snapshot reports 4 observability Deployments below desired readiness after the worker outage; the prior ready inventory is historical until requalified.
- Mutable Harbor tags caused different image contents on different nodes for node-exporter and Prometheus. Both workloads now use explicit upstream digest references; runtime versions must be recorded independently of tags.
- Prometheus now uses a protected 20 GiB PVC with 15-day / 15 GiB retention. A one-shot OTLP sentinel remained queryable after a controlled restart.
- Promtail collects `train-ticket`, `sock-shop`, and `otel-demo` pod logs and drops entries outside the Loki retention window.

## Blocking safety state

- 157 pre-existing cluster-scoped ChaosBlade resources are owned by the existing fault platform, not this benchmark. They have no Kubernetes owner references or benchmark run IDs and must not be deleted automatically.
- The live CRD is cluster-scoped `chaosblade.io/v1alpha1`; the running operator reports version 1.8.0. Current CR phases are 104 `Running` and 53 `Error`.
- Formal fault execution remains blocked until their owner confirms the supported recovery path and target-side residue is checked.
- The local `chaos_control` MCP service treats ChaosBlade as cluster-scoped, verifies live target Pod UID before create, requires a one-time baseline capability, blocks on unsafe unowned global ChaosBlade state, and deletes only ledger-owned experiments. A durable deadline ledger and independent watchdog enforce `duration_seconds` and retry failed cleanup. Because the 157 historical resources are still present, real fault execution remains blocked.
- Previously supplied access credentials were removed from the workspace preparation document and must be rotated before remote deployment or model/Harbor qualification.

## Next completion gates

1. Authorize the dedicated deployment SSH public key and install/qualify DeepSeek Harness on the target host.
2. Supply rotated Harbor Robot credentials at runtime, mirror Sock Shop digests, scale up, and qualify its business paths and signals.
3. Renew kubelet serving certificates using the cluster distribution runbook.
4. Reconcile the historical ChaosBlade resources with their owning platform.
5. Execute the seven-model capability probe and freeze the comparable Harness × Model matrix.
6. Activate one Episode-scoped MCP environment on the target host, qualify all four HTTP endpoints plus BladeAI read-only SSE, then execute Codex/Claude Code/DeepSeek Harness smoke trials.
7. Freeze source-commit-to-runtime-image mappings and only then unlock the first 6–10 Episodes.
