from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load_module(monkeypatch: pytest.MonkeyPatch):
    custom_logger = ModuleType("litellm.integrations.custom_logger")

    class CustomLogger:  # pragma: no cover - test stub only.
        pass

    custom_logger.CustomLogger = CustomLogger
    integrations = ModuleType("litellm.integrations")
    integrations.__path__ = []  # type: ignore[attr-defined]
    integrations.custom_logger = custom_logger
    litellm = ModuleType("litellm")
    litellm.__path__ = []  # type: ignore[attr-defined]
    litellm.integrations = integrations
    monkeypatch.setitem(sys.modules, "litellm", litellm)
    monkeypatch.setitem(sys.modules, "litellm.integrations", integrations)
    monkeypatch.setitem(sys.modules, "litellm.integrations.custom_logger", custom_logger)
    module = importlib.import_module("stage2_service.gateway_audit_callback")
    return importlib.reload(module)


def _data(
    *,
    trial_id: str | None,
    harness: str = "codex",
    model_alias: str = "gpt-5.5",
    model: str = "gpt-5.5",
    request_id: str = "req-1",
    prompt: str = "do not leak prompt",
    api_key: str = "top-level-api-key",
) -> dict[str, object]:
    headers: dict[str, object] = {
        "x-resbench-harness": harness,
        "x-resbench-model-alias": model_alias,
        "x-resbench-request-id": request_id,
        "x-resbench-config-sha256": "wrong-header-value",
    }
    if trial_id is not None:
        headers["x-resbench-trial-id"] = trial_id
    return {
        "proxy_server_request": {
            "headers": headers,
            "body": {"prompt": prompt, "cookie": "cookie-secret"},
            "cookies": {"session": "cookie-secret"},
        },
        "model": model,
        "prompt": "top-level prompt should be ignored",
        "api_key": api_key,
    }


def _read_rows(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("trial_id", ["trial-20260905-01", "qualification-20260905-01"])
@pytest.mark.parametrize("harness", ["codex", "claude-code", "deepseek-harness", "bladeai"])
def test_gateway_audit_callback_writes_valid_row_and_uses_local_config_sha(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    trial_id: str,
    harness: str,
):
    module = _load_module(monkeypatch)
    audit_dir = tmp_path / "audit"
    config = tmp_path / "litellm.yaml"
    config.write_text("model_list:\n  - model_name: gpt-5.5\n", encoding="utf-8")
    monkeypatch.setenv("RESBENCH_GATEWAY_AUDIT_DIR", str(audit_dir))
    monkeypatch.setenv("STAGE2_LITELLM_CONFIG_FILE", str(config))

    asyncio.run(
        module.logger_instance.async_pre_call_hook(
            user_api_key_dict=None,
            cache=None,
            data=_data(trial_id=trial_id, harness=harness),
            call_type="completion",
        )
    )

    rows = _read_rows(audit_dir / f"{trial_id}.jsonl")
    assert len(rows) == 1
    row = rows[0]
    assert row["schema_version"] == "stage2-gateway-request.v1"
    assert row["trial_id"] == trial_id
    assert row["harness"] == harness
    assert row["model_alias"] == "gpt-5.5"
    assert row["request_id"] == "req-1"
    assert row["gateway_config_sha256"] == hashlib.sha256(config.read_bytes()).hexdigest()
    assert row["gateway_config_sha256"] != "wrong-header-value"
    assert row["outcome"] == "received"
    assert row["status"] is None
    assert "prompt" not in row
    assert "api_key" not in row


def test_gateway_audit_callback_rejects_header_alias_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    module = _load_module(monkeypatch)
    audit_dir = tmp_path / "audit"
    config = tmp_path / "litellm.yaml"
    config.write_text("model_list: []\n", encoding="utf-8")
    monkeypatch.setenv("RESBENCH_GATEWAY_AUDIT_DIR", str(audit_dir))
    monkeypatch.setenv("STAGE2_LITELLM_CONFIG_FILE", str(config))

    with pytest.raises(ValueError, match="invalid gateway audit identity"):
        asyncio.run(
            module.logger_instance.async_pre_call_hook(
                user_api_key_dict=None,
                cache=None,
                data=_data(trial_id="trial-20260905-01", model_alias="claude-opus-5", model="gpt-5.5"),
                call_type="completion",
            )
        )


def test_gateway_audit_callback_ignores_missing_trial_id_and_does_not_leak_sensitive_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    module = _load_module(monkeypatch)
    audit_dir = tmp_path / "audit"
    config = tmp_path / "litellm.yaml"
    config.write_text("model_list: []\n", encoding="utf-8")
    monkeypatch.setenv("RESBENCH_GATEWAY_AUDIT_DIR", str(audit_dir))
    monkeypatch.setenv("STAGE2_LITELLM_CONFIG_FILE", str(config))

    result = asyncio.run(
        module.logger_instance.async_pre_call_hook(
            user_api_key_dict=None,
            cache=None,
            data=_data(trial_id=None, request_id="req-ignored"),
            call_type="completion",
        )
    )
    assert result is None
    assert not audit_dir.exists() or not any(audit_dir.iterdir())

    asyncio.run(
        module.logger_instance.async_pre_call_hook(
            user_api_key_dict=None,
            cache=None,
            data=_data(trial_id="trial-20260905-02", request_id="req-2"),
            call_type="completion",
        )
    )
    text = (audit_dir / "trial-20260905-02.jsonl").read_text(encoding="utf-8")
    assert "do not leak prompt" not in text
    assert "cookie-secret" not in text
    assert "top-level-api-key" not in text
    assert "wrong-header-value" not in text


@pytest.mark.parametrize(
    "trial_id",
    ["../escape", "trial/escape", "", "invalid space", "x" * 161],
)
def test_gateway_audit_callback_rejects_invalid_trial_id_and_path_traversal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    trial_id: str,
):
    module = _load_module(monkeypatch)
    audit_dir = tmp_path / "audit"
    config = tmp_path / "litellm.yaml"
    config.write_text("model_list: []\n", encoding="utf-8")
    monkeypatch.setenv("RESBENCH_GATEWAY_AUDIT_DIR", str(audit_dir))
    monkeypatch.setenv("STAGE2_LITELLM_CONFIG_FILE", str(config))

    with pytest.raises(ValueError, match="invalid gateway audit identity"):
        asyncio.run(
            module.logger_instance.async_pre_call_hook(
                user_api_key_dict=None,
                cache=None,
                data=_data(trial_id=trial_id),
                call_type="completion",
            )
        )

    assert not audit_dir.exists() or not any(audit_dir.iterdir())


@pytest.mark.parametrize("field, value", [("harness", "unknown"), ("model_alias", ""), ("request_id", "bad/request")])
def test_gateway_audit_callback_rejects_invalid_metadata_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
    value: str,
):
    module = _load_module(monkeypatch)
    audit_dir = tmp_path / "audit"
    config = tmp_path / "litellm.yaml"
    config.write_text("model_list: []\n", encoding="utf-8")
    monkeypatch.setenv("RESBENCH_GATEWAY_AUDIT_DIR", str(audit_dir))
    monkeypatch.setenv("STAGE2_LITELLM_CONFIG_FILE", str(config))

    kwargs = {"trial_id": "trial-20260905-04"}
    kwargs[field] = value
    with pytest.raises(ValueError, match="invalid gateway audit identity"):
        asyncio.run(
            module.logger_instance.async_pre_call_hook(
                user_api_key_dict=None,
                cache=None,
                data=_data(**kwargs),
                call_type="completion",
            )
        )

    assert not audit_dir.exists() or not any(audit_dir.iterdir())


def test_gateway_audit_callback_rejects_symlink_audit_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    module = _load_module(monkeypatch)
    config = tmp_path / "litellm.yaml"
    config.write_text("model_list: []\n", encoding="utf-8")
    real_dir = tmp_path / "real-audit"
    real_dir.mkdir()
    symlink_dir = tmp_path / "audit-link"
    symlink_dir.symlink_to(real_dir, target_is_directory=True)
    monkeypatch.setenv("RESBENCH_GATEWAY_AUDIT_DIR", str(symlink_dir))
    monkeypatch.setenv("STAGE2_LITELLM_CONFIG_FILE", str(config))

    with pytest.raises(ValueError, match="gateway audit directory must not be a symlink"):
        asyncio.run(
            module.logger_instance.async_pre_call_hook(
                user_api_key_dict=None,
                cache=None,
                data=_data(trial_id="trial-20260905-05"),
                call_type="completion",
            )
        )


def test_gateway_audit_callback_rejects_symlink_config_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    module = _load_module(monkeypatch)
    audit_dir = tmp_path / "audit"
    config = tmp_path / "litellm.yaml"
    config.write_text("model_list: []\n", encoding="utf-8")
    config_link = tmp_path / "litellm-link.yaml"
    config_link.symlink_to(config)
    monkeypatch.setenv("RESBENCH_GATEWAY_AUDIT_DIR", str(audit_dir))
    monkeypatch.setenv("STAGE2_LITELLM_CONFIG_FILE", str(config_link))

    with pytest.raises(OSError):
        asyncio.run(
            module.logger_instance.async_pre_call_hook(
                user_api_key_dict=None,
                cache=None,
                data=_data(trial_id="trial-20260905-06"),
                call_type="completion",
            )
        )


def test_gateway_audit_callback_supports_async_writes_without_corrupting_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    module = _load_module(monkeypatch)
    audit_dir = tmp_path / "audit"
    config = tmp_path / "litellm.yaml"
    config.write_text("model_list: []\n", encoding="utf-8")
    monkeypatch.setenv("RESBENCH_GATEWAY_AUDIT_DIR", str(audit_dir))
    monkeypatch.setenv("STAGE2_LITELLM_CONFIG_FILE", str(config))

    trial_id = "trial-20260905-07"

    async def async_write(index: int) -> None:
        await module.logger_instance.async_pre_call_hook(
            user_api_key_dict=None,
            cache=None,
            data=_data(trial_id=trial_id, request_id=f"req-{index}"),
            call_type="completion",
        )

    async def run_async_writes() -> None:
        await asyncio.gather(*(async_write(i) for i in range(20)))

    asyncio.run(run_async_writes())

    rows = _read_rows(audit_dir / f"{trial_id}.jsonl")
    assert len(rows) == 20
    assert {row["request_id"] for row in rows} == {f"req-{i}" for i in range(20)}
    assert max((len(json.dumps(row, separators=(",", ":")).encode("utf-8")) for row in rows)) <= 4096
    assert (audit_dir / f"{trial_id}.jsonl").stat().st_size <= 1_000_000
