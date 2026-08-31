# Local Stage-2 E2E Environment

This directory contains the local-only Kubernetes resources for the Codex
disturbance trial flow on an existing Kubernetes cluster. Mutating commands
require an explicit `--context` or `RESBENCH_E2E_CONTEXT`; inventory stays
read-only by default.

Runtime secrets are read from `~/.bashrc` variables `acuurl` and `acukey` or
from `RESBENCH_LLM_BASE_URL` and `RESBENCH_LLM_API_KEY`. The scripts redact
secret values from all status output and do not persist them.

```bash
./scripts/local_e2e_up.sh
./scripts/local_e2e_check.sh
./scripts/local_e2e_down.sh
```

The current low-cost path reuses the existing `otel-demo`, the cluster-level
ChaosBlade CRD/operator, and the existing `observability` services. It does not
create or delete a cluster or application namespace. `up` is therefore a strict
qualification command: it exits non-zero when another fault is active or the
deployed Stage-2 source identity does not match this branch.

`codex-eval` is isolated under `runs/local-e2e/bin` with a Trial-local
`CODEX_HOME`. Fault execution is through Kubernetes `ChaosBlade` CRs handled by
the existing operator; the host `blade` binary is not part of the Trial
permission boundary.
