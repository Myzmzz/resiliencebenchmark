from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scripts import render_litellm_gateway as renderer
from stage2_service.contracts import (
    STAGE2_DEFAULT_MODEL,
    STAGE2_MODEL_MATRIX,
    STAGE2_SUPPORTED_MODELS,
)

PRODUCTION_CONFIG = Path("deploy/stage2/litellm/config.yaml")
PRODUCTION_ENV_EXAMPLE = Path("deploy/stage2/litellm/providers.env.example")
PRODUCTION_REGISTRY = Path("harness/models.yaml")

SAMPLE_CONFIG = """
model_list:
  - model_name: alias-a
    litellm_params:
      model: openai/upstream-a
      api_base: https://gateway.example/v1
      api_key: os.environ/PROVIDER_A_KEY
  - model_name: alias-b
    litellm_params:
      model: anthropic/upstream-b
      api_key: os.environ/PROVIDER_B_KEY
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
"""


def test_production_config_references_are_covered_by_env_example():
    config = yaml.safe_load(PRODUCTION_CONFIG.read_text(encoding="utf-8"))
    example = renderer.parse_env_file(PRODUCTION_ENV_EXAMPLE)

    assert renderer.required_names(config) <= set(example)
    assert "LITELLM_MASTER_KEY" in renderer.required_names(config)


def test_production_config_serves_every_stage2_matrix_alias():
    """Gateway routing, the Harness registry, and the Stage-2 matrix must agree."""
    config = yaml.safe_load(PRODUCTION_CONFIG.read_text(encoding="utf-8"))
    registry = yaml.safe_load(PRODUCTION_REGISTRY.read_text(encoding="utf-8"))["models"]
    aliases = set(renderer.model_aliases(config))

    assert set(STAGE2_SUPPORTED_MODELS) <= aliases
    assert set(STAGE2_SUPPORTED_MODELS) <= set(registry)
    assert set(STAGE2_MODEL_MATRIX) <= set(STAGE2_SUPPORTED_MODELS)
    assert STAGE2_DEFAULT_MODEL in STAGE2_MODEL_MATRIX
    assert all(alias == alias.lower() for alias in STAGE2_SUPPORTED_MODELS)
    # Every alias resolves to exactly one upstream: no silent load-balancing.
    assert len(renderer.model_aliases(config)) == len(aliases)


def test_parse_env_file_handles_export_quotes_and_comments(tmp_path):
    env_file = tmp_path / "providers.env"
    env_file.write_text(
        "# comment\n\nexport PROVIDER_A_KEY='sk-a'\nPROVIDER_B_KEY = \"sk-b\"\nLITELLM_MASTER_KEY=sk-master\n",
        encoding="utf-8",
    )

    assert renderer.parse_env_file(env_file) == {
        "PROVIDER_A_KEY": "sk-a",
        "PROVIDER_B_KEY": "sk-b",
        "LITELLM_MASTER_KEY": "sk-master",
    }


def test_missing_credential_is_reported():
    config = yaml.safe_load(SAMPLE_CONFIG)

    problems = renderer.validate_credentials(config, {"PROVIDER_A_KEY": "sk-a", "LITELLM_MASTER_KEY": " "})

    assert problems == [
        "missing or empty credential: LITELLM_MASTER_KEY",
        "missing or empty credential: PROVIDER_B_KEY",
    ]


def test_render_writes_configmap_and_secret_with_only_referenced_names(tmp_path):
    config = yaml.safe_load(SAMPLE_CONFIG)
    env = {
        "PROVIDER_A_KEY": "sk-a",
        "PROVIDER_B_KEY": "sk-b",
        "LITELLM_MASTER_KEY": "sk-master",
        "UNRELATED_TOKEN": "leave-me-out",
    }

    configmap, secret, client_secret = renderer.render_manifests(SAMPLE_CONFIG, config, env, "ns-test")
    written = renderer.write_manifests(tmp_path, configmap, secret, client_secret)

    assert configmap["metadata"] == {
        "name": "litellm-config",
        "namespace": "ns-test",
        "labels": {"app.kubernetes.io/managed-by": "resiliencebenchmark"},
    }
    assert configmap["data"]["config.yaml"] == SAMPLE_CONFIG
    assert configmap["data"]["gateway_audit.py"] == renderer.AUDIT_CALLBACK_PATH.read_text()
    assert secret["stringData"] == {
        "LITELLM_MASTER_KEY": "sk-master",
        "PROVIDER_A_KEY": "sk-a",
        "PROVIDER_B_KEY": "sk-b",
    }
    assert [path.name for path in written] == [
        "litellm-config.configmap.yaml",
        "litellm-upstream.secret.yaml",
        "resbench-stage2-gateway-client.secret.yaml",
    ]
    reloaded = yaml.safe_load(written[1].read_text(encoding="utf-8"))
    assert "UNRELATED_TOKEN" not in reloaded["stringData"]
    assert client_secret["metadata"]["name"] == "resbench-stage2-gateway-client"
    assert client_secret["stringData"] == {"llm-base-url": "http://127.0.0.1:4000/v1", "llm-api-key": "sk-master"}
    assert "PROVIDER_A_KEY" not in str(client_secret)
    assert (written[1].stat().st_mode & 0o777) == 0o600


def test_gateway_callback_instance_is_enabled_in_production():
    config = yaml.safe_load(PRODUCTION_CONFIG.read_text())
    assert config["litellm_settings"]["callbacks"] == ["gateway_audit.logger_instance"]


def test_cli_check_mode_exits_non_zero_without_writing(tmp_path, capsys):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(SAMPLE_CONFIG, encoding="utf-8")
    env_file = tmp_path / "providers.env"
    env_file.write_text("PROVIDER_A_KEY=sk-a\nLITELLM_MASTER_KEY=sk-master\n", encoding="utf-8")

    code = renderer.main(["--config", str(config_file), "--env-file", str(env_file), "--output-dir", str(tmp_path / "out")])

    captured = capsys.readouterr()
    assert code == 2
    assert "PROVIDER_B_KEY: MISSING" in captured.out
    assert "sk-a" not in captured.out and "sk-a" not in captured.err
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("flag", [["--check"], []])
def test_cli_complete_env_reports_success(tmp_path, capsys, flag):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(SAMPLE_CONFIG, encoding="utf-8")
    env_file = tmp_path / "providers.env"
    env_file.write_text("PROVIDER_A_KEY=sk-a\nPROVIDER_B_KEY=sk-b\nLITELLM_MASTER_KEY=sk-master\n", encoding="utf-8")

    code = renderer.main(["--config", str(config_file), "--env-file", str(env_file), *flag])

    assert code == 0
    assert "credentials complete" in capsys.readouterr().out


def test_pacing_is_absent_unless_asked_for():
    """An unpaced render must ship the reviewed routing table unchanged."""
    config = yaml.safe_load(renderer.DEFAULT_CONFIG.read_text(encoding="utf-8"))

    assert renderer.pace_config(
        config, max_parallel_requests=None, replicas=1, account_rpm=None, account_tpm=None
    ) == config


def test_pacing_divides_the_account_budget_over_the_replicas():
    """Each Pod runs its own gateway and cannot see the others' traffic."""
    config = yaml.safe_load(renderer.DEFAULT_CONFIG.read_text(encoding="utf-8"))

    paced = renderer.pace_config(
        config, max_parallel_requests=2, replicas=5, account_rpm=500, account_tpm=3_000_000
    )

    assert paced["litellm_settings"]["max_parallel_requests"] == 2
    # The deliberate choice not to re-issue a request the transcript omits.
    assert paced["litellm_settings"]["num_retries"] == 0
    for entry in paced["model_list"]:
        assert entry["litellm_params"]["rpm"] == 100
        assert entry["litellm_params"]["tpm"] == 600_000
    # Routing itself is untouched: same aliases, same upstreams.
    assert [item["model_name"] for item in paced["model_list"]] == [
        item["model_name"] for item in config["model_list"]
    ]
    assert [item["litellm_params"]["api_base"] for item in paced["model_list"]] == [
        item["litellm_params"]["api_base"] for item in config["model_list"]
    ]


def test_pacing_refuses_a_budget_that_leaves_nothing_per_replica():
    config = yaml.safe_load(renderer.DEFAULT_CONFIG.read_text(encoding="utf-8"))

    with pytest.raises(ValueError, match="less than one request"):
        renderer.pace_config(config, max_parallel_requests=None, replicas=20,
                             account_rpm=10, account_tpm=None)
    with pytest.raises(ValueError, match="less than one token"):
        renderer.pace_config(config, max_parallel_requests=None, replicas=20,
                             account_rpm=None, account_tpm=10)
    with pytest.raises(ValueError, match="at least 1"):
        renderer.pace_config(config, max_parallel_requests=0, replicas=1,
                             account_rpm=None, account_tpm=None)


def test_rendered_configmap_can_be_named_for_the_fleet():
    config = yaml.safe_load(renderer.DEFAULT_CONFIG.read_text(encoding="utf-8"))
    env = {name: "test-only-value" for name in renderer.required_names(config)}

    configmap, _secret, _client = renderer.render_manifests(
        "model_list: []\n", config, env, "resiliencebenchmark-system", "litellm-config-fleet"
    )

    assert configmap["metadata"]["name"] == "litellm-config-fleet"
