import json
from pathlib import Path

from scripts import locust_workload


class FakeRunner:
    def __init__(self):
        self.calls = []

    def run(self, argv, *, stdin=None):
        self.calls.append((argv, stdin))
        return "ok"


def test_sock_shop_render_uses_seed_mix_secret_refs_and_no_values():
    defaults, app = locust_workload.load_application("sock-shop")
    fixture = locust_workload.load_fixture(Path("environment/workloads/sock-shop/runtime-fixture.example.yaml"), "sock-shop")
    image = "harbor.example/workload:commit@sha256:" + "a" * 64

    plan = locust_workload.render_plan("sock-shop", "baseline-001", defaults, app, fixture, image, None)
    rendered = json.dumps(plan)

    assert plan["randomSeed"] == 2026082202
    assert sum(item["weightPercent"] for item in plan["trafficMix"]) == 100
    assert "secretKeyRef" in rendered
    assert "sock-shop-workload-user" in rendered
    assert "password=" not in rendered
    assert plan["manifest"][1]["spec"]["template"]["spec"]["containers"][0]["image"] == image


def test_otel_demo_render_has_no_credential_environment():
    defaults, app = locust_workload.load_application("otel-demo")
    fixture = locust_workload.load_fixture(Path("environment/workloads/otel-demo/runtime-fixture.example.yaml"), "otel-demo")
    image = "harbor.example/workload:commit@sha256:" + "b" * 64

    plan = locust_workload.render_plan("otel-demo", "baseline-001", defaults, app, fixture, image, 12.0)
    env = plan["manifest"][1]["spec"]["template"]["spec"]["containers"][0]["env"]

    assert plan["randomSeed"] == 2026082203
    assert not any(item["name"].startswith("SOCK_SHOP_") for item in env)
    assert {"name": "RESBENCH_BASELINE_THROUGHPUT_RPS", "value": "12.0"} in env


def test_real_start_requires_digest_pinned_image(tmp_path):
    kubeconfig = tmp_path / "config"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")
    runner = FakeRunner()

    code = locust_workload.run(
        [
            "start",
            "--application",
            "otel-demo",
            "--fixture",
            "environment/workloads/otel-demo/runtime-fixture.example.yaml",
            "--image",
            "harbor.example/workload:latest",
            "--kubeconfig",
            str(kubeconfig),
            "--execute",
        ],
        runner=runner,
    )

    assert code == 2
    assert runner.calls == []


def test_cli_accepts_bounded_smoke_duration(capsys):
    image = "harbor.example/workload:commit@sha256:" + "c" * 64

    code = locust_workload.run(
        [
            "validate",
            "--application",
            "otel-demo",
            "--fixture",
            "environment/workloads/otel-demo/runtime-fixture.example.yaml",
            "--image",
            image,
            "--duration-seconds",
            "60",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert report["plan"]["durationSeconds"] == 60
