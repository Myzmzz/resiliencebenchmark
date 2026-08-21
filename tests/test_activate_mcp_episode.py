from __future__ import annotations

import json
from pathlib import Path

import yaml

from scripts import activate_mcp_episode as activate


REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = REPO_ROOT / "tasks/schemas/episode-public.schema.json"


class FakeWriter:
    def __init__(self):
        self.calls = []

    def write_env_files(self, files, *, env_dir, mode, group_by_file):
        self.calls.append(
            {
                "files": files,
                "env_dir": env_dir,
                "mode": mode,
                "group_by_file": group_by_file,
            }
        )


class FakeRunner:
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.calls = []

    def __call__(self, argv, timeout_seconds):
        self.calls.append({"argv": argv, "timeout": timeout_seconds})
        return activate.CommandResult(
            self.returncode,
            b"using tttttttttttttttttttttttttttttttt",
            b"https://prometheus.example.invalid failed",
        )


def test_default_schema_is_repository_relative_not_current_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    episode, issues = activate.load_episode(
        REPO_ROOT / "tasks/examples/public/episode.timeout-missing.v0.1.yaml",
        activate.DEFAULT_SCHEMA,
    )

    assert issues == []
    assert episode == {"application": "otel-demo", "namespace": "otel-demo"}


def write_episode(tmp_path: Path, *, name: str = "otel-demo", namespace: str = "otel-demo") -> Path:
    episode = {
        "schema_version": "episode-public.v0.1",
        "episode_id": "EPI-ACTIVATE-001",
        "title": "Episode activation smoke test",
        "status": "example",
        "application": {
            "name": name,
            "namespace": namespace,
            "candidate_services": ["frontend"],
        },
        "agent_goal": "Validate the MCP runtime activation path for one public episode.",
        "environment_snapshot": {
            "snapshot_id": "activation-smoke",
            "health_prerequisites": ["services are ready"],
            "reset_contract": ["cleanup is complete"],
        },
        "workload": {"profile": "smoke", "slo": ["latency stays bounded"]},
        "observability": {"metrics": ["prom"], "traces": ["jaeger"], "logs": ["loki"], "kubernetes": ["pods"]},
        "source_access": {"mode": "read_only", "allowed_paths": ["/workspace/src/otel-demo"], "forbidden_paths": ["/ground_truth"]},
        "action_space": {
            "allowed_trigger_classes": ["latency"],
            "allowed_target_scope": ["namespace-local resources"],
            "forbidden_actions": ["no destructive cleanup"],
        },
        "budget": {"max_experiments": 1, "max_duration_minutes": 10, "max_concurrent_faults": 1},
        "safety_constraints": ["one namespace only"],
        "expected_agent_output": ["structured report"],
        "leakage_controls": ["no hidden answers"],
    }
    path = tmp_path / "episode.yaml"
    path.write_text(yaml.safe_dump(episode, sort_keys=False), encoding="utf-8")
    return path


def runtime_env(tmp_path: Path) -> dict[str, str]:
    kubeconfig_root = tmp_path / "kubeconfigs"
    kubeconfig_root.mkdir(exist_ok=True)
    k8s = kubeconfig_root / "k8s.yaml"
    chaos = kubeconfig_root / "chaos.yaml"
    k8s.write_text("apiVersion: v1\n", encoding="utf-8")
    chaos.write_text("apiVersion: v1\n", encoding="utf-8")
    return {
        "RESBENCH_MCP_TOKEN": "t" * 32,
        "RESBENCH_MCP_ISSUER_URL": "https://issuer.example.invalid",
        "RESBENCH_MCP_RESOURCE_URL": "https://mcp.example.invalid",
        "RESBENCH_MCP_SCOPE": "resbench:mcp",
        "RESBENCH_K8S_RO_KUBECONFIG": str(k8s),
        "RESBENCH_CHAOS_KUBECONFIG": str(chaos),
        "RESBENCH_PROMETHEUS_URL": "https://prometheus.example.invalid",
        "RESBENCH_JAEGER_URL": "https://jaeger.example.invalid",
        "RESBENCH_LOKI_URL": "https://loki.example.invalid",
        "RESBENCH_JAEGER_ALLOWED_SERVICES": "frontend,checkoutservice",
        "RESBENCH_CHAOS_CONTROLLER_TOKEN_REF": "secret/resbench-controller",
        "RESBENCH_CHAOS_CONTROLLER_POD_UID": "pod-uid-123",
        "RESBENCH_CHAOS_CONTROLLER_POD_NAMESPACE": "controller",
        "RESBENCH_CHAOS_CONTROLLER_POD_NAME": "resbench-controller",
    }


def test_dry_run_reads_episode_and_never_writes_or_leaks_runtime_values(tmp_path):
    env = runtime_env(tmp_path)
    writer = FakeWriter()

    report = activate.run_activation(
        episode_path=write_episode(tmp_path),
        schema_path=SCHEMA,
        env=env,
        kubeconfig_root=tmp_path / "kubeconfigs",
        execute=False,
        writer=writer,
    )
    encoded = json.dumps(report)

    assert report["status"] == "not_executed"
    assert report["episode"] == {"application": "otel-demo", "namespace": "otel-demo"}
    assert writer.calls == []
    for value in (
        env["RESBENCH_MCP_TOKEN"],
        env["RESBENCH_MCP_ISSUER_URL"],
        env["RESBENCH_MCP_RESOURCE_URL"],
        env["RESBENCH_K8S_RO_KUBECONFIG"],
        env["RESBENCH_CHAOS_KUBECONFIG"],
        env["RESBENCH_PROMETHEUS_URL"],
        env["RESBENCH_JAEGER_URL"],
        env["RESBENCH_LOKI_URL"],
    ):
        assert value not in encoded
    assert report["runtimeSources"]["token"] == "env"


def test_dry_run_without_runtime_secrets_reports_requirements_but_succeeds(tmp_path):
    report = activate.run_activation(
        episode_path=write_episode(tmp_path),
        schema_path=SCHEMA,
        env={},
        kubeconfig_root=tmp_path / "kubeconfigs",
    )

    assert report["status"] == "not_executed"
    assert report["issues"] == []
    assert set(report["runtimeSources"].values()) == {"missing"}
    assert "RESBENCH_MCP_TOKEN" in report["envKeys"]["k8s_ro.env"]


def test_episode_schema_name_and_namespace_are_enforced(tmp_path):
    env = runtime_env(tmp_path)

    invalid_name = activate.run_activation(
        episode_path=write_episode(tmp_path, name="unknown-app"),
        schema_path=SCHEMA,
        env=env,
        kubeconfig_root=tmp_path / "kubeconfigs",
    )
    invalid_namespace = activate.run_activation(
        episode_path=write_episode(tmp_path, namespace="bad,namespace"),
        schema_path=SCHEMA,
        env=env,
        kubeconfig_root=tmp_path / "kubeconfigs",
    )

    assert invalid_name["status"] == "blocked"
    assert invalid_namespace["status"] == "blocked"
    assert all("schema validation failed" in issue["message"] for issue in invalid_name["issues"])
    assert all("schema validation failed" in issue["message"] for issue in invalid_namespace["issues"])


def test_runtime_values_can_come_from_safe_files_and_kubeconfig_paths(tmp_path):
    env = runtime_env(tmp_path)
    token_file = tmp_path / "token"
    issuer_file = tmp_path / "issuer"
    token_file.write_text("x" * 32 + "\n", encoding="utf-8")
    issuer_file.write_text("https://issuer-from-file.example.invalid\n", encoding="utf-8")

    report = activate.run_activation(
        episode_path=write_episode(tmp_path),
        schema_path=SCHEMA,
        env={key: value for key, value in env.items() if key not in {"RESBENCH_MCP_TOKEN", "RESBENCH_MCP_ISSUER_URL"}},
        file_overrides={
            "mcp_token_file": token_file,
            "mcp_issuer_url_file": issuer_file,
            "k8s_kubeconfig": Path(env["RESBENCH_K8S_RO_KUBECONFIG"]),
            "chaos_kubeconfig": Path(env["RESBENCH_CHAOS_KUBECONFIG"]),
        },
        kubeconfig_root=tmp_path / "kubeconfigs",
    )

    assert report["status"] == "not_executed"
    assert report["runtimeSources"]["token"] == "file"
    assert report["runtimeSources"]["issuer_url"] == "file"
    assert report["runtimeSources"]["k8s_kubeconfig"] == "file"


def test_invalid_token_url_kubeconfig_and_control_chars_are_blocked_and_redacted(tmp_path):
    env = runtime_env(tmp_path)
    env["RESBENCH_MCP_TOKEN"] = "short-token"
    env["RESBENCH_PROMETHEUS_URL"] = "https://user:pass@prometheus.example.invalid/query?x=1"
    env["RESBENCH_K8S_RO_KUBECONFIG"] = str(tmp_path / "missing.yaml")
    env["RESBENCH_CHAOS_CONTROLLER_POD_NAME"] = "controller\nleak"

    report = activate.run_activation(
        episode_path=write_episode(tmp_path),
        schema_path=SCHEMA,
        env=env,
        kubeconfig_root=tmp_path / "kubeconfigs",
    )
    encoded = json.dumps(report)

    assert report["status"] == "blocked"
    fields = {issue["field"] for issue in report["issues"]}
    assert {"token", "prometheus_url", "k8s_kubeconfig", "controller_pod_name"}.issubset(fields)
    assert "short-token" not in encoded
    assert "user:pass" not in encoded
    assert "controller\nleak" not in encoded


def test_kubeconfig_must_stay_under_dedicated_service_root(tmp_path):
    env = runtime_env(tmp_path)
    outside = tmp_path / "admin-kubeconfig.yaml"
    outside.write_text("apiVersion: v1\n", encoding="utf-8")
    env["RESBENCH_K8S_RO_KUBECONFIG"] = str(outside)

    report = activate.run_activation(
        episode_path=write_episode(tmp_path),
        schema_path=SCHEMA,
        env=env,
        kubeconfig_root=tmp_path / "kubeconfigs",
    )

    assert report["status"] == "blocked"
    assert any(
        issue["field"] == "k8s_kubeconfig" and "dedicated service kubeconfig root" in issue["message"]
        for issue in report["issues"]
    )


def test_kubeconfig_permission_check_requires_service_group_read_and_no_world_access(tmp_path, monkeypatch):
    path = tmp_path / "kubeconfig"
    path.write_text("apiVersion: v1\n", encoding="utf-8")
    path.chmod(0o644)
    metadata = path.stat()

    class Group:
        gr_gid = metadata.st_gid

    monkeypatch.setattr(activate.grp, "getgrnam", lambda _name: Group())
    monkeypatch.setattr(activate.os, "geteuid", lambda: metadata.st_uid)

    issues = activate.check_kubeconfig_permissions({"k8s_kubeconfig": path})

    assert any("inaccessible to others" in issue["message"] for issue in issues)


def test_execute_atomically_writes_expected_env_files_with_permissions_and_keys(tmp_path):
    env = runtime_env(tmp_path)
    writer = FakeWriter()

    report = activate.run_activation(
        episode_path=write_episode(tmp_path, name="sock-shop", namespace="sock-shop"),
        schema_path=SCHEMA,
        env=env,
        kubeconfig_root=tmp_path / "kubeconfigs",
        execute=True,
        writer=writer,
        host_check=lambda: [],
        kubeconfig_access_check=lambda _paths: [],
    )

    assert report["status"] == "activated"
    assert len(writer.calls) == 1
    call = writer.calls[0]
    assert call["mode"] == 0o640
    assert call["group_by_file"] == activate.SERVICE_GROUPS
    assert set(call["files"]) == {"k8s_ro.env", "telemetry_ro.env", "source_ro.env", "chaos_control.env"}
    assert 'RESBENCH_K8S_RO_NAMESPACE_ALLOWLIST="sock-shop"' in call["files"]["k8s_ro.env"]
    assert 'RESBENCH_SOURCE_ALLOWED_APPLICATIONS="sock-shop"' in call["files"]["source_ro.env"]
    assert 'RESBENCH_CHAOS_EXECUTE_ENABLED="false"' in call["files"]["chaos_control.env"]
    assert 'RESBENCH_TELEMETRY_ALLOW_RAW_QUERIES="false"' in call["files"]["telemetry_ro.env"]
    assert '\nRESBENCH_MCP_TOKEN="' in call["files"]["k8s_ro.env"] or call["files"]["k8s_ro.env"].startswith("RESBENCH_MCP_TOKEN=")
    assert report["envKeys"]["source_ro.env"] == [
        "RESBENCH_MCP_ISSUER_URL",
        "RESBENCH_MCP_RESOURCE_URL",
        "RESBENCH_MCP_SCOPE",
        "RESBENCH_MCP_TOKEN",
        "RESBENCH_SOURCE_ALLOWED_APPLICATIONS",
        "RESBENCH_SOURCE_ROOT",
    ]


def test_execute_requires_host_root_and_resbench_before_writing(tmp_path):
    writer = FakeWriter()

    report = activate.run_activation(
        episode_path=write_episode(tmp_path),
        schema_path=SCHEMA,
        env=runtime_env(tmp_path),
        kubeconfig_root=tmp_path / "kubeconfigs",
        execute=True,
        writer=writer,
        host_check=lambda: [{"severity": "ERROR", "field": "host", "message": "execute requires root"}],
    )

    assert report["status"] == "blocked"
    assert writer.calls == []


def test_restart_is_opt_in_and_restart_failure_fails_closed(tmp_path):
    env = runtime_env(tmp_path)
    writer = FakeWriter()
    runner = FakeRunner()

    no_restart = activate.run_activation(
        episode_path=write_episode(tmp_path),
        schema_path=SCHEMA,
        env=env,
        kubeconfig_root=tmp_path / "kubeconfigs",
        execute=True,
        writer=writer,
        runner=runner,
        host_check=lambda: [],
        kubeconfig_access_check=lambda _paths: [],
    )
    assert no_restart["status"] == "activated"
    assert runner.calls == []

    failing = FakeRunner(returncode=1)
    report = activate.run_activation(
        episode_path=write_episode(tmp_path),
        schema_path=SCHEMA,
        env=env,
        kubeconfig_root=tmp_path / "kubeconfigs",
        execute=True,
        restart=True,
        writer=FakeWriter(),
        runner=failing,
        host_check=lambda: [],
        kubeconfig_access_check=lambda _paths: [],
    )
    encoded = json.dumps(report)

    assert report["status"] == "failed"
    assert failing.calls[0]["argv"] == ["systemctl", "restart", *activate.UNIT_NAMES]
    assert env["RESBENCH_MCP_TOKEN"] not in encoded
    assert env["RESBENCH_PROMETHEUS_URL"] not in encoded
