# Sock Shop pinned deployment

Sock Shop is rendered by `scripts/render_sock_shop.py`, not by a remote Kustomize base. The renderer downloads the archived canonical `complete-demo.yaml` from commit `9dff06fae4981921caec6a62393a6ebfce4b3e3f`, verifies SHA-256 `02d70d2c7b576ea8b18fc436e18b9158d5b662e50180998b82127d8050813771`, pins every referenced image by digest, and replaces the removed `beta.kubernetes.io/os` selector with `kubernetes.io/os`.

This is a legacy research baseline, not a production recommendation. In particular, the canonical manifest contains an old RabbitMQ release and previously used unversioned Mongo, Redis Alpine, and RabbitMQ exporter references. Digest pinning makes the chosen behavior reproducible but does not remove their age, compatibility, or security risks.

Render locally:

```bash
python3 scripts/render_sock_shop.py > artifacts/sock-shop/rendered.yaml
```

Before applying:

1. verify the renderer succeeds without SHA-256 or image-pin errors;
2. verify all container images include `@sha256:`;
3. run server-side dry-run with the explicit benchmark kubeconfig;
4. confirm the `sock-shop` namespace is empty or owned by this preparation run;
5. preserve the rendered manifest and image inventory in the private artifact bundle.

After applying, require all Deployments Ready, front-end HTTP readiness, catalogue/cart/order smoke paths, baseline traffic, and metrics/traces/logs before creating any Episode.
