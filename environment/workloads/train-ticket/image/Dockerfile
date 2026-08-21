ARG PYTHON_BASE_IMAGE=python:3.12.5-slim-bookworm@sha256:c24c34b502635f1f7c4e99dc09a2cbd85d480b7dcfd077198c6b5af138906390
FROM ${PYTHON_BASE_IMAGE}

LABEL org.opencontainers.image.title="resiliencebenchmark train-ticket workload"
LABEL org.opencontainers.image.description="Train-Ticket workload generator for benchmark-controlled search, login, and order flows"
LABEL org.opencontainers.image.source="https://github.com/Myzmzz/resiliencebenchmark"

ENV PYTHONUNBUFFERED=1
WORKDIR /opt/resiliencebenchmark/train-ticket

COPY train_ticket_workload_generator.py ./train_ticket_workload_generator.py
COPY run-workload.sh ./run-workload.sh

RUN chmod 0555 ./run-workload.sh \
    && mkdir -p /results \
    && chown 65532:65532 /results \
    && chmod 0775 /results

USER 65532:65532
ENTRYPOINT ["/opt/resiliencebenchmark/train-ticket/run-workload.sh"]
