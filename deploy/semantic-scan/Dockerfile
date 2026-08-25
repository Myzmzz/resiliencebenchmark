FROM 1.94.151.57:85/train-ticket/python-base:3.12.5-slim-bookworm-eac7a234d332@sha256:eac7a234d33269f362593c31d2ff1db7b116fbd794929f1f6015f5ea812ff254

ARG KUBECTL_VERSION=v1.31.4
ARG CRANE_VERSION=v0.20.3
ARG CODEGRAPH_VERSION=0.9.4
ARG CHAOSBLADE_VERSION=1.8.0

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    RESBENCH_CODEGRAPH_COMMAND=/usr/local/bin/codegraph \
    RESBENCH_CHAOSBLADE_COMMAND=/usr/local/bin/blade

WORKDIR /opt/resiliencebenchmark

ADD --checksum=sha256:298e19e9c6c17199011404278f0ff8168a7eca4217edad9097af577023a5620f https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl /tmp/kubectl
ADD --checksum=sha256:36c67a932f489b3f2724b64af90b599a8ef2aa7b004872597373c0ad694dc059 https://github.com/google/go-containerregistry/releases/download/${CRANE_VERSION}/go-containerregistry_Linux_x86_64.tar.gz /tmp/crane.tar.gz
ADD --checksum=sha256:af4dfe25c17868d2260cec243e702de088738ba8bf379962bed8296362eb0c8a https://github.com/colbymchenry/codegraph/releases/download/v${CODEGRAPH_VERSION}/codegraph-linux-x64.tar.gz /tmp/codegraph.tar.gz
ADD --checksum=sha256:a49bb08dfe2a2292c60600ad8e34bf922fe11449317a03c67120838f6e7ee236 https://github.com/chaosblade-io/chaosblade/releases/download/v${CHAOSBLADE_VERSION}/chaosblade-${CHAOSBLADE_VERSION}-linux_amd64.tar.gz /tmp/chaosblade.tar.gz

RUN python - <<'PY'
import os
import pathlib
import tarfile

bin_dir = pathlib.Path("/usr/local/bin")
bin_dir.mkdir(parents=True, exist_ok=True)
pathlib.Path("/tmp/kubectl").replace(bin_dir / "kubectl")
os.chmod(bin_dir / "kubectl", 0o755)
with tarfile.open("/tmp/crane.tar.gz", "r:gz") as archive:
    archive.extract("crane", bin_dir, filter="data")
with tarfile.open("/tmp/codegraph.tar.gz", "r:gz") as archive:
    archive.extractall("/opt", filter="data")
with tarfile.open("/tmp/chaosblade.tar.gz", "r:gz") as archive:
    archive.extractall("/opt", filter="data")
os.chmod(bin_dir / "crane", 0o755)
os.symlink("/opt/codegraph-linux-x64/bin/codegraph", bin_dir / "codegraph")
os.symlink("/opt/chaosblade-1.8.0-linux_amd64/blade", bin_dir / "blade")
PY

RUN codegraph --version | grep -Fx 0.9.4 && blade version

COPY pyproject.toml uv.lock ./
COPY backend backend
COPY controller controller
COPY disturbances disturbances
COPY evaluator evaluator
COPY harness harness
COPY mcp_servers mcp_servers
COPY progression progression
COPY resilience_agent resilience_agent
COPY scoring scoring
COPY scripts scripts
COPY tasks tasks
COPY environment/shared environment/shared

RUN python -m pip install --retries 10 --timeout 120 -e '.[runtime]'

ENTRYPOINT ["python", "scripts/run_semantic_pipeline.py"]
CMD ["--execute", "--run-root", "/data/mj/resbench-runs", "--system-root", "/data/mj/resbench-system", "--repository-url", "https://github.com/open-telemetry/opentelemetry-demo.git", "--revision", "2.2.0", "--expected-commit", "b74a7bc7bbe66099c61951f42b24dab8b6f02d18"]
