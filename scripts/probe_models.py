#!/usr/bin/env python3
"""Probe LLM gateway capabilities for BenchmarkFactory harnesses.

The probe reads model aliases from ``harness/models.yaml`` and runtime
credentials from environment variables. It prints a redacted JSON report and
never persists endpoints, API keys, or request transcripts.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urljoin, urlparse

import yaml


BASE_URL_ENV = "RESBENCH_LLM_BASE_URL"
API_KEY_ENV = "RESBENCH_LLM_API_KEY"
DEFAULT_MODELS_CONFIG = Path("harness/models.yaml")
MAX_RESPONSE_BYTES = 2_000_000


@dataclass
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    elapsed_ms: int


Transport = Callable[[str, str, Mapping[str, str], Optional[bytes], float], HttpResponse]


class UrllibTransport:
    def __call__(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse:
        start = time.monotonic()
        request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - explicit runtime URL.
                data = response.read(MAX_RESPONSE_BYTES + 1)
                status = int(response.status)
                response_headers = {key.lower(): value for key, value in response.headers.items()}
        except urllib.error.HTTPError as exc:
            data = exc.read(MAX_RESPONSE_BYTES + 1)
            status = int(exc.code)
            response_headers = {key.lower(): value for key, value in exc.headers.items()}
        if len(data) > MAX_RESPONSE_BYTES:
            raise ValueError(f"gateway response exceeded {MAX_RESPONSE_BYTES} bytes")
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return HttpResponse(status=status, headers=response_headers, body=data, elapsed_ms=elapsed_ms)


def load_models_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError("models config top-level document must be a mapping")
    models = data.get("models")
    if not isinstance(models, dict) or not models:
        raise ValueError("models config must define a non-empty models mapping")
    return data


def selected_model_items(config: dict[str, Any], aliases: list[str] | None) -> list[tuple[str, dict[str, Any]]]:
    models = config["models"]
    if aliases:
        missing = [alias for alias in aliases if alias not in models]
        if missing:
            raise ValueError(f"unknown model alias(es): {', '.join(missing)}")
        return [(alias, models[alias]) for alias in aliases]
    return [(alias, spec) for alias, spec in models.items()]


def redacted_env_source(name: str, value: str | None) -> dict[str, Any]:
    return {"source": f"env:{name}", "present": bool(value)}


def join_endpoint(base_url: str, path: str) -> str:
    return urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))


def validate_gateway_base_url(base_url: str) -> None:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{BASE_URL_ENV} must be an explicit http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f"{BASE_URL_ENV} must not contain userinfo, a query, or a fragment")
    if parsed.scheme == "http":
        is_loopback = parsed.hostname.lower() == "localhost"
        if not is_loopback:
            try:
                is_loopback = ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:
                is_loopback = False
        if not is_loopback:
            raise ValueError(f"{BASE_URL_ENV} must use HTTPS unless it targets loopback")


def transport_error_message(exc: Exception) -> str:
    return f"gateway request failed ({type(exc).__name__}); verify runtime connectivity and protocol support"


def json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def decode_json(response: HttpResponse) -> tuple[Any | None, str | None]:
    try:
        text = response.body.decode("utf-8")
    except UnicodeDecodeError as exc:
        return None, f"response body is not UTF-8: {exc}"
    if not text.strip():
        return None, "response body is empty"
    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        return None, f"response body is not JSON: {exc}"


def probe_status_for_http(status: int) -> str:
    if 200 <= status < 300:
        return "supported"
    if status in {400, 404, 405, 422}:
        return "unsupported"
    return "failed"


def extract_model_ids(data: Any) -> list[str]:
    if isinstance(data, dict):
        candidates = data.get("data")
        if candidates is None:
            candidates = data.get("models")
    elif isinstance(data, list):
        candidates = data
    else:
        candidates = None
    if not isinstance(candidates, list):
        return []
    ids: list[str] = []
    for item in candidates:
        if isinstance(item, str):
            ids.append(item)
        elif isinstance(item, dict):
            model_id = item.get("id") or item.get("name") or item.get("model")
            if isinstance(model_id, str):
                ids.append(model_id)
    return ids


def extract_message_content(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts) if parts else None
    return None


def extract_tool_calls(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return []
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return []
    tool_calls = message.get("tool_calls")
    return tool_calls if isinstance(tool_calls, list) else []


def decode_sse_chunks(body: bytes) -> list[Any]:
    text = body.decode("utf-8", errors="replace")
    chunks: list[Any] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            chunks.append(json.loads(data))
        except json.JSONDecodeError:
            chunks.append(data)
    return chunks


def base_headers(api_key: str) -> dict[str, str]:
    return {
        "authorization": f"Bearer {api_key}",
        "content-type": "application/json",
        "accept": "application/json",
    }


def request_json(
    transport: Transport,
    method: str,
    url: str,
    headers: Mapping[str, str],
    payload: dict[str, Any] | None,
    timeout: float,
) -> HttpResponse:
    body = json_bytes(payload) if payload is not None else None
    return transport(method, url, headers, body, timeout)


def model_alias_probe(
    transport: Transport,
    base_url: str,
    api_key: str,
    alias: str,
    upstream_model: str,
    timeout: float,
) -> dict[str, Any]:
    url = join_endpoint(base_url, "/models")
    try:
        response = request_json(transport, "GET", url, base_headers(api_key), None, timeout)
    except Exception as exc:  # noqa: BLE001 - transport is injected by tests and varies in production.
        return {
            "check": "model_alias_resolution",
            "protocol": "openai_compatible",
            "status": "failed",
            "endpoint": "/models",
            "errorType": type(exc).__name__,
            "message": transport_error_message(exc),
        }
    data, error = decode_json(response)
    ids = extract_model_ids(data) if error is None else []
    resolved = alias in ids or upstream_model in ids
    status = probe_status_for_http(response.status)
    if status == "supported" and not resolved:
        status = "unsupported"
    return {
        "check": "model_alias_resolution",
        "protocol": "openai_compatible",
        "status": status,
        "endpoint": "/models",
        "httpStatus": response.status,
        "latencyMs": response.elapsed_ms,
        "resolved": resolved,
        "matchedModel": upstream_model if upstream_model in ids else alias if alias in ids else None,
        "modelCount": len(ids),
        "message": error if error else None,
    }


def openai_chat_probe(
    transport: Transport,
    base_url: str,
    api_key: str,
    model: str,
    check: str,
    payload: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    url = join_endpoint(base_url, "/chat/completions")
    try:
        response = request_json(transport, "POST", url, base_headers(api_key), payload, timeout)
    except Exception as exc:  # noqa: BLE001
        return {
            "check": check,
            "protocol": "openai_chat_completions",
            "status": "failed",
            "endpoint": "/chat/completions",
            "errorType": type(exc).__name__,
            "message": transport_error_message(exc),
        }
    status = probe_status_for_http(response.status)
    data, error = decode_json(response)
    content = extract_message_content(data)
    message = error
    detail: dict[str, Any] = {}

    if check == "openai_chat_completions_basic":
        if status == "supported" and not content and not extract_tool_calls(data):
            status = "failed"
            message = "response did not contain assistant content or tool calls"
        detail["contentPreview"] = (content or "")[:80] if content else None
    elif check == "structured_json_output":
        parsed = None
        if content:
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError as exc:
                message = f"assistant content is not JSON: {exc}"
        if status == "supported" and not isinstance(parsed, dict):
            status = "failed"
        detail["validJsonObject"] = isinstance(parsed, dict)
    elif check in {"single_tool_call", "parallel_tool_calls"}:
        tool_calls = extract_tool_calls(data)
        minimum = 2 if check == "parallel_tool_calls" else 1
        if status == "supported" and len(tool_calls) < minimum:
            status = "unsupported"
            message = f"expected at least {minimum} tool call(s), observed {len(tool_calls)}"
        detail["toolCallCount"] = len(tool_calls)
    return {
        "check": check,
        "protocol": "openai_chat_completions",
        "status": status,
        "endpoint": "/chat/completions",
        "httpStatus": response.status,
        "latencyMs": response.elapsed_ms,
        "providerReportedModel": data.get("model") if isinstance(data, dict) else None,
        "message": message,
        **detail,
    }


def openai_stream_probe(
    transport: Transport,
    base_url: str,
    api_key: str,
    model: str,
    timeout: float,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with the word ok."}],
        "temperature": 0,
        "stream": True,
    }
    headers = {**base_headers(api_key), "accept": "text/event-stream"}
    url = join_endpoint(base_url, "/chat/completions")
    try:
        response = request_json(transport, "POST", url, headers, payload, timeout)
    except Exception as exc:  # noqa: BLE001
        return {
            "check": "streaming",
            "protocol": "openai_chat_completions",
            "status": "failed",
            "endpoint": "/chat/completions",
            "errorType": type(exc).__name__,
            "message": transport_error_message(exc),
        }
    status = probe_status_for_http(response.status)
    chunks = decode_sse_chunks(response.body) if 200 <= response.status < 300 else []
    if status == "supported" and not chunks:
        status = "unsupported"
    return {
        "check": "streaming",
        "protocol": "openai_chat_completions",
        "status": status,
        "endpoint": "/chat/completions",
        "httpStatus": response.status,
        "latencyMs": response.elapsed_ms,
        "eventCount": len(chunks),
        "message": None if chunks else "no parseable server-sent event chunks observed",
    }


def anthropic_messages_probe(
    transport: Transport,
    base_url: str,
    api_key: str,
    model: str,
    timeout: float,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "Reply with ok."}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
        "accept": "application/json",
    }
    url = join_endpoint(base_url, "/messages")
    try:
        response = request_json(transport, "POST", url, headers, payload, timeout)
    except Exception as exc:  # noqa: BLE001
        return {
            "check": "anthropic_messages_basic",
            "protocol": "anthropic_messages",
            "status": "failed",
            "modelFailureImpact": "protocol_only",
            "endpoint": "/messages",
            "errorType": type(exc).__name__,
            "message": transport_error_message(exc),
        }
    status = probe_status_for_http(response.status)
    data, error = decode_json(response)
    return {
        "check": "anthropic_messages_basic",
        "protocol": "anthropic_messages",
        "status": status,
        "modelFailureImpact": "protocol_only",
        "endpoint": "/messages",
        "httpStatus": response.status,
        "latencyMs": response.elapsed_ms,
        "providerReportedModel": data.get("model") if isinstance(data, dict) else None,
        "message": error,
    }


def basic_payload(model: str) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with exactly: ok"}],
        "temperature": 0,
    }


def structured_json_payload(model: str) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": 'Return only this JSON object: {"probe":"ok","supported":true}',
            }
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }


def tool_payload(model: str, parallel: bool) -> dict[str, Any]:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "record_probe_signal",
                "description": "Record a harmless benchmark probe signal.",
                "parameters": {
                    "type": "object",
                    "properties": {"signal": {"type": "string"}},
                    "required": ["signal"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "record_probe_second_signal",
                "description": "Record a second harmless benchmark probe signal.",
                "parameters": {
                    "type": "object",
                    "properties": {"signal": {"type": "string"}},
                    "required": ["signal"],
                    "additionalProperties": False,
                },
            },
        },
    ]
    prompt = "Call record_probe_signal once with signal='ok'."
    selected_tools = tools[:1]
    if parallel:
        prompt = "Call both provided tools once. Do not answer in text."
        selected_tools = tools
    return {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "tools": selected_tools,
        "tool_choice": "auto",
        "parallel_tool_calls": parallel,
    }


def planned_probe(alias: str, spec: Mapping[str, Any]) -> dict[str, Any]:
    upstream = str(spec.get("upstream_model") or alias)
    candidates = spec.get("protocol_candidates") or []
    checks = [
        "model_alias_resolution",
        "openai_chat_completions_basic",
        "streaming",
        "single_tool_call",
        "parallel_tool_calls",
        "structured_json_output",
    ]
    if "anthropic_messages" in candidates:
        checks.append("anthropic_messages_basic")
    return {
        "alias": alias,
        "upstreamModel": upstream,
        "displayName": spec.get("display_name"),
        "protocolCandidates": candidates,
        "overallStatus": "planned",
        "probes": [
            {"check": check, "status": "planned", "message": "dry-run: no network request sent"}
            for check in checks
        ],
    }


def probe_model(
    transport: Transport,
    base_url: str,
    api_key: str,
    alias: str,
    spec: Mapping[str, Any],
    timeout: float,
) -> dict[str, Any]:
    upstream = str(spec.get("upstream_model") or alias)
    candidates = list(spec.get("protocol_candidates") or [])
    probes: list[dict[str, Any]] = [
        model_alias_probe(transport, base_url, api_key, alias, upstream, timeout),
        openai_chat_probe(transport, base_url, api_key, upstream, "openai_chat_completions_basic", basic_payload(upstream), timeout),
        openai_stream_probe(transport, base_url, api_key, upstream, timeout),
        openai_chat_probe(transport, base_url, api_key, upstream, "single_tool_call", tool_payload(upstream, False), timeout),
        openai_chat_probe(transport, base_url, api_key, upstream, "parallel_tool_calls", tool_payload(upstream, True), timeout),
        openai_chat_probe(
            transport,
            base_url,
            api_key,
            upstream,
            "structured_json_output",
            structured_json_payload(upstream),
            timeout,
        ),
    ]
    if "anthropic_messages" in candidates:
        probes.append(anthropic_messages_probe(transport, base_url, api_key, upstream, timeout))

    capabilities = {
        "aliasResolved": probe_value(probes, "model_alias_resolution", "resolved", default=False),
        "openaiChatCompletions": probe_supported(probes, "openai_chat_completions_basic"),
        "streaming": probe_supported(probes, "streaming"),
        "singleToolCall": probe_supported(probes, "single_tool_call"),
        "parallelToolCalls": probe_supported(probes, "parallel_tool_calls"),
        "structuredJsonOutput": probe_supported(probes, "structured_json_output"),
        "anthropicMessages": probe_supported(probes, "anthropic_messages_basic")
        if "anthropic_messages" in candidates
        else None,
    }
    primary_checks = [
        "model_alias_resolution",
        "openai_chat_completions_basic",
        "streaming",
        "single_tool_call",
        "parallel_tool_calls",
        "structured_json_output",
    ]
    if any(probe["status"] == "failed" for probe in probes if probe["check"] in primary_checks):
        overall = "probed_with_failures"
    elif any(probe["status"] == "unsupported" for probe in probes if probe["check"] in primary_checks):
        overall = "probed_with_unsupported_capabilities"
    else:
        overall = "supported"
    return {
        "alias": alias,
        "upstreamModel": upstream,
        "displayName": spec.get("display_name"),
        "protocolCandidates": candidates,
        "overallStatus": overall,
        "capabilities": capabilities,
        "probes": probes,
    }


def probe_supported(probes: list[dict[str, Any]], check: str) -> bool:
    return any(probe["check"] == check and probe["status"] == "supported" for probe in probes)


def probe_value(probes: list[dict[str, Any]], check: str, key: str, default: Any = None) -> Any:
    for probe in probes:
        if probe["check"] == check:
            return probe.get(key, default)
    return default


def run_probe(
    models_config: Path,
    env: Mapping[str, str],
    aliases: list[str] | None = None,
    dry_run: bool = False,
    timeout: float | None = None,
    transport: Transport | None = None,
) -> dict[str, Any]:
    config = load_models_config(models_config)
    defaults = config.get("defaults") if isinstance(config.get("defaults"), dict) else {}
    timeout_seconds = timeout or float(
        ((defaults.get("capability_probe") or {}) if isinstance(defaults.get("capability_probe"), dict) else {}).get(
            "timeout_seconds",
            120,
        )
    )
    items = selected_model_items(config, aliases)
    base_url = env.get(BASE_URL_ENV)
    api_key = env.get(API_KEY_ENV)
    report: dict[str, Any] = {
        "schemaVersion": "resiliencebenchmark.model_probe/v1",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "dryRun": dry_run,
        "modelsConfig": models_config.as_posix(),
        "credentialSources": {
            "baseUrl": redacted_env_source(BASE_URL_ENV, base_url),
            "apiKey": redacted_env_source(API_KEY_ENV, api_key),
        },
        "models": [],
        "issues": [],
    }
    if dry_run:
        report["models"] = [planned_probe(alias, spec) for alias, spec in items]
        return report
    if not base_url:
        report["issues"].append({"severity": "ERROR", "message": f"{BASE_URL_ENV} is required"})
        return report
    if not api_key:
        report["issues"].append({"severity": "ERROR", "message": f"{API_KEY_ENV} is required"})
        return report
    try:
        validate_gateway_base_url(base_url)
    except ValueError as exc:
        report["issues"].append({"severity": "ERROR", "message": str(exc)})
        return report
    if not 0.5 <= timeout_seconds <= 300:
        report["issues"].append(
            {"severity": "ERROR", "message": "probe timeout must be between 0.5 and 300 seconds"}
        )
        return report

    active_transport = transport or UrllibTransport()
    report["models"] = [
        probe_model(active_transport, base_url, api_key, alias, spec, timeout_seconds)
        for alias, spec in items
    ]
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Probe BenchmarkFactory LLM gateway capabilities")
    parser.add_argument(
        "--models-config",
        default=DEFAULT_MODELS_CONFIG.as_posix(),
        help="path to harness/models.yaml, defaults to harness/models.yaml",
    )
    parser.add_argument("--model", action="append", help="model alias to probe; repeat to select multiple aliases")
    parser.add_argument("--timeout", type=float, help="per-request timeout in seconds")
    parser.add_argument("--dry-run", action="store_true", help="print planned probes without network requests")
    return parser


def main(argv: list[str] | None = None, transport: Transport | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        report = run_probe(
            Path(args.models_config),
            os.environ,
            aliases=args.model,
            dry_run=args.dry_run,
            timeout=args.timeout,
            transport=transport,
        )
    except Exception as exc:  # noqa: BLE001 - CLI should emit structured setup failures.
        print(json.dumps({"schemaVersion": "resiliencebenchmark.model_probe/v1", "issues": [
            {"severity": "ERROR", "message": str(exc), "errorType": type(exc).__name__}
        ]}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2 if any(issue.get("severity") == "ERROR" for issue in report.get("issues", [])) else 0


if __name__ == "__main__":
    raise SystemExit(main())
