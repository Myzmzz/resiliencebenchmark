from __future__ import annotations

import json

import pytest

from scripts import qualify_mcp_endpoints as qualify


TOKEN = "t" * 40


def runtime_env():
    env = {qualify.TOKEN_ENV: TOKEN}
    for index, spec in enumerate(qualify.ENDPOINTS, start=1):
        env[spec.url_env] = f"http://127.0.0.1:{18080 + index}/mcp"
    return env


def test_dry_run_reports_only_presence_and_does_not_call_checker():
    calls = []

    async def checker(*args):
        calls.append(args)
        return {}

    report = qualify.run_qualification(runtime_env(), execute=False, checker=checker)
    encoded = json.dumps(report)

    assert report["status"] == "not_executed"
    assert calls == []
    assert TOKEN not in encoded
    assert "127.0.0.1" not in encoded


def test_execute_aggregates_redacted_success_results():
    calls = []

    async def checker(spec, url, token, timeout):
        calls.append((spec.name, url, token, timeout))
        return {"name": spec.name, "status": "qualified", "toolCount": spec.minimum_tools}

    report = qualify.run_qualification(runtime_env(), execute=True, checker=checker)
    encoded = json.dumps(report)

    assert report["status"] == "qualified"
    assert len(calls) == 4
    assert TOKEN not in encoded
    assert "127.0.0.1" not in encoded


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:18081/mcp",
        "http://example.test:18081/mcp",
        "http://user:pass@127.0.0.1:18081/mcp",
        "http://127.0.0.1:18081/other",
        "http://127.0.0.1/mcp",
        "http://127.0.0.1:18081/mcp?token=x",
    ],
)
def test_loopback_url_validation_rejects_unsafe_values(url):
    with pytest.raises(qualify.QualificationError):
        qualify.validate_loopback_url(url)


def test_token_validation_is_fail_closed():
    for token in ["", "short", " " + TOKEN, "t" * 20 + "\n" + "t" * 20]:
        with pytest.raises(qualify.QualificationError):
            qualify.validate_token(token)


def test_execute_failure_does_not_echo_endpoint_or_exception():
    async def checker(spec, url, token, timeout):
        raise RuntimeError(f"failed {url} Bearer {token}")

    report = qualify.run_qualification(runtime_env(), execute=True, checker=checker)
    encoded = json.dumps(report)

    assert report["status"] == "failed"
    assert TOKEN not in encoded
    assert "127.0.0.1" not in encoded
    assert "Bearer" not in encoded


def test_invalid_timeout_blocks_before_checker():
    with pytest.raises(qualify.QualificationError):
        qualify.run_qualification(runtime_env(), execute=True, timeout=61)
