FROM node:24.13.0-bookworm-slim AS node_runtime
FROM bladeai-src AS bladeai_source
FROM otel-src AS otel_source
FROM otel-chart AS otel_chart

FROM python:3.12.13-slim-bookworm

ARG TARGETARCH
ARG KUBECTL_VERSION=v1.28.15
ARG HELM_VERSION=v4.1.1
ARG UV_VERSION=0.10.9

ENV DEBIAN_FRONTEND=noninteractive \
    PATH=/app/.venv/bin:/opt/resiliencebenchmark/deepseek-harness/bin:/usr/local/bin:/usr/bin:/bin \
    PYTHONUNBUFFERED=1 \
    STAGE2_REPO_ROOT=/app \
    STAGE2_PRIVATE_ROOT=/var/lib/resbench-stage2/private \
    STAGE2_ARTIFACT_ROOT=/var/lib/resbench-stage2/artifacts \
    STAGE2_SOURCE_ROOT=/opt/resiliencebenchmark/sources \
    STAGE2_OTEL_CHART_FILE=/opt/resiliencebenchmark/charts/opentelemetry-demo-0.40.5.tgz \
    STAGE2_KUBECONFIG=/var/lib/resbench-stage2/private/service.kubeconfig \
    STAGE2_RUNTIME_ENV_FILE=/etc/resbench-stage2/otel-demo.env \
    STAGE2_BLADEAI_PYTHON=/opt/bladeai-venv/bin/python

RUN apt-get -o Acquire::Retries=5 update \
    && apt-get -o Acquire::Retries=5 install -y --no-install-recommends ca-certificates curl git jq build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY --from=node_runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node_runtime /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

RUN test "$TARGETARCH" = "amd64" \
    && curl -fsSLo /tmp/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
    && curl -fsSLo /tmp/kubectl.sha256 "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl.sha256" \
    && echo "$(cat /tmp/kubectl.sha256)  /tmp/kubectl" | sha256sum -c - \
    && install -m 0755 /tmp/kubectl /usr/local/bin/kubectl \
    && curl -fsSLo "/tmp/helm-${HELM_VERSION}-linux-amd64.tar.gz" "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz" \
    && curl -fsSLo /tmp/helm.sha256 "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz.sha256sum" \
    && (cd /tmp && sha256sum -c helm.sha256) \
    && tar -xzf "/tmp/helm-${HELM_VERSION}-linux-amd64.tar.gz" -C /tmp \
    && install -m 0755 /tmp/linux-amd64/helm /usr/local/bin/helm \
    && rm -rf /tmp/kubectl* /tmp/helm* /tmp/linux-amd64

RUN pip install --no-cache-dir "uv==${UV_VERSION}"

WORKDIR /app
COPY . /app
RUN uv sync --frozen --extra runtime --no-dev

COPY --from=bladeai_source / /opt/blade-ai
RUN npm ci --prefix /opt/blade-ai/tui --ignore-scripts --no-audit --no-fund \
    && npm run --prefix /opt/blade-ai/tui build \
    && rm -rf /opt/blade-ai/tui/node_modules \
    && python -m venv /opt/bladeai-venv \
    && /opt/bladeai-venv/bin/pip install --no-cache-dir --editable /opt/blade-ai \
    && /opt/bladeai-venv/bin/python -c "from pathlib import Path; from chaos_agent.chaosblade_installer import download_chaosblade; download_chaosblade(Path('/opt/blade-ai/vendor/chaosblade'), target='linux_amd64')"

RUN npm install --global --no-audit --no-fund \
      @openai/codex@0.139.0 \
      @anthropic-ai/claude-code@2.1.233

RUN install -d -m 0755 /opt/resiliencebenchmark/deepseek-harness
COPY harness/deepseek-harness/runtime-lock/package.json /opt/resiliencebenchmark/deepseek-harness/package.json
COPY harness/deepseek-harness/runtime-lock/package-lock.json /opt/resiliencebenchmark/deepseek-harness/package-lock.json
RUN npm ci --prefix /opt/resiliencebenchmark/deepseek-harness --omit=dev --ignore-scripts --no-audit --no-fund \
    && install -d -m 0755 /opt/resiliencebenchmark/deepseek-harness/bin \
    && printf '%s\n' '#!/bin/sh' 'exec node /opt/resiliencebenchmark/deepseek-harness/node_modules/@deepseek-ai/dsh/lib/bin.js "$@"' > /opt/resiliencebenchmark/deepseek-harness/bin/dsh \
    && chmod 0755 /opt/resiliencebenchmark/deepseek-harness/bin/dsh

COPY --from=otel_source / /opt/resiliencebenchmark/sources/otel-demo-2.2.0
COPY --from=otel_chart /opentelemetry-demo-0.40.5.tgz /opt/resiliencebenchmark/charts/opentelemetry-demo-0.40.5.tgz

RUN install -d -o 10001 -g 10001 -m 0700 \
      /var/lib/resbench-stage2 \
      /var/lib/resbench-stage2/private \
      /var/lib/resbench-stage2/artifacts \
    && install -d -o 10001 -g 10001 -m 0750 /etc/resbench-stage2

ENV HOME=/var/lib/resbench-stage2
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["/app/.venv/bin/python", "-m", "stage2_service"]
