from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from stage2_service.contracts import STAGE2_SUPPORTED_MODELS
from stage2_service.gateway_config import GatewayConfigError, GatewayConfigSnapshot


def _route(alias: str, *, api_base: str = "https://gateway.example/v1") -> dict:
    return {
        "model_name": alias,
        "litellm_params": {
            "model": f"openai/{alias}",
            "api_base": api_base,
            "api_key": "os.environ/UPSTREAM_API_KEY",
        },
    }


def _write_config(path: Path, routes: list[dict], **extra: object) -> Path:
    payload = {"model_list": routes, **extra}
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _valid_config(path: Path) -> Path:
    return _write_config(
        path,
        [_route(alias) for alias in STAGE2_SUPPORTED_MODELS]
        + [_route("gpt-5.5-nexustokenai", api_base="https://alternate.example/v1")],
    )


def test_snapshot_parses_required_routes_and_keeps_alternates_out_of_required_axis(tmp_path: Path):
    config = _valid_config(tmp_path / "config.yaml")

    snapshot = GatewayConfigSnapshot.from_file(config, required_aliases=STAGE2_SUPPORTED_MODELS)

    assert len(snapshot.config_sha256) == 64
    assert set(snapshot.required_routes()) == set(STAGE2_SUPPORTED_MODELS)
    assert "gpt-5.5-nexustokenai" not in snapshot.required_routes()
    assert snapshot.route("gpt-5.5") == {
        "model_alias": "gpt-5.5",
        "provider": "openai",
        "upstream_model": "gpt-5.5",
        "api_base_host": "gateway.example",
        "api_base_scheme": "https",
        "api_base_path": "/v1",
        "credential_env_ref": "UPSTREAM_API_KEY",
    }
    assert snapshot.route("gpt-5.5-nexustokenai")["api_base_host"] == "alternate.example"
    with pytest.raises(TypeError):
        snapshot._routes["extra"] = snapshot._routes["gpt-5.5"]
    public_route = snapshot.route("gpt-5.5")
    public_route["provider"] = "changed"
    assert snapshot.route("gpt-5.5")["provider"] == "openai"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda routes: routes[:-1],
            "missing required alias",
        ),
        (
            lambda routes: [*routes, _route(STAGE2_SUPPORTED_MODELS[0])],
            "duplicate LiteLLM model route",
        ),
    ],
)
def test_snapshot_rejects_missing_and_duplicate_required_aliases(tmp_path: Path, mutate, message: str):
    routes = [_route(alias) for alias in STAGE2_SUPPORTED_MODELS]
    config = _write_config(tmp_path / "config.yaml", mutate(routes))

    with pytest.raises(GatewayConfigError, match=message):
        GatewayConfigSnapshot.from_file(config, required_aliases=STAGE2_SUPPORTED_MODELS)


def test_snapshot_rejects_active_router_settings(tmp_path: Path):
    config = _write_config(
        tmp_path / "config.yaml",
        [_route(alias) for alias in STAGE2_SUPPORTED_MODELS],
        router_settings={"fallbacks": [{"gpt-5.5": ["gpt-5.5-nexustokenai"]}]},
    )

    with pytest.raises(GatewayConfigError, match="router_settings"):
        GatewayConfigSnapshot.from_file(config, required_aliases=STAGE2_SUPPORTED_MODELS)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("api_key", "sk-inline-secret", "os.environ/NAME"),
        ("api_key", "os.environ/bad-name", "env reference is invalid"),
        ("api_base", "https://user:pass@gateway.example/v1", "userinfo"),
        ("api_base", "https://gateway.example/v1?route=other", "query"),
        ("api_base", "not-a-url", "explicit http\\(s\\) URL"),
        ("model", "gpt-5.5", "provider/model"),
    ],
)
def test_snapshot_rejects_unsafe_route_fields(tmp_path: Path, field: str, value: str, message: str):
    routes = [_route(alias) for alias in STAGE2_SUPPORTED_MODELS]
    routes[0]["litellm_params"][field] = value
    config = _write_config(tmp_path / "config.yaml", routes)

    with pytest.raises(GatewayConfigError, match=message):
        GatewayConfigSnapshot.from_file(config, required_aliases=STAGE2_SUPPORTED_MODELS)
