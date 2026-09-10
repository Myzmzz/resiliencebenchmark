from __future__ import annotations

import json
import time
from pathlib import Path
from threading import Event, Lock, Thread
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


def _gateway_config(tmp_path: Path, *, name: str = "litellm.yaml", host: str = "gateway.example") -> Path:
    path = tmp_path / name
    path.write_text(
        yaml.safe_dump(
            {
                "model_list": [
                    {
                        **_route(alias),
                        "litellm_params": {
                            **_route(alias)["litellm_params"],
                            "api_base": f"https://{host}/v1",
                        },
                    }
                    for alias in STAGE2_SUPPORTED_MODELS
                ]
            }
        ),
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
    system._gateway_readiness = {}
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


def _probe_error_report() -> dict:
    return {
        "schemaVersion": "resiliencebenchmark.model_probe/v1",
        "issues": [
            {
                "severity": "ERROR",
                "message": "gateway model probe failed",
                "errorType": "HTTPError",
            }
        ],
        "models": [],
    }


def _all_runnable(result: dict) -> bool:
    return all(
        result["model_matrix"][harness][model]
        for harness in result["model_matrix"]
        for model in STAGE2_SUPPORTED_MODELS
    )


def _call_preflight(system: Stage2System, *, timeout: float = 0.5) -> dict:
    result: dict[str, dict] = {}

    def invoke() -> None:
        result["value"] = system.preflight()

    thread = Thread(target=invoke)
    thread.start()
    thread.join(timeout=timeout)
    assert not thread.is_alive(), "preflight() must not wait for gateway readiness refresh"
    return result["value"]


def test_preflight_records_gateway_routes_and_caches_probe_results(tmp_path: Path, monkeypatch):
    snapshot = GatewayConfigSnapshot.from_file(
        _gateway_config(tmp_path),
        required_aliases=STAGE2_SUPPORTED_MODELS,
    )
    calls: list[tuple[str, ...]] = []

    def prober(snapshot_arg, aliases):
        assert snapshot_arg.config_sha256 == snapshot.config_sha256
        calls.append(tuple(aliases))
        return _probe_report()

    system = _system(tmp_path, snapshot, prober)
    system._gateway_models = lambda: (set(STAGE2_SUPPORTED_MODELS), None)
    monkeypatch.setenv("STAGE2_HARNESS_CAPABILITIES_FILE", str(_qualification_file(tmp_path)))

    refreshed = system.refresh_gateway_readiness()
    first = system.preflight()
    second = system.preflight()

    assert refreshed["status"] == "complete"
    assert first["gateway_config"]["config_sha256"] == snapshot.config_sha256
    assert first["gateway_config"]["routes"]["gpt-5.5"]["api_base_host"] == "gateway.example"
    assert first["gateway_probe"]["status"] == "complete"
    assert set(first["gateway_probe"]["available_models"]) == set(STAGE2_SUPPORTED_MODELS)
    assert first["model_probes"]["gpt-5.5"]["route"]["credential_env_ref"] == "UPSTREAM_API_KEY"
    assert first["model_probes"]["gpt-5.5"]["runnable"] is True
    assert _all_runnable(first)
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

    system.refresh_gateway_readiness()
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


def test_catalog_failure_cannot_be_hidden_by_successful_model_probes(tmp_path: Path):
    snapshot = GatewayConfigSnapshot.from_file(
        _gateway_config(tmp_path), required_aliases=STAGE2_SUPPORTED_MODELS
    )
    system = _system(tmp_path, snapshot, lambda *_: _probe_report())
    system._gateway_models = lambda: (set(), "TimeoutError")

    assert system.refresh_gateway_readiness()["status"] == "failed"
    result = system.preflight()
    assert result["gateway_probe"]["model_catalog_error"] == "TimeoutError"
    assert not any(row["runnable"] for row in result["model_probes"].values())


def test_preflight_returns_immediately_while_gateway_catalog_or_probe_is_running(tmp_path: Path, monkeypatch):
    snapshot = GatewayConfigSnapshot.from_file(
        _gateway_config(tmp_path),
        required_aliases=STAGE2_SUPPORTED_MODELS,
    )
    catalog_started = Event()
    catalog_release = Event()
    probe_started = Event()
    probe_release = Event()

    def gateway_models():
        catalog_started.set()
        catalog_release.wait(timeout=5)
        return set(STAGE2_SUPPORTED_MODELS), None

    def prober(_snapshot, _aliases):
        probe_started.set()
        probe_release.wait(timeout=5)
        return _probe_report()

    system = _system(tmp_path, snapshot, prober)
    system._gateway_models = gateway_models
    monkeypatch.setenv("STAGE2_HARNESS_CAPABILITIES_FILE", str(_qualification_file(tmp_path)))

    first = _call_preflight(system)
    assert first["gateway_probe"]["status"] == "running"
    assert first["status"] == "ERROR"
    assert all(row["probe_status"] == "running" for row in first["model_probes"].values())
    assert all("probe_error" not in row for row in first["model_probes"].values())
    assert not _all_runnable(first)
    assert catalog_started.wait(timeout=1)

    catalog_release.set()
    assert probe_started.wait(timeout=1)
    second = _call_preflight(system)
    assert second["gateway_probe"]["status"] == "running"
    assert second["status"] == "ERROR"
    assert not _all_runnable(second)

    probe_release.set()
    refreshed = system.refresh_gateway_readiness()
    assert refreshed["status"] == "complete"
    assert _all_runnable(system.preflight())


def test_concurrent_preflight_starts_only_one_gateway_refresh(tmp_path: Path, monkeypatch):
    snapshot = GatewayConfigSnapshot.from_file(
        _gateway_config(tmp_path),
        required_aliases=STAGE2_SUPPORTED_MODELS,
    )
    started = Event()
    release = Event()
    count = 0
    count_lock = Lock()

    def gateway_models():
        nonlocal count
        with count_lock:
            count += 1
        started.set()
        release.wait(timeout=5)
        return set(STAGE2_SUPPORTED_MODELS), None

    system = _system(tmp_path, snapshot, lambda _snapshot, _aliases: _probe_report())
    system._gateway_models = gateway_models
    monkeypatch.setenv("STAGE2_HARNESS_CAPABILITIES_FILE", str(_qualification_file(tmp_path)))

    results: list[dict] = []
    threads = [Thread(target=lambda: results.append(system.preflight())) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=1)
        assert not thread.is_alive()
    assert started.wait(timeout=1)
    assert count == 1
    assert len(results) == 20
    assert all(result["gateway_probe"]["status"] == "running" for result in results)
    assert all(not _all_runnable(result) for result in results)

    release.set()
    assert system.refresh_gateway_readiness()["status"] == "complete"


def test_expired_success_fails_closed_and_ttl_counts_from_completion(tmp_path: Path, monkeypatch):
    snapshot = GatewayConfigSnapshot.from_file(
        _gateway_config(tmp_path),
        required_aliases=STAGE2_SUPPORTED_MODELS,
    )
    calls = 0

    def prober(_snapshot, _aliases):
        nonlocal calls
        calls += 1
        return _probe_report()

    system = _system(tmp_path, snapshot, prober)
    system._gateway_models = lambda: (set(STAGE2_SUPPORTED_MODELS), None)
    system._probe_cache_ttl_seconds = 300.0
    monkeypatch.setenv("STAGE2_HARNESS_CAPABILITIES_FILE", str(_qualification_file(tmp_path)))

    assert system.refresh_gateway_readiness()["status"] == "complete"
    key = (snapshot.config_sha256, system.config.llm_base_url, tuple(STAGE2_SUPPORTED_MODELS))
    with system._probe_lock:
        entry = system._gateway_readiness[key]
        entry.started_monotonic = time.monotonic() - 10_000
        entry.completed_monotonic = time.monotonic()

    assert _all_runnable(system.preflight())
    assert calls == 1

    with system._probe_lock:
        system._gateway_readiness[key].completed_monotonic = time.monotonic() - 301.0
    result = system.preflight()

    assert result["gateway_probe"]["status"] == "running"
    assert result["status"] == "ERROR"
    assert not _all_runnable(result)
    assert calls == 2


def test_failed_gateway_probe_is_cached_then_recovers_after_ttl(tmp_path: Path, monkeypatch):
    snapshot = GatewayConfigSnapshot.from_file(
        _gateway_config(tmp_path),
        required_aliases=STAGE2_SUPPORTED_MODELS,
    )
    calls = 0
    fail = True

    def prober(_snapshot, _aliases):
        nonlocal calls
        calls += 1
        if fail:
            raise RuntimeError("probe failed at https://gateway.example/v1 with Authorization header")
        return _probe_report()

    system = _system(tmp_path, snapshot, prober)
    system._gateway_models = lambda: (set(STAGE2_SUPPORTED_MODELS), None)
    monkeypatch.setenv("STAGE2_HARNESS_CAPABILITIES_FILE", str(_qualification_file(tmp_path)))

    failed = system.refresh_gateway_readiness()
    assert failed["status"] == "failed"
    assert failed["error"] == "gateway model probe failed"
    assert "gateway.example" not in failed["error"]
    result = system.preflight()
    assert result["gateway_probe"]["status"] == "failed"
    assert result["model_probes"]["gpt-5.5"]["probe_error"] is True
    assert not _all_runnable(result)
    assert calls == 1

    fail = False
    key = (snapshot.config_sha256, system.config.llm_base_url, tuple(STAGE2_SUPPORTED_MODELS))
    with system._probe_lock:
        system._gateway_readiness[key].completed_monotonic = time.monotonic() - 301.0
    running = system.preflight()
    assert running["gateway_probe"]["status"] == "running"
    assert not _all_runnable(running)
    assert system.refresh_gateway_readiness()["status"] == "complete"
    assert _all_runnable(system.preflight())
    assert calls == 2


def test_probe_report_with_error_issue_is_failed_but_single_model_status_is_complete(
    tmp_path: Path,
    monkeypatch,
):
    snapshot = GatewayConfigSnapshot.from_file(
        _gateway_config(tmp_path),
        required_aliases=STAGE2_SUPPORTED_MODELS,
    )
    mode = {"error_report": True}

    def prober(_snapshot, _aliases):
        if mode["error_report"]:
            return _probe_error_report()
        return _probe_report({"qwen3.8-max": "probed_with_unsupported_capabilities"})

    system = _system(tmp_path, snapshot, prober)
    system._gateway_models = lambda: (set(STAGE2_SUPPORTED_MODELS), None)
    monkeypatch.setenv("STAGE2_HARNESS_CAPABILITIES_FILE", str(_qualification_file(tmp_path)))

    failed = system.refresh_gateway_readiness()
    assert failed["status"] == "failed"
    assert system.preflight()["model_probes"]["gpt-5.5"]["probe_error"] is True

    key = (snapshot.config_sha256, system.config.llm_base_url, tuple(STAGE2_SUPPORTED_MODELS))
    with system._probe_lock:
        system._gateway_readiness[key].completed_monotonic = time.monotonic() - 301.0
    mode["error_report"] = False
    assert system.refresh_gateway_readiness()["status"] == "complete"
    result = system.preflight()
    assert result["gateway_probe"]["status"] == "complete"
    assert result["model_probes"]["qwen3.8-max"]["runnable"] is False
    assert "probe_error" not in result["model_probes"]["gpt-5.5"]


def test_thread_start_failure_does_not_leave_gateway_probe_running(
    tmp_path: Path,
    monkeypatch,
):
    snapshot = GatewayConfigSnapshot.from_file(
        _gateway_config(tmp_path),
        required_aliases=STAGE2_SUPPORTED_MODELS,
    )

    class FailingThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("thread creation failed")

    system = _system(tmp_path, snapshot, lambda _snapshot, _aliases: _probe_report())
    monkeypatch.setenv("STAGE2_HARNESS_CAPABILITIES_FILE", str(_qualification_file(tmp_path)))
    monkeypatch.setattr("stage2_service.runtime_factory.Thread", FailingThread)

    result = system.refresh_gateway_readiness()

    assert result["status"] == "failed"
    assert result["error"] == "gateway readiness refresh could not be started"
    assert result["completed_at"] is not None


def test_old_route_refresh_does_not_populate_new_route_snapshot(tmp_path: Path, monkeypatch):
    old_snapshot = GatewayConfigSnapshot.from_file(
        _gateway_config(tmp_path, name="old.yaml", host="old-gateway.example"),
        required_aliases=STAGE2_SUPPORTED_MODELS,
    )
    new_snapshot = GatewayConfigSnapshot.from_file(
        _gateway_config(tmp_path, name="new.yaml", host="new-gateway.example"),
        required_aliases=STAGE2_SUPPORTED_MODELS,
    )
    current = {"snapshot": old_snapshot}
    old_started = Event()
    old_release = Event()
    old_done = Event()
    new_started = Event()
    new_release = Event()

    def prober(snapshot_arg, _aliases):
        if snapshot_arg.config_sha256 == old_snapshot.config_sha256:
            old_started.set()
            old_release.wait(timeout=5)
            old_done.set()
            return _probe_report()
        if snapshot_arg.config_sha256 == new_snapshot.config_sha256:
            new_started.set()
            new_release.wait(timeout=5)
            return _probe_report()
        raise AssertionError("unexpected gateway snapshot")

    system = _system(tmp_path, old_snapshot, prober)
    system._gateway_snapshot = lambda: (current["snapshot"], None)
    system._gateway_models = lambda: (set(STAGE2_SUPPORTED_MODELS), None)
    monkeypatch.setenv("STAGE2_HARNESS_CAPABILITIES_FILE", str(_qualification_file(tmp_path)))

    assert system.preflight()["gateway_probe"]["status"] == "running"
    assert old_started.wait(timeout=1)
    current["snapshot"] = new_snapshot
    new_result = system.preflight()
    assert new_result["gateway_probe"]["status"] == "running"
    assert new_result["gateway_config"]["config_sha256"] == new_snapshot.config_sha256
    assert not _all_runnable(new_result)
    assert new_started.wait(timeout=1)

    old_release.set()
    assert old_done.wait(timeout=1)
    still_new_running = system.preflight()
    assert still_new_running["gateway_config"]["config_sha256"] == new_snapshot.config_sha256
    assert still_new_running["gateway_probe"]["status"] == "running"
    assert not _all_runnable(still_new_running)

    new_release.set()
    assert system.refresh_gateway_readiness()["status"] == "complete"
    assert _all_runnable(system.preflight())


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


def test_model_probe_statuses_expose_provider_quota_reason():
    system = object.__new__(Stage2System)
    report = _probe_report()
    report["models"][0]["overallStatus"] = "probed_with_failures"
    report["models"][0]["failureClasses"] = ["quota_exhausted"]

    result = system._model_probe_statuses(
        snapshot=SimpleNamespace(route=lambda alias: {"model_alias": alias}),
        available_models=set(STAGE2_SUPPORTED_MODELS),
        probe_report=report,
    )

    alias = STAGE2_SUPPORTED_MODELS[0]
    assert result[alias]["runnable"] is False
    assert result[alias]["failure_classes"] == ["quota_exhausted"]
    assert result[alias]["reason"] == "upstream model quota exhausted"


def test_preflight_requires_current_config_file_not_an_old_snapshot(tmp_path: Path):
    snapshot = GatewayConfigSnapshot.from_file(
        _gateway_config(tmp_path), required_aliases=STAGE2_SUPPORTED_MODELS
    )
    calls = []
    system = _system(tmp_path, snapshot, lambda *_: calls.append("probe"))
    system.config.gateway_config_file = None

    result = system.preflight()

    assert result["gateway_probe"]["status"] == "failed"
    assert result["gateway_config"]["config_sha256"] is None
    assert not any(row["runnable"] for row in result["model_probes"].values())
    assert calls == []


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
