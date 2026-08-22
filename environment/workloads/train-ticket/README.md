# Train-Ticket Workloads

This directory contains the public, controller-owned workload definitions for
Train-Ticket benchmark preparation.

The profile file intentionally excludes the live target URL, account names,
passwords, station choices, and kubeconfig paths. Those values must be supplied
at runtime through a fixture consumed by `scripts/train_ticket_workload.py`.

Runtime fixture shape:

```yaml
target:
  base_url: http://ts-gateway-service.train-ticket.svc.cluster.local:18888
  base_url_ref: runtime://train-ticket/base-url
  allowed_hosts:
    - ts-gateway-service.train-ticket.svc.cluster.local
credentials:
  kubernetes_secret_ref:
    name: train-ticket-workload-user
    username_key: username
    password_key: password
artifacts:
  pvc_claim: train-ticket-workload-results
cluster:
  allowed_namespaces:
    - train-ticket
scenario:
  from_station: Shang Hai
  to_station: Su Zhou
  travel_date: "2026-08-22"
```

Only secret references are rendered into Kubernetes objects. Literal credential
values are rejected. `target.base_url` must be `http` or `https`, must not
contain userinfo, and its hostname must exactly match `target.allowed_hosts` so
runtime credentials cannot be sent to an unintended host.

The synthetic workload uses `ts-gateway-service:18888` as its API boundary.
`ts-ui-dashboard` remains the browser-facing UI, but routing benchmark traffic
through the dedicated Gateway exposes the same service-discovery path used by
the application while avoiding an extra Nginx proxy layer.

The workload image is now a small Python generator under `image/`. It is based
on the previously verified Train-Ticket login, travel search, preserve, and
order lookup flow, but the public image does not embed private endpoints,
accounts, passwords, kubeconfig paths, or fixed dates. Build it with
`scripts/build_train_ticket_workload.py`; the command is dry-run by default and
only calls `docker buildx` with `--execute`.
The Dockerfile keeps an official digest-pinned default. When the build host
cannot reach Docker Hub, set `TRAIN_TICKET_WORKLOAD_BASE_IMAGE` (or
`--base-image`) to a Harbor-accessible `name@sha256:<linux/amd64 digest>`; the
builder rejects tags or unpinned overrides.

The image must still be built, mirrored or pushed through the approved registry
flow, and pinned as `name:tag@sha256:<digest>` in `spec.generator.image` (or
passed through the controller's `--image` runtime override) before a real
`start --execute` can run. Until that digest is resolved, these files are a
preparation contract and dry-run renderer, not a claim that the workload is
already executable in the cluster.

Real starts write `/results/train-ticket.jtl` to the fixture-provided PVC.
The baseline profile writes `/results/train-ticket-baseline.jtl` and records
the deterministic search/login/order flow selection in the JTL thread name.
`--duration-seconds 60` is reserved for bounded installation smoke; omitting it
uses the declared 600-second profile for formal baseline collection.
`stop --execute` removes only this run's Job and ConfigMap by `run_id`; it never
deletes the result PVC.

The repository provides `results-pvc.yaml` for the current shared-cluster
adapter. It explicitly selects the already-qualified `nfs-client` storage
class instead of relying on the cluster's ambiguous set of default storage
classes. Applying it creates only the controller-owned result volume; workload
credentials remain a separately managed runtime Secret.
