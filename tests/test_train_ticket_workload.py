from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts import train_ticket_workload


PROFILE = Path("environment/workloads/train-ticket/profiles.yaml")
EXAMPLE_FIXTURE = Path("environment/workloads/train-ticket/runtime-fixture.example.yaml")
TARGET_HOST = "ts-ui-dashboard.train-ticket.svc.cluster.local"


class FakeRunner:
    def __init__(self, status: dict | None = None):
        self.commands: list[list[str]] = []
        self.inputs: list[str | None] = []
        self.status = status or {"items": []}

    def run(self, argv: list[str], *, stdin: str | None = None) -> str:
        self.commands.append(argv)
        self.inputs.append(stdin)
        if "apply" in argv:
            return "configmap/tt-workload-created\njob.batch/tt-workload-created"
        if "delete" in argv:
            return "job.batch deleted"
        if "get" in argv:
            return json.dumps(self.status)
        raise AssertionError(argv)


def fixture_file(
    tmp_path: Path,
    *,
    secret_ref: bool = True,
    base_url: str | None = None,
    allowed_hosts: list[str] | None = None,
    pvc_claim: str = "train-ticket-workload-results",
) -> Path:
    credentials = (
        {
            "kubernetes_secret_ref": {
                "name": "train-ticket-workload-user",
                "username_key": "username",
                "password_key": "password",
            }
        }
        if secret_ref
        else {"username_env": "TRAIN_TICKET_USERNAME", "password_env": "TRAIN_TICKET_PASSWORD"}
    )
    path = tmp_path / "runtime.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "target": {
                    "base_url": base_url or f"http://{TARGET_HOST}",
                    "base_url_ref": "runtime://fixture/base-url",
                    "allowed_hosts": allowed_hosts or [TARGET_HOST],
                },
                "credentials": credentials,
                "artifacts": {"pvc_claim": pvc_claim},
                "cluster": {"allowed_namespaces": ["train-ticket"]},
                "scenario": {
                    "from_station": "Shang Hai",
                    "to_station": "Su Zhou",
                    "travel_date": "2026-08-22",
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return path


def test_render_is_deterministic_and_has_controller_labels(tmp_path):
    fixture = train_ticket_workload.load_fixture(fixture_file(tmp_path))
    profile = train_ticket_workload.load_profile(PROFILE, "order")

    first = train_ticket_workload.render_plan("tt-run-001", "train-ticket", profile, fixture)
    second = train_ticket_workload.render_plan("tt-run-001", "train-ticket", profile, fixture)

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    labels = first["manifest"][1]["metadata"]["labels"]
    assert labels["resiliencebenchmark.io/run-id"] == "tt-run-001"
    assert labels["resiliencebenchmark.io/owner"] == "benchmark-controller"
    assert first["targetUrlRef"] == "runtime://fixture/base-url"
    assert first["targetFlowQps"] == 1.0
    assert first["concurrency"] == 1
    assert first["durationSeconds"] == 600
    assert first["abortThresholds"]["maxP95LatencyMs"] == 2500
    assert first["resultArtifact"] == "/results/train-ticket.jtl"
    assert first["artifactRef"] == "pvc://train-ticket/train-ticket-workload-results/results/train-ticket.jtl"
    assert first["artifactPvcClaim"] == "train-ticket-workload-results"
    workload_env = first["manifest"][1]["spec"]["template"]["spec"]["containers"][0]["env"]
    assert {"name": "TRAIN_TICKET_ALLOWED_HOSTS", "value": TARGET_HOST} in workload_env
    assert first["manifest"][1]["spec"]["template"]["spec"]["containers"][0]["name"] == "workload-generator"
    assert first["workloadConfig"]["cleanupCreatedOrders"] is True


def test_repository_example_fixture_is_valid():
    fixture = train_ticket_workload.load_fixture(EXAMPLE_FIXTURE)

    assert fixture.base_url == f"http://{TARGET_HOST}"
    assert fixture.allowed_hosts == (TARGET_HOST,)
    assert fixture.pvc_claim == "train-ticket-workload-results"


def test_fixture_rejects_literal_secret_fields(tmp_path):
    path = fixture_file(tmp_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["credentials"]["password"] = "do-not-store-this"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")

    with pytest.raises(train_ticket_workload.WorkloadError, match="literal credential fields"):
        train_ticket_workload.load_fixture(path)


def test_render_secret_ref_without_secret_values(tmp_path):
    fixture = train_ticket_workload.load_fixture(fixture_file(tmp_path))
    profile = train_ticket_workload.load_profile(PROFILE, "order")

    plan = train_ticket_workload.render_plan("tt-run-002", "train-ticket", profile, fixture)
    rendered = json.dumps(plan, sort_keys=True)

    assert "secretKeyRef" in rendered
    assert "train-ticket-workload-user" in rendered
    assert "do-not-store-this" not in rendered
    assert "password:" not in rendered


def test_target_host_allowlist_rejects_ssrf_and_userinfo(tmp_path):
    with pytest.raises(train_ticket_workload.WorkloadError, match="hostname is not in target.allowed_hosts"):
        train_ticket_workload.load_fixture(
            fixture_file(
                tmp_path,
                base_url="http://metadata.google.internal",
                allowed_hosts=[TARGET_HOST],
            )
        )
    with pytest.raises(train_ticket_workload.WorkloadError, match="must not contain userinfo"):
        train_ticket_workload.load_fixture(
            fixture_file(
                tmp_path,
                base_url=f"http://user:secret@{TARGET_HOST}",
                allowed_hosts=[TARGET_HOST],
            )
        )
    with pytest.raises(train_ticket_workload.WorkloadError, match="must use http or https"):
        train_ticket_workload.load_fixture(
            fixture_file(
                tmp_path,
                base_url=f"file://{TARGET_HOST}/etc/passwd",
                allowed_hosts=[TARGET_HOST],
            )
        )
    with pytest.raises(train_ticket_workload.WorkloadError, match="must not contain a path"):
        train_ticket_workload.load_fixture(
            fixture_file(
                tmp_path,
                base_url=f"http://{TARGET_HOST}/unexpected?redirect=http://elsewhere.invalid",
                allowed_hosts=[TARGET_HOST],
            )
        )


def test_allowed_hosts_rejects_ports_paths_and_duplicates(tmp_path):
    with pytest.raises(train_ticket_workload.WorkloadError, match="without scheme, userinfo, port, or path"):
        train_ticket_workload.load_fixture(fixture_file(tmp_path, allowed_hosts=[f"{TARGET_HOST}:8080"]))
    with pytest.raises(train_ticket_workload.WorkloadError, match="duplicate hosts"):
        train_ticket_workload.load_fixture(fixture_file(tmp_path, allowed_hosts=[TARGET_HOST, TARGET_HOST]))


def test_env_ref_mode_does_not_render_literal_credentials(tmp_path):
    fixture = train_ticket_workload.load_fixture(fixture_file(tmp_path, secret_ref=False))
    profile = train_ticket_workload.load_profile(PROFILE, "search")

    plan = train_ticket_workload.render_plan("tt-run-003", "train-ticket", profile, fixture)
    rendered = json.dumps(plan, sort_keys=True)

    assert plan["credentialMode"] == "environmentRef"
    assert "TRAIN_TICKET_PASSWORD" in rendered
    assert "secretKeyRef" not in rendered


def test_boundaries_reject_bad_run_id_and_namespace(tmp_path):
    fixture = train_ticket_workload.load_fixture(fixture_file(tmp_path))

    with pytest.raises(train_ticket_workload.WorkloadError, match="run_id"):
        train_ticket_workload.validate_run_id("Bad_Run")
    with pytest.raises(train_ticket_workload.WorkloadError, match="not in fixture allowlist"):
        train_ticket_workload.assert_namespace_allowed("default", fixture)


def test_start_defaults_to_dry_run_without_kubectl(tmp_path, capsys):
    fixture = fixture_file(tmp_path)

    code = train_ticket_workload.run(
        ["start", "--fixture", str(fixture), "--profile-file", str(PROFILE), "--run-id", "tt-run-004"],
        runner=FakeRunner(),
    )

    output = json.loads(capsys.readouterr().out)
    assert code == 0
    assert output["dryRun"] is True
    assert output["action"] == "start"
    assert output["plan"]["objects"][1]["kind"] == "Job"
    assert output["plan"]["artifactRef"] == "pvc://train-ticket/train-ticket-workload-results/results/train-ticket.jtl"


def test_execute_start_requires_resolved_digest(tmp_path, capsys):
    fixture = fixture_file(tmp_path)
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")

    code = train_ticket_workload.run(
        [
            "start",
            "--execute",
            "--fixture",
            str(fixture),
            "--profile-file",
            str(PROFILE),
            "--run-id",
            "tt-run-005",
            "--kubeconfig",
            str(kubeconfig),
        ],
        runner=FakeRunner(),
    )

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert "resolved image pinned as name@sha256" in captured.err


def test_execute_start_rejects_indirect_environment_credentials(tmp_path, capsys):
    fixture = fixture_file(tmp_path, secret_ref=False)
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")

    code = train_ticket_workload.run(
        [
            "start",
            "--execute",
            "--fixture",
            str(fixture),
            "--profile-file",
            str(PROFILE),
            "--run-id",
            "tt-run-credentials",
            "--kubeconfig",
            str(kubeconfig),
        ],
        runner=FakeRunner(),
    )

    captured = capsys.readouterr()
    assert code == 2
    assert "requires credentials.kubernetes_secret_ref" in captured.err


def test_execute_start_renders_results_persistent_volume_claim(tmp_path, capsys):
    fixture = fixture_file(tmp_path, pvc_claim="tt-results-pvc")
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")
    profile = yaml.safe_load(PROFILE.read_text(encoding="utf-8"))
    profile["spec"]["generator"]["image"] = "harbor.example/train-ticket/workload@sha256:" + "1" * 64
    profile_path = tmp_path / "profiles.yaml"
    profile_path.write_text(yaml.safe_dump(profile), encoding="utf-8")
    runner = FakeRunner()

    code = train_ticket_workload.run(
        [
            "start",
            "--execute",
            "--fixture",
            str(fixture),
            "--profile-file",
            str(profile_path),
            "--run-id",
            "tt-run-pvc",
            "--kubeconfig",
            str(kubeconfig),
        ],
        runner=runner,
    )

    output = json.loads(capsys.readouterr().out)
    manifest = yaml.safe_load_all(runner.inputs[0])
    rendered = list(manifest)
    job = rendered[1]
    volumes = job["spec"]["template"]["spec"]["volumes"]
    assert code == 0
    assert output["plan"]["artifactPvcClaim"] == "tt-results-pvc"
    assert {"name": "results", "persistentVolumeClaim": {"claimName": "tt-results-pvc"}} in volumes
    assert "emptyDir" not in json.dumps(job)


def test_stop_deletes_only_this_run_id_objects(tmp_path):
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")
    runner = FakeRunner()

    output = train_ticket_workload.stop_run(kubeconfig, "train-ticket", "tt-run-006", runner)

    command = runner.commands[0]
    assert output == "job.batch deleted"
    assert command[:4] == ["kubectl", "--kubeconfig", str(kubeconfig), "--namespace"]
    assert command[-4:] == [
        "job,configmap",
        "-l",
        "resiliencebenchmark.io/workload=train-ticket,resiliencebenchmark.io/run-id=tt-run-006",
        "--ignore-not-found=true",
    ]
    assert "persistentvolumeclaim" not in " ".join(command).lower()
    assert "pvc" not in " ".join(command).lower()


def test_status_uses_run_id_selector(tmp_path):
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")
    runner = FakeRunner(status={"items": [{"kind": "Job", "metadata": {"name": "tt-workload"}, "status": {"active": 1}}]})

    status = train_ticket_workload.get_status(kubeconfig, "train-ticket", "tt-run-007", runner)

    assert status["items"][0]["active"] == 1
    assert any("resiliencebenchmark.io/run-id=tt-run-007" in part for part in runner.commands[0])


def test_cli_validate_outputs_summary_without_manifest(tmp_path):
    fixture = fixture_file(tmp_path)

    result = subprocess.run(
        [
            "python3",
            "scripts/train_ticket_workload.py",
            "validate",
            "--fixture",
            str(fixture),
            "--profile-file",
            str(PROFILE),
            "--run-id",
            "tt-run-008",
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    output = json.loads(result.stdout)
    assert output["valid"] is True
    assert "manifest" not in output["plan"]
    assert output["plan"]["targetUrlRef"] == "runtime://fixture/base-url"
    assert result.stderr == ""
