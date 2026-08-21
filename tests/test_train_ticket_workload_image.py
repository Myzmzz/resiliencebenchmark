from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
import yaml

from scripts import build_train_ticket_workload


IMAGE_DIR = Path("environment/workloads/train-ticket/image")
GENERATOR = IMAGE_DIR / "train_ticket_workload_generator.py"
DOCKERFILE = IMAGE_DIR / "Dockerfile"
PROFILE = Path("environment/workloads/train-ticket/profiles.yaml")


def load_generator():
    spec = importlib.util.spec_from_file_location("train_ticket_workload_generator", GENERATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TrainTicketHandler(BaseHTTPRequestHandler):
    seen: list[tuple[str, str, dict[str, Any] | None]] = []
    fail_preserve_status = False
    fail_cancel_status = False
    omit_order_id = False
    large_order_response = False

    def log_message(self, format: str, *args):  # noqa: A002
        return

    def _body(self) -> dict[str, Any] | None:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return None
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _send(self, payload: dict[str, Any], status: int = 200) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self) -> None:
        body = self._body()
        self.seen.append(("POST", self.path, body))
        if self.path == "/api/v1/users/login":
            self._send({"status": 1, "data": {"token": "token-for-test", "userId": "account-for-test"}})
        elif self.path == "/api/v1/travelservice/trips/left":
            self._send(
                {
                    "status": 1,
                    "data": [
                        {
                            "tripId": {"type": "G", "number": "1234"},
                            "startStation": body["startPlace"],
                            "terminalStation": body["endPlace"],
                        }
                    ]
                }
            )
        elif self.path == "/api/v1/preserveservice/preserve":
            self._send({"status": 0 if self.fail_preserve_status else 1, "msg": "ok"})
        elif self.path == "/api/v1/orderOtherService/orderOther/refresh":
            if self.large_order_response:
                self._send({"status": 1, "data": "x" * (2 * 1024 * 1024 + 8)})
                return
            data = [{}] if self.omit_order_id else [{"id": "older-order-for-test"}, {"id": "newest-order-for-test"}]
            self._send({"status": 1, "data": data})
        else:
            self._send({"error": "not found"}, status=404)

    def do_GET(self) -> None:
        self.seen.append(("GET", self.path, None))
        if self.path == "/api/v1/contactservice/contacts/account/account-for-test":
            self._send({"status": 1, "data": [{"id": "contact-for-test"}]})
        elif self.path == "/api/v1/cancelservice/cancel/newest-order-for-test/account-for-test":
            self._send({"status": 0 if self.fail_cancel_status else 1, "msg": "cancelled"})
        else:
            self._send({"error": "not found"}, status=404)


@pytest.fixture
def train_ticket_server():
    TrainTicketHandler.seen = []
    TrainTicketHandler.fail_preserve_status = False
    TrainTicketHandler.fail_cancel_status = False
    TrainTicketHandler.omit_order_id = False
    TrainTicketHandler.large_order_response = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), TrainTicketHandler)
    try:
        import threading

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()


def workload_config(profile_id: str = "order") -> dict[str, Any]:
    return {
        "profileId": profile_id,
        "targetFlowQps": 50.0,
        "concurrency": 1,
        "durationSeconds": 0.03,
        "abortThresholds": {
            "maxErrorRate": 0.5,
            "maxP95LatencyMs": 5000,
            "maxConsecutiveFailures": 5,
        },
        "scenario": {
            "from_station": "Shang Hai",
            "to_station": "Su Zhou",
            "travel_date": "2026-08-22",
        },
        "steps": [],
        "cleanupCreatedOrders": True,
    }


def allowed_host(base_url: str) -> str:
    parsed = urlparse(base_url)
    assert parsed.hostname
    return parsed.hostname


def test_generator_runs_order_flow_and_writes_jmeter_compatible_jtl(tmp_path, train_ticket_server):
    generator = load_generator()
    result_path = tmp_path / "train-ticket.jtl"

    code = generator.run_workload(
        workload_config("order"),
        train_ticket_server,
        "runtime-user",
        "runtime-password",
        result_path,
        allowed_host(train_ticket_server),
    )

    rows = result_path.read_text(encoding="utf-8").splitlines()
    assert code == 0
    assert rows[0].startswith("timeStamp,elapsed,label,responseCode")
    assert "login" in rows[1]
    assert any("preserve-order" in row for row in rows)
    assert any("query-order" in row for row in rows)
    assert any("cleanup-order" in row for row in rows)
    assert ("POST", "/api/v1/users/login", {"username": "runtime-user", "password": "runtime-password"}) in TrainTicketHandler.seen
    assert any(path == "/api/v1/travelservice/trips/left" for _, path, _ in TrainTicketHandler.seen)
    preserve_body = next(body for method, path, body in TrainTicketHandler.seen if method == "POST" and path == "/api/v1/preserveservice/preserve")
    assert preserve_body["foodType"] == 0
    assert ("GET", "/api/v1/cancelservice/cancel/newest-order-for-test/account-for-test", None) in TrainTicketHandler.seen


def test_generator_abort_threshold_returns_nonzero(tmp_path):
    generator = load_generator()
    result_path = tmp_path / "train-ticket.jtl"
    config = workload_config("search")
    config["abortThresholds"]["maxConsecutiveFailures"] = 1

    code = generator.run_workload(
        config,
        "http://127.0.0.1:1",
        "runtime-user",
        "runtime-password",
        result_path,
        "127.0.0.1",
    )

    assert code == 3
    assert result_path.read_text(encoding="utf-8").splitlines()[0] == ",".join(generator.JTL_FIELDS)


def test_generator_marks_train_ticket_application_status_failures(tmp_path, train_ticket_server):
    generator = load_generator()
    result_path = tmp_path / "train-ticket.jtl"
    TrainTicketHandler.fail_preserve_status = True
    config = workload_config("order")
    config["abortThresholds"]["maxConsecutiveFailures"] = 1

    code = generator.run_workload(config, train_ticket_server, "runtime-user", "runtime-password", result_path, allowed_host(train_ticket_server))

    rows = result_path.read_text(encoding="utf-8").splitlines()
    assert code == 3
    assert any("preserve application status indicates failure" in row for row in rows)
    assert not any("cleanup-order" in row for row in rows)


def test_generator_requires_order_id_before_cleanup(tmp_path, train_ticket_server):
    generator = load_generator()
    result_path = tmp_path / "train-ticket.jtl"
    TrainTicketHandler.omit_order_id = True
    config = workload_config("order")
    config["abortThresholds"]["maxConsecutiveFailures"] = 1

    code = generator.run_workload(config, train_ticket_server, "runtime-user", "runtime-password", result_path, allowed_host(train_ticket_server))

    rows = result_path.read_text(encoding="utf-8").splitlines()
    assert code == 3
    assert any("missing order id" in row for row in rows)
    assert not any("cleanup-order" in row for row in rows)


def test_generator_marks_cancel_application_status_failure(tmp_path, train_ticket_server):
    generator = load_generator()
    result_path = tmp_path / "train-ticket.jtl"
    TrainTicketHandler.fail_cancel_status = True
    config = workload_config("order")
    config["abortThresholds"]["maxConsecutiveFailures"] = 1

    code = generator.run_workload(config, train_ticket_server, "runtime-user", "runtime-password", result_path, allowed_host(train_ticket_server))

    rows = result_path.read_text(encoding="utf-8").splitlines()
    assert code == 3
    assert any("cleanup-order" in row and "cleanup-order application status indicates failure" in row for row in rows)


def test_generator_caps_response_body_at_two_mib(tmp_path, train_ticket_server):
    generator = load_generator()
    result_path = tmp_path / "train-ticket.jtl"
    TrainTicketHandler.large_order_response = True
    config = workload_config("order")
    config["abortThresholds"]["maxConsecutiveFailures"] = 1

    code = generator.run_workload(config, train_ticket_server, "runtime-user", "runtime-password", result_path, allowed_host(train_ticket_server))

    rows = result_path.read_text(encoding="utf-8").splitlines()
    assert code == 3
    assert any("response body exceeded 2097152 bytes" in row for row in rows)


def test_generator_rejects_runtime_host_mismatch(monkeypatch, tmp_path, train_ticket_server):
    generator = load_generator()
    monkeypatch.setenv("TRAIN_TICKET_ALLOWED_HOSTS", "ts-ui-dashboard.train-ticket.svc.cluster.local")

    with pytest.raises(generator.WorkloadRuntimeError, match="hostname is not in TRAIN_TICKET_ALLOWED_HOSTS"):
        generator.run_workload(
            workload_config("search"),
            train_ticket_server,
            "runtime-user",
            "runtime-password",
            tmp_path / "out.jtl",
            "ts-ui-dashboard.train-ticket.svc.cluster.local",
        )


def test_generator_creates_and_flushes_jtl_header_before_first_flow(monkeypatch, tmp_path, train_ticket_server):
    generator = load_generator()
    result_path = tmp_path / "train-ticket.jtl"

    def fake_run_flow(profile_id, client, config, username, password, thread_name):
        assert result_path.read_text(encoding="utf-8").splitlines()[0] == ",".join(generator.JTL_FIELDS)
        return [
            generator.Sample(
                timestamp_ms=1,
                elapsed_ms=1,
                label="synthetic",
                response_code="200",
                response_message="OK",
                thread_name=thread_name,
                success=True,
                failure_message="",
                response_bytes=2,
                sent_bytes=2,
                url=train_ticket_server,
                latency_ms=1,
                connect_ms=1,
            )
        ]

    monkeypatch.setattr(generator, "run_flow", fake_run_flow)
    code = generator.run_workload(
        workload_config("search"),
        train_ticket_server,
        "runtime-user",
        "runtime-password",
        result_path,
        allowed_host(train_ticket_server),
    )

    assert code == 0
    assert "synthetic" in result_path.read_text(encoding="utf-8")


def test_main_requires_controller_rendered_allowed_hosts(monkeypatch, tmp_path, train_ticket_server):
    generator = load_generator()
    config_path = tmp_path / "workload.json"
    config_path.write_text(json.dumps(workload_config("search")), encoding="utf-8")
    monkeypatch.setenv("WORKLOAD_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("TRAIN_TICKET_BASE_URL", train_ticket_server)
    monkeypatch.delenv("TRAIN_TICKET_ALLOWED_HOSTS", raising=False)
    monkeypatch.setenv("TRAIN_TICKET_USERNAME", "runtime-user")
    monkeypatch.setenv("TRAIN_TICKET_PASSWORD", "runtime-password")
    monkeypatch.setenv("RESULT_ARTIFACT", str(tmp_path / "train-ticket.jtl"))

    assert generator.main() == 2


@pytest.mark.parametrize(
    ("env_name", "value", "message"),
    [
        ("CONNECT_TIMEOUT_MS", "abc", "CONNECT_TIMEOUT_MS must be an integer"),
        ("CONNECT_TIMEOUT_MS", "99", "CONNECT_TIMEOUT_MS must be between 100 and 60000"),
        ("RESPONSE_TIMEOUT_MS", "120001", "RESPONSE_TIMEOUT_MS must be between 100 and 120000"),
        ("ABORT_MIN_SAMPLES", "0", "ABORT_MIN_SAMPLES must be between 1 and 100000"),
    ],
)
def test_generator_validates_runtime_integer_env(monkeypatch, tmp_path, train_ticket_server, env_name, value, message):
    generator = load_generator()
    monkeypatch.setenv(env_name, value)

    with pytest.raises(generator.WorkloadRuntimeError, match=message):
        generator.run_workload(
            workload_config("search"),
            train_ticket_server,
            "runtime-user",
            "runtime-password",
            tmp_path / "train-ticket.jtl",
            allowed_host(train_ticket_server),
        )


def test_cleanup_created_orders_must_be_boolean(tmp_path, train_ticket_server):
    generator = load_generator()
    config = workload_config("order")
    config["cleanupCreatedOrders"] = "false"

    with pytest.raises(generator.WorkloadRuntimeError, match="cleanupCreatedOrders must be a boolean"):
        generator.run_workload(
            config,
            train_ticket_server,
            "runtime-user",
            "runtime-password",
            tmp_path / "train-ticket.jtl",
            allowed_host(train_ticket_server),
        )


def test_profiles_use_python_generator_contract():
    data = yaml.safe_load(PROFILE.read_text(encoding="utf-8"))
    generator = data["spec"]["generator"]

    assert generator["kind"] == "python"
    assert generator["command"] == ["/opt/resiliencebenchmark/train-ticket/run-workload.sh"]
    assert generator["resultArtifact"] == "/results/train-ticket.jtl"
    assert "{{TRAIN_TICKET_WORKLOAD_IMAGE_DIGEST}}" in generator["image"]
    assert "jmeter" not in json.dumps(generator).lower()
    profile = next(item for item in data["spec"]["profiles"] if item["id"] == "order")
    assert profile["targetFlowQps"] == 1.0
    assert profile["concurrency"] == 1
    assert profile["cleanupCreatedOrders"] is True
    assert any(step["name"] == "cleanup-order" for step in profile["steps"])


def test_image_files_are_pinned_executable_and_do_not_embed_runtime_secrets():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    shell = (IMAGE_DIR / "run-workload.sh").read_text(encoding="utf-8")
    generator = GENERATOR.read_text(encoding="utf-8")
    combined = "\n".join([dockerfile, shell, generator])

    assert "ARG PYTHON_BASE_IMAGE=python:3.12.5-slim-bookworm@sha256:c24c34b502635f1f7c4e99dc09a2cbd85d480b7dcfd077198c6b5af138906390" in dockerfile
    assert "FROM ${PYTHON_BASE_IMAGE}" in dockerfile
    assert "pip install" not in dockerfile
    assert "chown 65532:65532 /results" in dockerfile
    assert "exec python" in shell
    assert "/results/train-ticket.jtl" in generator
    forbidden_fragments = [
        "fdse" + "_microservice",
        "111" + "111",
        ".".join(["1", "94", "151", "57"]),
        ".".join(["116", "63", "51", "45"]),
    ]
    assert all(fragment not in combined for fragment in forbidden_fragments)


class FakeBuildRunner:
    def __init__(self, digest: str):
        self.digest = digest
        self.commands: list[list[str]] = []

    def run(self, argv: list[str]) -> str:
        self.commands.append(argv)
        metadata_path = Path(argv[argv.index("--metadata-file") + 1])
        metadata_path.write_text(
            json.dumps(
                {
                    "containerimage.digest": self.digest,
                    "containerimage.config.digest": "sha256:" + "f" * 64,
                }
            ),
            encoding="utf-8",
        )
        return "built"


def test_build_script_dry_run_and_execute_use_fixed_buildx_argv(monkeypatch):
    monkeypatch.setenv("HARBOR_REGISTRY", "harbor.example:85")
    monkeypatch.setenv("HARBOR_PROJECT_TRAIN_TICKET", "train-ticket")
    plan = build_train_ticket_workload.plan_build(
        repository=None,
        tag="v1",
        dockerfile=DOCKERFILE,
        context=IMAGE_DIR,
        platform="linux/amd64",
        push=False,
    )

    dry_run = build_train_ticket_workload.build_image(plan, execute=False)
    assert dry_run["dryRun"] is True
    assert dry_run["ref"] == "harbor.example:85/train-ticket/train-ticket-workload:v1"
    assert "--load" in dry_run["command"]
    assert "--push" not in dry_run["command"]
    assert "--build-arg" in dry_run["command"]
    assert any(part.startswith("PYTHON_BASE_IMAGE=") for part in dry_run["command"])
    assert all("/Users/" not in part for part in dry_run["command"])

    digest = "sha256:" + "a" * 64
    runner = FakeBuildRunner(digest)
    output = build_train_ticket_workload.build_image(plan, execute=True, runner=runner)
    assert output["dryRun"] is False
    assert output["digest"] == digest
    assert output["pinnedImage"] == f"{plan.repository}:{plan.tag}@{digest}"
    assert runner.commands[0][:3] == ["docker", "buildx", "build"]
    assert runner.commands[0][-2:] == ["--load", str(plan.context)]


def test_build_script_accepts_only_digest_pinned_base_override(monkeypatch):
    monkeypatch.setenv("HARBOR_REGISTRY", "harbor.example:85")
    monkeypatch.setenv("HARBOR_PROJECT_TRAIN_TICKET", "train-ticket")
    pinned = "harbor.example:85/train-ticket/python-base@sha256:" + "b" * 64

    plan = build_train_ticket_workload.plan_build(
        repository=None,
        tag="v1",
        dockerfile=DOCKERFILE,
        context=IMAGE_DIR,
        platform="linux/amd64",
        push=False,
        base_image=pinned,
    )
    assert plan.base_image == pinned
    assert f"PYTHON_BASE_IMAGE={pinned}" in build_train_ticket_workload.build_argv(plan, Path("metadata.json"))

    with pytest.raises(build_train_ticket_workload.BuildError, match="base image"):
        build_train_ticket_workload.plan_build(
            repository=None,
            tag="v1",
            dockerfile=DOCKERFILE,
            context=IMAGE_DIR,
            platform="linux/amd64",
            push=False,
            base_image="harbor.example:85/train-ticket/python-base:latest",
        )


def test_build_script_push_requires_manifest_digest(monkeypatch):
    monkeypatch.setenv("HARBOR_REGISTRY", "harbor.example:85")
    monkeypatch.setenv("HARBOR_PROJECT_TRAIN_TICKET", "train-ticket")
    plan = build_train_ticket_workload.plan_build(
        repository=None,
        tag="v1",
        dockerfile=DOCKERFILE,
        context=IMAGE_DIR,
        platform="linux/amd64",
        push=True,
    )

    with pytest.raises(build_train_ticket_workload.BuildError, match="manifest digest"):
        build_train_ticket_workload.build_image(plan, execute=True, runner=FakeBuildRunner(""))


def test_build_script_cli_defaults_to_dry_run(monkeypatch):
    monkeypatch.setenv("HARBOR_REGISTRY", "harbor.example:85")
    monkeypatch.setenv("HARBOR_PROJECT_TRAIN_TICKET", "train-ticket")

    result = subprocess.run(
        ["python3", "scripts/build_train_ticket_workload.py"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    output = json.loads(result.stdout)
    assert output["dryRun"] is True
    assert output["pinnedImage"] is None
    assert result.stderr == ""
