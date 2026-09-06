# Stage-2 paired image build assets

`scripts/build_stage2_image.py` builds and pushes the controller overlay and
the isolated agent-runtime image as a pair, then renders the old-cluster
workload templates (`stage2.yaml`, `stage2-integration.yaml`, and
`stage2-matrix-job.yaml`) plus `execution-identities.yaml`. It never applies manifests.

The command requires the upstream ChaosBlade Git worktree that contains
`blade-ai/`. The build script does not use that working tree as a Docker
context. It verifies the fixed `blade-ai-v0.6.2` tag resolves to
`d8c5473ccda329a3841f114f83a43881a2205ab5`, reads the tagged `blade-ai`
subtree with `git archive`, and materializes a clean temporary Buildx
`bladeai-src` context. Dirty or untracked files in the local upstream checkout
are ignored.

```text
python scripts/build_stage2_image.py --bladeai-repo /absolute/path/to/chaosblade-upstream
```

The tagged release name is `blade-ai-v0.6.2`; the Python package version inside
that tag is `0.3.0`. `Dockerfile.agent` validates this real package version and
the upstream `mcp>=1.0,<2.0` dependency before installing BladeAI into its own
venv with a pinned MCP 1.x dependency. The agent-exec daemon venv remains
separate and may use MCP 2.x.

The metadata contains both immutable image references and the rendered output
paths, plus the BladeAI release tag, commit, subtree, package version and MCP
pin used for the agent image. Review those files and perform deployment
separately in the approved old cluster workflow.

## Kubernetes 1.28 compatibility

The templates use ordinary LiteLLM containers, not `initContainers.restartPolicy: Always`.
The matrix Job writes a Controller-owned completion marker after preserving its main
exit code; LiteLLM and agent-runtime observe that marker and exit. The marker directory
is mode 0700 for UID 10001, so evaluated Agent UID 10002 cannot write it. Timeout,
cleanup and Oracle recovery remain independent Controller responsibilities.

## Execution and cleanup identities

The agent daemon mounts host `/sys/fs/cgroup/resbench-agent-exec` at container
`/run/resbench-cgroups`, using a `DirectoryOrCreate` hostPath so kubelet
pre-creates this dedicated subtree. Docker's default `/sys/fs/cgroup` is
read-only, so a nested mount destination there cannot be created by runc.
The old node's cgroup filesystem root is mode `0555`; the deliberately limited
daemon must not gain `DAC_OVERRIDE` or change that global mode merely to create
its prefix. Per-Pod children and resource limits remain daemon-owned. A Unix
socket readiness check runs only against the daemon endpoint, which is opened
after network and cgroup setup; an initial process start is not readiness.

New Pods use `resbench-stage2-controller`; this avoids changing the ServiceAccount
used by old deployments before they are rolled. Apply the base RBAC and
`execution-identities.yaml` before starting a new image. The Controller creates
private kubeconfigs with a rotating `tokenFile` and fixed Kubernetes `as` users:

- `resbench-stage2-executor`: create and read fault CRs; add a UID fence on a target Pod.
- `resbench-stage2-finalizer`: read/delete/patch fault CRs and remove UID fences; no fault creation.

Both credential files remain in Controller-private storage and are absent from
the Agent container. D6-A, failed-create cleanup, explicit cleanup and TTL cleanup
all select the finalizer configuration internally. A tool caller cannot select it.
The logical initiator (`AGENT_MCP` / Controller / timer) remains distinct from the
Kubernetes request identity used to carry out an authorized cleanup.

The Controller is the trusted platform authority. It retains application/Helm
maintenance privileges, including RBAC management needed by the existing reset
workflow. This is not a claim that a malicious Controller cannot change RBAC;
the protected boundary is the evaluated Agent versus Controller credentials and
policy. The two delegated operation identities themselves have separate grants.

Before any real fault, run `scripts/qualify_execution_identities.py` inside the
new Controller using its base, executor and finalizer kubeconfig paths. It checks
the API-server-reported users, resource reads/creates/deletes/patches, Pod fences,
controller liveness reads, namespace limits and permitted delegation via identity
and authorization reviews only. It creates no faults. This check is separate from
canaries and Linux Agent-isolation tests.

The mechanism is supported by the old Kubernetes 1.28 API server and client:
[impersonation authorization](https://github.com/kubernetes/kubernetes/blob/v1.28.0/staging/src/k8s.io/apiserver/pkg/endpoints/filters/impersonation.go),
[kubeconfig identity fields](https://github.com/kubernetes/client-go/blob/v0.28.0/tools/clientcmd/api/v1/types.go).
