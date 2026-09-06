# Stage-2 model gateway (LiteLLM sidecar)

Every Stage-2 pod (`resbench-stage2`, `resbench-stage2-integration`, the
matrix Job) declares LiteLLM as an ordinary container on loopback port 4000,
supported by the old cluster's Kubernetes 1.28. The trusted Controller uses
`RESBENCH_LLM_BASE_URL=http://127.0.0.1:4000/v1` and the proxy master key.
Evaluated Harnesses instead receive a per-Trial inference-only relay address
and token; they cannot access gateway administration or its master key.
This directory decides which upstream provider serves each public alias.
The fixed gateway image is the official LiteLLM 1.92.0 release mirrored as
`1.94.151.57:85/observe/resbench-litellm:1.92.0`. Templates pin its published
image and use the actual `/app/.venv/bin/litellm` CLI with `--host 127.0.0.1`.
Health probes execute inside the container because Pod-IP HTTP probes cannot
reach a loopback-only listener. `python -m litellm` is not a valid entrypoint
in this release. The former `aiobs-litellm:v1` image lacked Prisma and raised
an internal error while processing anonymous authentication failures.
`scripts/probe_models.py` insists on loopback for plain
`http://` gateways, which is why the proxy is a sidecar rather than a Service.

## Files

| File | Purpose |
| --- | --- |
| `config.yaml` | LiteLLM routing table (`model_list`). Committed; contains no secrets. |
| `providers.env.example` | Names of the credentials the routing table references. |
| `../../../scripts/render_litellm_gateway.py` | Renders `litellm-config`, provider-only `litellm-upstream`, and Controller-only `resbench-stage2-gateway-client` from a protected local env file. |
| `../stage2*.yaml` | Pod templates carrying `containers[litellm]`; matrix completion uses the Controller-owned completion marker. |

The real credentials live outside git, for example
`<project root>/.secrets/llm-providers.env` (mode `0600`).

## Aliases

| Public alias | Upstream | Upstream model id | Used by |
| --- | --- | --- | --- |
| `gpt-5.5` | aigcbest new-api relay (`https://api2.aigcbest.top/v1`) | `gpt-5.5` | Harness default (Codex, BladeAI, DeepSeek Harness) |
| `claude-opus-5` | Acucompute console, Anthropic protocol | `claude-opus-5` | Claude Code native model |
| `deepseek-v4-pro-0813` | DeepSeek official (`https://api.deepseek.com/v1`) | `deepseek-v4-pro` | supported model |
| `deepseek-v4-flash-0731` | DeepSeek official | `deepseek-v4-flash` | supported model |
| `qwen3.8-max` | DashScope compatible mode | `qwen3.8-max` | supported model |
| `qwen3.8-flash` | DashScope compatible mode | `qwen3.8-flash` | supported model |
| `gpt-5.6-sol` | Acucompute console | `gpt-5.6-sol` | legacy alias for earlier qualification refs |
| `gpt-5.5-nexustokenai` | nexustokenai relay (`https://api.nexustokenai.com/v1`) | `gpt-5.5` | explicit alternate route, never an automatic fallback |

Why aigcbest carries the default: in a side-by-side sample on 2026-09-05 the
nexustokenai relay prefixed every chat-completion answer with an invisible
U+200B zero-width space (strict JSON parsing fails, `probe_models.py` reports
`structured_json_output` failed), produced one Cloudflare 524 timeout, and
rejects python-urllib clients (Cloudflare 1010). Its Responses API output is
clean, so the alternate alias remains usable for Codex-only runs. aigcbest
answered 48/48 sampled requests cleanly. DeepSeek does not accept the
`json_schema` response format (HTTP 400); `json_object` works and the probe
accepts either.

DeepSeek only publishes undated ids; the dated aliases name the V4 Flash
(2026-07-31) and V4 Pro (2026-08-13) releases those ids currently serve.
Aliases are lowercase because Stage-2 request ids embed them.

`stage2_service.contracts.STAGE2_SUPPORTED_MODELS` lists the aliases the
service accepts; `STAGE2_MODEL_MATRIX` (`gpt-5.5`, `claude-opus-5`) is the
formal matrix axis. `harness/models.yaml` describes the same aliases for the
probe and the trial runner. `tests/test_render_litellm_gateway.py` fails when
these three places disagree.

## Deploy or update the gateway

Current deployment scope is **only the old cluster**, using
`/Users/mymz/.kube/coroot-config`, context `kubernetes-admin@kubernetes`.
Do not deploy or test on the new cluster. The manifests are desired state,
not evidence that the running deployments already have these containers.

Inventory active tasks and archive existing workload specifications before
rollout. Preserve each workload's data paths and node placement. Do not apply
the complete Deployment templates over the old workloads; patch the reviewed
container, identity, volume and security changes as one coherent update.
Deploying only the new Controller image does not create the Agent boundary.

The old e2e workload uses a 5Gi emptyDir for its data, unlike the main service's
PVC. Back up and verify its `artifacts` before replacing the Pod, then restore
only those records to the same path. Do not copy old private credentials or
claim historical records qualify the new runtime. Pin each rollout to its
currently verified node where the AppArmor profile is installed.

All Controller templates now reference `resbench-stage2-gateway-client` for
the gateway URL/key, while `litellm-upstream` is accessible only to the gateway.
The application runtime Secret retains its existing configuration. This avoids
changing legacy consumers or accidentally retaining their upstream model route.

Keep provider credentials and rendered Secrets in a private directory outside
Git. Never print decoded Secrets or place keys in command arguments. Reuse the
existing gateway master key when appropriate, or update the gateway and its
Controller clients together. The following prepares the gateway objects only;
workload rollout and runtime Secret updates remain a separate reviewed step.

```bash
cd resiliencebenchmark-stage2-d0-integration
GATEWAY_ENV_FILE="../.secrets/llm-providers.env"
umask 077
GATEWAY_RENDER_DIR=$(mktemp -d /tmp/resbench-litellm.XXXXXX)
uv run python scripts/render_litellm_gateway.py \
    --env-file "$GATEWAY_ENV_FILE" --output-dir "$GATEWAY_RENDER_DIR"
kubectl --kubeconfig /Users/mymz/.kube/coroot-config \
    --context kubernetes-admin@kubernetes apply -f "$GATEWAY_RENDER_DIR"
```

After the reviewed rollout, resolve the exact new integration Pod and run:

```bash
kubectl --kubeconfig /Users/mymz/.kube/coroot-config \
    --context kubernetes-admin@kubernetes -n resiliencebenchmark-system \
    exec <verified-new-integration-pod> -c stage2 -- /app/.venv/bin/python /app/scripts/probe_models.py \
    --models-config /app/harness/models.yaml \
    --model gpt-5.5 --model claude-opus-5 --model deepseek-v4-pro-0813 \
    --model deepseek-v4-flash-0731 --model qwen3.8-max --model qwen3.8-flash
```

Do not delete the private rendered Secret directory using a broad variable or
wildcard. After successful verification, resolve and remove only that exact
temporary directory under the usual credential-handling procedure.

Earlier deployment notes referred to a different environment with node
`vm-0-10-ubuntu`; they must not be used as the old-cluster inventory. The old
cluster has `tcse-v100-01/02/03`. Resolve actual Secret references, mounted
ConfigMaps, node placement and active tasks from that cluster before changes.

## Adding a model

1. Add a `model_list` entry to `config.yaml`; a new provider also needs a new
   `os.environ/<NAME>` reference and a line in `providers.env.example`.
2. Register the alias in `harness/models.yaml` with its protocol candidates.
3. Add the alias to `STAGE2_SUPPORTED_MODELS` in `stage2_service/contracts.py`
   (and to `STAGE2_MODEL_MATRIX` only if it joins the formal matrix).
4. Run `uv run pytest tests/test_render_litellm_gateway.py tests/test_probe_models.py`,
   re-render the gateway, rebuild the Stage-2 image
   (`scripts/build_stage2_image.py`) so the service accepts the alias, and run
   `probe_models.py` inside the pod before qualifying trials.

## Local validation

`gateway_audit.logger_instance` is a proxy ingress callback. LiteLLM loads
`gateway_audit.py` from the **same directory as config.yaml**, not from an
arbitrary Python module path. The renderer includes both files in one ConfigMap;
Controller and gateway mount the same config file using read-only `subPath`
mounts. Restart the reviewed workloads after a configuration update so their
snapshots and the running router move together.

The callback writes only Controller-owned request metadata to the private
`gateway-audit` emptyDir shared with the Controller, never with the Agent.
`received` means the request arrived at the proxy, **not** that the model or
experiment succeeded. NativeRunner validates all expected request IDs and
persists `gateway-requests.json`; D0 import revalidates this durable artifact.
No prompt, response body, token or cookie belongs in this receipt. Missing or
inconsistent receipts cannot qualify a trial. Model availability is checked
separately per alias; one unavailable alias does not disable healthy aliases.

Run the real proxy image against an in-container fake provider without network
access or real credentials. This checks four request forms with four Harness
identity labels (16 requests), not actual Harness execution or live models:

```bash
docker run --rm --network none --platform linux/amd64 \
    --user 10001:10001 --read-only --tmpfs /tmp:rw,nosuid,size=256m \
    --mount "type=bind,source=$PWD/stage2_service/gateway_audit_callback.py,target=/probe-mod/gateway_audit.py,readonly" \
    --mount "type=bind,source=$PWD/tests/integration/gateway_proxy_probe.py,target=/probe.py,readonly" \
    --entrypoint /app/.venv/bin/python \
    1.94.151.57:85/observe/resbench-litellm:1.92.0@sha256:237ed94c2b4bd821d44f4abd4b57b2ae7b3108a7b4a768d1d8cd7c1b3884c604 /probe.py
```

The probe also checks anonymous requests return 401 and never reach the fake
provider. With no virtual-key database, this pinned release rejects a non-master
key with 400 (`No connected db.`); that rejection is distinct from anonymous 401.
Neither case may return 500. The 16 authorized protocol/audit requests remain
separate from these negative authentication checks.
