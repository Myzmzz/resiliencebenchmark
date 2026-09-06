from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from types import SimpleNamespace

import yaml

from stage2_service.capability_preflight import CAPABILITY_QUALIFICATION_SCHEMA
from stage2_service.contracts import STAGE2_SUPPORTED_MODELS
from stage2_service.gateway_config import GatewayConfigSnapshot
from stage2_service.runtime_factory import RuntimeConfigurationError, Stage2RuntimeConfig, Stage2System


def _route(alias: str) -> dict:
    return {
        "model_name": alias,
        "litellm_params": {
            "model": f"openai/{alias}",
            "api_base": "https://gateway.example/v1",
            "api_key": "os.environ/UPSTREAM_API_KEY",
        },
    }


def _gateway_config(tmp_path: Path) -> Path:
    path = tmp_path / "litellm.yaml"
    path.write_text(
        yaml.safe_dump({"model_list": [_route(alias) for alias in STAGE2_SUPPORTED_MODELS]}),
        encoding="utf-8",
    )
    return path


def _system(tmp_path: Path, snapshot: GatewayConfigSnapshot, prober):
    system = object.__new__(Stage2System)
    system.config = SimpleNamespace(
        repo_root=Path(__file__).resolve().parents[1],
        llm_base_url="http://127.0.0.1:4000/v1",
        llm_api_key="runtime-key",
        gateway_config_file=snapshot.config_path,
        gateway_snapshot=snapshot,
    )
    system.d0_gate = SimpleNamespace(inventory=lambda: {"campaigns": []})
    system._active_lock = Lock()
    system._active_controls = {}
    system._model_probe_runner = prober
    system._probe_cache_ttl_seconds = 300.0
    system._probe_lock = Lock()
    system._probe_cache = {}
    return system


def _qualification_file(tmp_path: Path) -> Path:
    path = tmp_path / "qualification.json"
    harnesses = {}
    for harness in ("codex", "claude-code", "deepseek-harness", "bladeai"):
        harnesses[harness] = {
            "qualification": {
                "status": "passed",
                "evidence_ref": f"artifacts/qualification/{harness}.json",
            },
            "capability": {
                "kind": harness,
                "execution_model": "stream",
                "streams_tool_results": True,
                "post_hoc_trace": False,
                "supports_resume": True,
                "supports_mid_turn_feedback": True,
                "feedback_channels": ["in_band_mcp"],
                "code_execution": "platform_sandbox",
            },
        }
    path.write_text(
        json.dumps(
            {
                "schema_version": CAPABILITY_QUALIFICATION_SCHEMA,
                "harnesses": harnesses,
            }
        ),
        encoding="utf-8",
    )
    return path


def _probe_report(status_by_alias: dict[str, str] | None = None) -> dict:
    status_by_alias = status_by_alias or {}
    return {
        "schemaVersion": "resiliencebenchmark.model_probe/v1",
        "issues": [],
        "models": [
            {
                "alias": alias,
                "overallStatus": status_by_alias.get(alias, "supported"),
                "probes": [{"check": "openai_chat_completions_basic", "status": "supported"}],
            }
            for alias in STAGE2_SUPPORTED_MODELS
        ],
    }


def test_preflight_records_gateway_routes_and_caches_probe_results(tmp_path: Path, monkeypatch):
    snapshot = GatewayConfigSnapshot.from_file(
        _gateway_config(tmp_path),
        required_aliases=STAGE2_SUPPORTED_MODELS,
    )
    calls: list[tuple[str, ...]] = []

    def prober(snapshot_arg, aliases):
        assert snapshot_arg is snapshot
        calls.append(tuple(aliases))
        return _probe_report()

    system = _system(tmp_path, snapshot, prober)
    system._gateway_models = lambda: (set(STAGE2_SUPPORTED_MODELS), None)
    monkeypatch.setenv("STAGE2_HARNESS_CAPABILITIES_FILE", str(_qualification_file(tmp_path)))

    first = system.preflight()
    second = system.preflight()

    assert first["gateway_config"]["config_sha256"] == snapshot.config_sha256
    assert first["gateway_config"]["routes"]["gpt-5.5"]["api_base_host"] == "gateway.example"
    assert first["model_probes"]["gpt-5.5"]["route"]["credential_env_ref"] == "UPSTREAM_API_KEY"
    assert first["model_probes"]["gpt-5.5"]["runnable"] is True
    assert all(first["model_matrix"][harness][model] for harness in first["model_matrix"] for model in STAGE2_SUPPORTED_MODELS)
    assert second["model_probes"]["gpt-5.5"]["runnable"] is True
    assert calls == [tuple(STAGE2_SUPPORTED_MODELS)]


def test_preflight_requires_models_visibility_and_successful_probe(tmp_path: Path, monkeypatch):
    snapshot = GatewayConfigSnapshot.from_file(
        _gateway_config(tmp_path),
        required_aliases=STAGE2_SUPPORTED_MODELS,
    )
    system = _system(
        tmp_path,
        snapshot,
        lambda _snapshot, _aliases: _probe_report({"qwen3.8-max": "probed_with_unsupported_capabilities"}),
    )
    visible = set(STAGE2_SUPPORTED_MODELS) - {"deepseek-v4-flash-0731"}
    system._gateway_models = lambda: (visible, None)
    monkeypatch.setenv("STAGE2_HARNESS_CAPABILITIES_FILE", str(_qualification_file(tmp_path)))

    result = system.preflight()

    assert result["model_probes"]["qwen3.8-max"]["visible_in_gateway_models"] is True
    assert result["model_probes"]["qwen3.8-max"]["probe_status"] == "probed_with_unsupported_capabilities"
    assert result["model_probes"]["qwen3.8-max"]["runnable"] is False
    assert result["model_probes"]["deepseek-v4-flash-0731"]["visible_in_gateway_models"] is False
    assert result["model_probes"]["deepseek-v4-flash-0731"]["probe_status"] == "supported"
    assert result["model_probes"]["deepseek-v4-flash-0731"]["runnable"] is False
    assert result["status"] == "READY"
    assert all(result["harnesses"].values())
    assert all(
        not row["qwen3.8-max"] and not row["deepseek-v4-flash-0731"]
        for row in result["model_matrix"].values()
    )
    assert all(
        row["gpt-5.5"]
        for row in result["model_matrix"].values()
    )


def test_model_probe_statuses_require_real_gateway_snapshot_even_without_config_attr(tmp_path: Path):
    system = object.__new__(Stage2System)
    system.config = SimpleNamespace(
        repo_root=Path(__file__).resolve().parents[1],
        llm_base_url="http://127.0.0.1:4000/v1",
        llm_api_key="runtime-key",
    )
    system.d0_gate = SimpleNamespace(inventory=lambda: {"campaigns": []})
    system._active_lock = Lock()
    system._active_controls = {}
    system._model_probe_runner = lambda _snapshot, _aliases: _probe_report()
    system._probe_cache_ttl_seconds = 300.0
    system._probe_lock = Lock()
    system._probe_cache = {}

    result = system._model_probe_statuses(
        snapshot=None,
        available_models=set(STAGE2_SUPPORTED_MODELS),
        probe_report=_probe_report(),
    )

    assert all(not row["runnable"] for row in result.values())
    assert all(row["visible_in_gateway_models"] for row in result.values())
    assert all(row["probe_status"] == "supported" for row in result.values())


def test_runtime_config_from_env_requires_real_gateway_config_file(tmp_path: Path):
    env = {
        "STAGE2_REPO_ROOT": str(tmp_path),
        "STAGE2_PRIVATE_ROOT": str(tmp_path / "private"),
        "STAGE2_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
        "STAGE2_RUNTIME_ENV_FILE": str(tmp_path / "otel-demo.env"),
        "STAGE2_SOURCE_ROOT": str(tmp_path / "sources"),
        "STAGE2_OTEL_CHART_FILE": str(tmp_path / "otel-demo.tgz"),
        "STAGE2_KUBECONFIG": str(tmp_path / "service.kubeconfig"),
        "STAGE2_POD_NAME": "controller",
        "STAGE2_POD_UID": "controller-uid",
        "STAGE2_POD_NAMESPACE": "resiliencebenchmark-system",
        "RESBENCH_LLM_BASE_URL": "http://127.0.0.1:4000/v1",
        "RESBENCH_LLM_API_KEY": "runtime-key",
        "STAGE2_LITELLM_CONFIG_FILE": str(tmp_path / "missing.yaml"),
    }

    try:
        Stage2RuntimeConfig.from_env(env)
    except RuntimeConfigurationError as exc:
        assert "LiteLLM gateway config is not readable" in str(exc)
    else:  # pragma: no cover - assertion branch.
        raise AssertionError("missing runtime LiteLLM config must fail closed")
