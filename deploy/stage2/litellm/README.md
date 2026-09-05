# Stage-2 model gateway (LiteLLM sidecar)

Every Stage-2 pod (`resbench-stage2`, `resbench-stage2-integration`, the
matrix Job) runs LiteLLM as a native sidecar on loopback port 4000. The four
Harnesses only ever see `RESBENCH_LLM_BASE_URL=http://127.0.0.1:4000/v1` plus
the proxy master key; this directory decides which upstream provider serves
each public alias. `scripts/probe_models.py` insists on loopback for plain
`http://` gateways, which is why the proxy is a sidecar rather than a Service.

## Files

| File | Purpose |
| --- | --- |
| `config.yaml` | LiteLLM routing table (`model_list`). Committed; contains no secrets. |
| `providers.env.example` | Names of the credentials the routing table references. |
| `../../../scripts/render_litellm_gateway.py` | Renders ConfigMap `litellm-config` and Secret `litellm-upstream` from a local env file and checks nothing is missing. |
| `../stage2*.yaml` | Pod templates carrying the sidecar (`initContainers[litellm]`, `restartPolicy: Always`). |

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

```bash
cd resiliencebenchmark-stage2-d0-integration
ENV_FILE="../.secrets/llm-providers.env"

# 1. Reuse the master key already deployed (the stage2 runtime Secrets carry
#    the same value as llm-api-key); only rotate both sides together.
kubectl -n resiliencebenchmark-system get secret litellm-upstream \
    -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d
#    ...and paste it into LITELLM_MASTER_KEY inside $ENV_FILE.

# 2. Check that every os.environ/ reference has a value (prints no secrets).
uv run python scripts/render_litellm_gateway.py --env-file "$ENV_FILE" --check

# 3. Render and apply ConfigMap + Secret, then restart the pods that embed the
#    sidecar (the matrix Job picks the new objects up on its next run).
uv run python scripts/render_litellm_gateway.py --env-file "$ENV_FILE" --output-dir /tmp/litellm-render
kubectl apply -f /tmp/litellm-render/
kubectl -n resiliencebenchmark-system rollout restart deploy/resbench-stage2-integration
rm -rf /tmp/litellm-render

# 4. Verify from inside the pod (the litellm image has no curl; use python).
POD=$(kubectl -n resiliencebenchmark-system get pod -l app.kubernetes.io/name=resbench-stage2-integration -o jsonpath='{.items[0].metadata.name}')
kubectl -n resiliencebenchmark-system exec "$POD" -c stage2 -- python - <<'PY'
import json, os, urllib.request
req = urllib.request.Request(os.environ["RESBENCH_LLM_BASE_URL"].rstrip("/") + "/models",
                             headers={"Authorization": "Bearer " + os.environ["RESBENCH_LLM_API_KEY"]})
print(sorted(m["id"] for m in json.load(urllib.request.urlopen(req, timeout=10))["data"]))
PY
kubectl -n resiliencebenchmark-system exec "$POD" -c stage2 -- python /app/scripts/probe_models.py \
    --models-config /app/harness/models.yaml \
    --model gpt-5.5 --model claude-opus-5 --model deepseek-v4-pro-0813 \
    --model deepseek-v4-flash-0731 --model qwen3.8-max --model qwen3.8-flash
```

If the Deployments in the cluster were patched by hand before this directory
existed, re-apply `deploy/stage2/stage2-integration.yaml` (with
`__STAGE2_IMAGE__` / `__SOURCE_HEAD__` substituted) so the sidecar definition
matches the repository.

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

The cluster image can be exercised on a laptop with the same files:

```bash
docker run -d --name litellm-check --platform linux/amd64 -p 127.0.0.1:4017:4000 \
    -v "$PWD/deploy/stage2/litellm:/etc/litellm:ro" --env-file "$ENV_FILE" \
    1.94.151.57:85/observe/aiobs-litellm:v1
RESBENCH_LLM_BASE_URL=http://127.0.0.1:4017/v1 RESBENCH_LLM_API_KEY=<master key> \
    uv run python scripts/probe_models.py --models-config harness/models.yaml --model gpt-5.5
docker rm -f litellm-check
```
