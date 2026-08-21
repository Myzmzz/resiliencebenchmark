import json
from pathlib import Path

import pytest

from scripts import benchmark_prepare


def write_required_scaffold(root: Path) -> None:
    for rel in benchmark_prepare.REPO_REQUIRED_FILES:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# ok\n", encoding="utf-8")


def messages(issues):
    return [issue.message for issue in issues]


def test_validate_repo_accepts_minimal_scaffold(tmp_path):
    write_required_scaffold(tmp_path)

    issues = benchmark_prepare.validate_static_repo(tmp_path)

    assert not [issue for issue in issues if issue.severity == "ERROR"]
    assert any("no YAML/JSON configuration files" in issue.message for issue in issues)


def test_validate_repo_rejects_secrets_public_ips_and_user_paths(tmp_path):
    write_required_scaffold(tmp_path)
    config = tmp_path / "environment" / "targets.yaml"
    config.write_text(
        "\n".join(
            [
                "kind: ApplicationTarget",
                "apiVersion: benchmark/v1",
                "metadata:",
                "  name: unsafe",
                "spec:",
                "  namespace: default",
                "  sourceRef: /Users/example/project",
                "  observability: http://1.2.3.4:9090",
                "  apiKey: sk-abcdefghijklmnopqrstuvwxyz",
            ]
        ),
        encoding="utf-8",
    )

    issues = benchmark_prepare.validate_static_repo(tmp_path)

    combined = "\n".join(messages(issues))
    assert "possible secret material found" in combined
    assert "user absolute path found" in combined
    assert "public IPv4 address found: 1.2.3.4" in combined


def test_secret_scan_allows_typed_and_environment_credential_references():
    assert not benchmark_prepare.contains_secret_material("api_key: str")
    assert not benchmark_prepare.contains_secret_material("api_key = env.get(API_KEY_ENV)")
    assert benchmark_prepare.contains_secret_material("api_key: real-literal-value")
    assert benchmark_prepare.contains_secret_material("token: resbench_actualtokenvalue")
    assert benchmark_prepare.contains_secret_material("password: resbench_actualpassword")
    assert not benchmark_prepare.contains_secret_material("if not api_key:\n    report['status'] = 'missing'")
    assert not benchmark_prepare.contains_secret_material("token = _required(values, TOKEN_ENV)")
    assert not benchmark_prepare.contains_secret_material("token_verifier=token,")
    assert benchmark_prepare.contains_secret_material("token = hardcoded_runtime_value")
    assert benchmark_prepare.contains_secret_material("token = stronghardcodedvalue")


def test_validate_repo_rejects_agent_visible_ground_truth_keys(tmp_path):
    write_required_scaffold(tmp_path)
    config = tmp_path / "tasks" / "episode.yaml"
    config.write_text(
        "\n".join(
            [
                "kind: EpisodeSpec",
                "apiVersion: benchmark/v1",
                "metadata:",
                "  name: leaked",
                "spec:",
                "  application: train-ticket",
                "  visibleInputs: {}",
                "  safety: {}",
                "  budget: {}",
                "  oracleRef: hidden",
                "  groundTruth:",
                "    defect: retry-storm",
            ]
        ),
        encoding="utf-8",
    )

    issues = benchmark_prepare.validate_static_repo(tmp_path)

    assert any("ground truth key is agent-visible" in issue.message for issue in issues)


def test_validate_repo_allows_public_ground_truth_schema_contract(tmp_path):
    write_required_scaffold(tmp_path)
    schema = tmp_path / "tasks" / "schemas" / "ground-truth.schema.json"
    schema.parent.mkdir(parents=True, exist_ok=True)
    schema.write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {"hidden_truth_ref": {"type": "string"}},
            }
        ),
        encoding="utf-8",
    )

    issues = benchmark_prepare.validate_static_repo(tmp_path)

    assert not [issue for issue in issues if issue.severity == "ERROR"]


def test_validate_repo_checks_known_kind_required_fields(tmp_path):
    write_required_scaffold(tmp_path)
    config = tmp_path / "harness" / "agent.yaml"
    config.write_text(
        "\n".join(
            [
                "kind: HarnessSpec",
                "apiVersion: benchmark/v1",
                "metadata:",
                "  name: codex",
                "spec:",
                "  agent: codex",
            ]
        ),
        encoding="utf-8",
    )

    issues = benchmark_prepare.validate_static_repo(tmp_path)

    assert any("missing required field spec.models" in issue.message for issue in issues)
    assert any("missing required field spec.tools" in issue.message for issue in issues)


def test_parse_config_accepts_inert_custom_yaml_tags(tmp_path):
    config = tmp_path / "mcp.cordis.patch.yml"
    config.write_text("url: !!js process.env.MCP_URL\n", encoding="utf-8")

    data, error = benchmark_prepare.parse_config_file(config)

    assert error is None
    assert data == {"url": "process.env.MCP_URL"}


def test_validate_structured_contract_accepts_cordis_patch_list():
    issues = benchmark_prepare.validate_structured_contract(
        "harness/example.cordis.patch.yml",
        [{"insert": [{"id": "mcp-example"}]}],
    )

    assert issues == []


def test_run_kubectl_refuses_mutating_verbs(tmp_path):
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="read-only get allowlist"):
        benchmark_prepare.run_kubectl(kubeconfig, ["delete", "pods", "--all"])

    with pytest.raises(RuntimeError, match="read-only get allowlist"):
        benchmark_prepare.run_kubectl(
            kubeconfig,
            ["--namespace", "default", "delete", "pods", "example"],
        )


def test_qualify_namespace_summarizes_ready_deployments(monkeypatch, tmp_path):
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")

    def fake_run(_kubeconfig, args):
        assert args[0] == "get"
        assert "secret" not in args
        if args[:2] == ["get", "namespace"]:
            return {"metadata": {"name": args[2]}}
        if args[:2] == ["get", "deployments"]:
            return {
                "items": [
                    {
                        "metadata": {"name": "api"},
                        "spec": {"replicas": 2},
                        "status": {"readyReplicas": 1, "availableReplicas": 1},
                    },
                    {
                        "metadata": {"name": "load-generator"},
                        "spec": {"replicas": 0},
                        "status": {},
                    },
                ]
            }
        raise AssertionError(args)

    monkeypatch.setattr(benchmark_prepare, "run_kubectl", fake_run)

    result = benchmark_prepare.qualify_namespace(kubeconfig, "demo")

    assert result["exists"] is True
    assert result["loadGenerators"][0]["name"] == "load-generator"
    issue_text = json.dumps(result["issues"])
    assert "ready 1/2" in issue_text
    assert "0 replicas" in issue_text


def test_qualify_namespace_rejects_empty_target(monkeypatch, tmp_path):
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")

    def fake_run(_kubeconfig, args):
        if args[:2] == ["get", "namespace"]:
            return {"metadata": {"name": args[2]}}
        if args[:2] == ["get", "deployments"]:
            return {"items": []}
        raise AssertionError(args)

    monkeypatch.setattr(benchmark_prepare, "run_kubectl", fake_run)

    result = benchmark_prepare.qualify_namespace(kubeconfig, "empty")

    assert any(issue["severity"] == "ERROR" for issue in result["issues"])


def test_qualify_namespace_rejects_all_scaled_zero(monkeypatch, tmp_path):
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")

    def fake_run(_kubeconfig, args):
        if args[:2] == ["get", "namespace"]:
            return {"metadata": {"name": args[2]}}
        if args[:2] == ["get", "deployments"]:
            return {"items": [{"metadata": {"name": "api"}, "spec": {"replicas": 0}, "status": {}}]}
        raise AssertionError(args)

    monkeypatch.setattr(benchmark_prepare, "run_kubectl", fake_run)

    result = benchmark_prepare.qualify_namespace(kubeconfig, "paused")

    assert any("scaled to zero" in issue["message"] for issue in result["issues"])


def test_qualify_chaosblade_rejects_nonterminal_resources(monkeypatch, tmp_path):
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")

    def fake_run(_kubeconfig, args):
        assert args[:2] == ["get", "chaosblades.chaosblade.io"]
        return {"items": [{"status": {"phase": "Running"}}, {"status": {"phase": "Error"}}]}

    monkeypatch.setattr(benchmark_prepare, "run_kubectl", fake_run)

    result = benchmark_prepare.qualify_chaosblade(kubeconfig)

    assert result["count"] == 2
    assert any(issue["severity"] == "ERROR" for issue in result["issues"])
