# Semantic Scan Remote Runtime

This package runs the defect-scan and Episode-generation stage on the test
cluster worker. Commands are dry-run by default; use `--execute` only when the
remote target, source checkout, kube access, CodeGraph command, and model
runtime variables are ready.

Fixed paths:

- `/data/mj/resbench-system`: remote service state, logs, and repository copy.
- `/data/mj/resbench-runs`: per-run artifacts.

Each run first checks `/data/mj/resbench-system/source-cache` for a snapshot
whose manifest pins revision `2.2.0`, commit
`b74a7bc7bbe66099c61951f42b24dab8b6f02d18`, and the archive SHA256. A valid
cache is unpacked into the run workspace; Git is contacted only when the cache
is absent, and the successful clone is then cached for later runs.

Prepare the bundle from an existing local clone:

```bash
python -m scripts.prepare_semantic_source_cache \
  --source '../benchmark-sources/materialized/otel-demo-2.2.0' \
  --cache-dir /tmp/resbench-source-cache
```

Transfer the resulting archive and manifest to the worker cache directory.
The pipeline refuses partial, identity-mismatched, or SHA256-mismatched cache
entries instead of silently falling back to network input.

Host process installation:

```bash
python scripts/deploy_semantic_scan_remote.py
python scripts/deploy_semantic_scan_remote.py --execute --host 1.94.151.57 --user "$node1user"
```

Control API, available only on the worker loopback by default:

```bash
curl -s http://127.0.0.1:18085/health
curl -s -X POST http://127.0.0.1:18085/runs/start -H 'content-type: application/json' -d '{"execute":false}'
curl -s -X POST http://127.0.0.1:18085/runs/start -H 'content-type: application/json' -d '{"execute":true}'
curl -s http://127.0.0.1:18085/runs/status
curl -s -X POST http://127.0.0.1:18085/runs/cancel
```

Image and Job path:

```bash
# Run on the local Mac. BuildKit emits an Ubuntu-compatible linux/amd64 image
# and pushes it to Harbor; the remote worker never builds the image.
docker buildx build --platform linux/amd64 \
  -f deploy/semantic-scan/Dockerfile \
  -t 1.94.151.57:85/observe/resbench-semantic-scan:<tag> \
  --push .

# Resolve the pushed digest, then deploy/apply on the remote Ubuntu worker.
python scripts/deploy_semantic_scan_remote.py --execute --host 1.94.151.57 \
  --image '1.94.151.57:85/observe/resbench-semantic-scan@sha256:<digest>' \
  --apply-job --node-name tcse-v100-03
```

`--image` should use an immutable registry digest. The deployment command can
pull or run that image, but intentionally has no remote-build option.

The Job clones the pinned source, reads live Kubernetes state through the API
server, and writes artifacts to the hostPath run directory. It does not execute
ChaosBlade faults; candidates without a live-qualified ChaosBlade capability are
retained in the scan artifacts but excluded from Episode generation.

Model variables for the host service belong in
`/data/mj/resbench-system/semantic-scan.env`. The container Job expects an
optional Secret named `resbench-semantic-scan-model` with the same environment
keys, for example `RESILIENCE_AGENT_LLM_BASE_URL`,
`RESILIENCE_AGENT_LLM_API_KEY`, and `RESILIENCE_AGENT_LLM_MODEL`.
