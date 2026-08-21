# Environment Preparation Status

> Snapshot date: 2026-08-21
> This is environment-preparation evidence, not a benchmark result.

## Repository contracts

- 30 defect classes are present and schema-validated.
- Public Episode, private Evaluator input, Controller plan, Agent result, run trace, Oracle contract, and Oracle observation schemas are present.
- Controller safety checks and independent Evaluator logic have unit tests.
- Repository validation, secret/path scanning, dry-run, and read-only cluster qualification are available.
- Seven model aliases are registered; transport capability probes are implemented but have not been executed with a rotated runtime gateway credential. Long-context, tool-error recovery, Oracle-refusal, and OpenAI Responses behavior remain mandatory pre-matrix checks.
- DeepSeek Harness has an exact top-level package/integrity pin, a complete integrity-bearing npm lock that forces all observed DSH modules to rc.7, an idempotent `npm ci` host installer, and post-install dependency-tree validation. The remote root and `resbench` identities both report `0.1.0-rc.7`; complete tool-trace export still must be qualified before matrix freeze.
- Prometheus MCP Chart objects are installed with a pinned chart/image and restricted ServiceAccount, but its Deployment is scaled to zero because cluster nodes cannot pull the GHCR image; Harbor mirroring is required before qualification.
- Repository-local MCP implementations now exist for `k8s_ro`, `telemetry_ro`, `source_ro`, and `chaos_control`. Seven hardened loopback systemd units are deployed with per-service Unix identities, Episode-scoped RBAC/kubeconfigs, and 11 verified source locks. All four authenticated HTTP endpoints passed post-recovery qualification. BladeAI v0.6.2 connected all three read-only SSE clients to `verifier`; Chaos control was disabled and unconnected.
- The current read-only cluster state has all three nodes Ready. Remaining intentional hard gates are Sock Shop scale-zero and 157 historical cluster-scoped ChaosBlade resources.

## Recovered host incident

- At `2026-08-21T08:50:30Z`, worker `tcse-v100-03` stopped renewing its Node Lease after a recorded `SystemOOM`. The kernel selected an existing `all-in-one-linu` process as the OOM victim; the initial unlocked npm dependency resolution also consumed about 3.4 GiB and increased host pressure.
- The worker recovered at `2026-08-21T09:06:46Z`; SSH, kubelet, and Node Lease are current, and all three nodes are Ready. No benchmark fault was injected, and `RESBENCH_CHAOS_EXECUTE_ENABLED` remained false.
- Post-recovery qualification passed for the MCP host units, 11 source snapshots, DeepSeek runtime, all four authenticated HTTP endpoints, and all seven loopback listeners.
- The DeepSeek installer now uses a complete npm lock, rejects non-rc.7 DSH packages, requires 4 GiB available memory, caps the Node heap at 2 GiB, and lowers npm CPU/I/O priority.

## Source evidence

- Train-Ticket public upstream, OTel Demo 2.2.0, canonical Sock Shop deployment, and eight Sock Shop business component repositories are locked by commit and archive SHA-256.
- Local preparation materialization and offline re-verification passed for all locks.
- The Train-Ticket public upstream is not yet proven identical to the currently deployed Harbor images; a rebuild and digest map remain required.
- Sock Shop database and third-party infrastructure images intentionally have no application-source mapping.

## Live application state

### Train-Ticket

- All 48 Train-Ticket Deployments are Ready after worker recovery.
- `ts-order-service` was restored from 0/2 to 2/2 Ready by a scoped rolling restart.
- The original runtime failure included a full accept queue, thousands of file descriptors, and accumulated `CLOSE_WAIT` connections; recurrence under sustained load is not yet excluded.
- A controller-owned repeatable workload image source and renderer now exist with fixed flow profiles, application-status validation, host allowlisting, Kubernetes Secret references, created-order cleanup, and PVC-backed JTL artifacts. The image still needs to be built, pushed to Harbor, digest-pinned, and exercised against a healthy baseline before the workload is qualified.
- The latest read-only runtime image inventory observed 85 application/init containers; all were Ready or successfully completed and had runtime SHA-256 digests. This does not yet prove the deployed image-to-source mapping.

### OTel Demo

- All 23 OTel Demo Deployments are Ready after worker recovery; the standard load generator is 1/1 Ready.
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

- Prometheus, Jaeger, Loki, OTel Collector, Promtail, kube-state-metrics, and both node-exporter instances are Ready after worker recovery.
- Four private Harbor images had a non-executable `/wrapper.sh`; the live Deployments now bypass it with verified native binary paths and use `repository:tag@digest` pins. Prometheus, Jaeger, and Loki readiness endpoints return HTTP 200.
- `metrics-server` is temporarily pinned to the recovered worker because another worker could not pull its image. The aggregated API is Available again; Harbor digest mirroring and removal of this node pin remain required.
- Mutable Harbor tags caused different image contents on different nodes for node-exporter and Prometheus. Both workloads now use explicit upstream digest references; runtime versions must be recorded independently of tags.
- Prometheus now uses a protected 20 GiB PVC with 15-day / 15 GiB retention. A one-shot OTLP sentinel remained queryable after a controlled restart.
- Promtail collects `train-ticket`, `sock-shop`, and `otel-demo` pod logs and drops entries outside the Loki retention window.

## Scoped cleanup

- Deleted namespaces: `ai-obs`, `aiops`, `apitest`, `bliyun-ecs`, `chaos-mesh`, `fault-test`, and `xlang-beyla`.
- Deleted 40 `default` namespace objects with `mocker-`, `model-`, or `ts-` names; 19 associated PVs are gone. Declared PVC requests totaled 109.72 GiB.
- Deleted 13 Chaos Mesh Workflows, 13 WorkflowNodes, 44 Chaos Mesh CRDs, their Helm releases, webhooks, and orphan cluster RBAC.
- Three residual default/model NFS directories (about 198 MiB of data) were archived with SHA-256 verification before deletion; all target NFS paths are absent.
- `ischaos`, `observability`, and all 157 historical ChaosBlade resources were intentionally preserved. The cleanup backup is tracked by `runtime-backup://cleanup-20260821T114446Z`; Kubernetes manifests are recoverable, while PVC data not included in the NFS archive is not.

## Blocking safety state

- 157 pre-existing cluster-scoped ChaosBlade resources are owned by the existing fault platform, not this benchmark. They have no Kubernetes owner references or benchmark run IDs and must not be deleted automatically.
- The live CRD is cluster-scoped `chaosblade.io/v1alpha1`; the running operator reports version 1.8.0. Current CR phases are 104 `Running` and 53 `Error`.
- Formal fault execution remains blocked until their owner confirms the supported recovery path and target-side residue is checked.
- The local `chaos_control` MCP service treats ChaosBlade as cluster-scoped, verifies live target Pod UID before create, requires a one-time baseline capability, blocks on unsafe unowned global ChaosBlade state, and deletes only ledger-owned experiments. A durable deadline ledger and independent watchdog enforce `duration_seconds` and retry failed cleanup. Because the 157 historical resources are still present, real fault execution remains blocked.
- Previously supplied access credentials were removed from the workspace preparation document and must be rotated before remote deployment or model/Harbor qualification.

## Next completion gates

1. Supply rotated Harbor Robot credentials at runtime, mirror Sock Shop digests, scale up, and qualify its business paths and signals.
2. Renew kubelet serving certificates using the cluster distribution runbook.
3. Reconcile the historical ChaosBlade resources with their owning platform.
4. Supply a rotated model-gateway credential, execute the seven-model capability probe, and run Codex/Claude Code/DeepSeek smoke trials.
5. Qualify complete BladeAI and DeepSeek trajectory export for scored trials.
6. Freeze source-commit-to-runtime-image mappings and only then unlock the first 6–10 Episodes.
