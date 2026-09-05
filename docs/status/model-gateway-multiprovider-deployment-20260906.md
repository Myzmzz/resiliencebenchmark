# Multi-provider model gateway deployment (2026-09-06)

Deployed commit `e04a1e3` and the routing table in `deploy/stage2/litellm/` to
the two-node cluster, replacing a gateway that served only `gpt-5.6-sol` and
`claude-opus-5`.

## What changed in the cluster

| Object | Change |
| --- | --- |
| ConfigMap `litellm-config` | replaced with the repository routing table (8 aliases) |
| Secret `litellm-upstream` | added `AIGCBEST_API_KEY`, `NEXUSTOKENAI_API_KEY`, `DEEPSEEK_API_KEY`, `DASHSCOPE_API_KEY`, plus `ACUCOMPUTE_API_KEY` and `LITELLM_MASTER_KEY` copied in-cluster from the existing `upstream-api-key` / `master-key`, which were kept |
| Deployment `resbench-stage2-integration` | proxy container gained `envFrom` the Secret; image moved to `stage2-d0-e04a1e3@sha256:adf290e7…` |
| Deployment `litellm` (standalone) | same `envFrom`, so its next restart still resolves the new aliases |

Previous ConfigMap, Secret and Deployment were saved on the control-plane node
under `~/resbench-gateway-backup-20260905/` (mode 0600) before any change.

The master key was not moved through a laptop: the new Secret was built inside
the cluster from the live values. The deployed `upstream-api-key` turned out to
differ from the developer `~/.bashrc` value, which is why it is copied rather
than re-supplied.

## Verification, all from inside the pod

- `GET /v1/models` on the loopback proxy lists all eight aliases.
- `scripts/probe_models.py` from the image, six supported aliases: every one
  reports `supported` with no failing check, so alias resolution, chat
  completions, streaming, single and parallel tool calls, structured JSON and
  the Anthropic Messages bridge all work. Evidence on the PVC at
  `qualification/model-probe-20260906-multiprovider.json`. This also proves the
  cluster can reach the DeepSeek, DashScope and aigcbest endpoints.
- `GET /api/v1/stage2/options` lists the six aliases and reports all four
  Harnesses available on each; an unknown alias is refused with HTTP 422 before
  any trial starts.
- The matrix Job template passes a server-side dry run and carries the gateway
  as a native sidecar, which the batch path previously lacked entirely.

## Not done

The formal matrix axis moved from `gpt-5.6-sol` to `gpt-5.5`, so the eight
sealed D0 qualification campaigns behind `qualification-matrix.json` no longer
match the models they are bound to. A formal run needs those campaigns rerun
for `gpt-5.5` before its results are scorable.
