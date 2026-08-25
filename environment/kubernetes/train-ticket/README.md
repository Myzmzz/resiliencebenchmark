# Train-Ticket deployment bundle

`live-export.yaml` is the sanitized 2026-08-23 live snapshot. It contains the
48 Deployments, 3 StatefulSets, Services, PVCs, and application ConfigMaps, but
is retained as evidence rather than applied by the deployment executor.

`static-manifests.yaml` contains only objects that are not owned by the four
Helm releases. `deployment.yaml` routes Nacos, NacosDB, RabbitMQ, and TSDB to
the vendored chart packages and routes all other objects to `kubectl apply`.

No Secret object or Secret value is stored here. `required-secrets.yaml`
records only required names, types, and keys. For a fresh namespace, provision
those Secrets through an approved runtime source before activation. The
`runtime.env.example` variables render the redacted Helm value placeholders.

The chart packages come from the locked Train-Ticket source commit recorded in
`environment/shared/source-locks.yaml`; their package SHA-256 values are part of
`deployment.yaml`.
