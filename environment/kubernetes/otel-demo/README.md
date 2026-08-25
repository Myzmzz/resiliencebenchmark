# OTel Demo deployment bundle

The live release uses the official OpenTelemetry Helm repository, chart
`opentelemetry-demo` 0.40.5, and application version 2.2.0. `values.yaml` is the
sanitized full value set from successful release revision 16. Runtime password
and API-key fields are placeholders and must be supplied through
`runtime.env.example` variables.

The five former BusyBox `:latest` init-container references use the concrete
`train-ticket/busybox:1.32` image. `supplemental-manifests.yaml` retains the
controller-owned workload result PVC; the active-system marker is managed by
`scripts/deploy_application.py`.

The application-owned `load-generator` runs with one replica. Stage 2 observes
this built-in traffic but does not create, stop, or tune a separate workload.
