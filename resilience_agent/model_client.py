"""Responses API client for our internal reasoning model.

The implementation intentionally depends only on the standard library.  It
supports stateless multi-turn tool loops by replaying the model's output items
and corresponding ``function_call_output`` items on the next request.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .common import load_document


class ModelClientError(RuntimeError):
    """Raised when model configuration, transport, or response parsing fails."""


class ModelTransportError(ModelClientError):
    """Retryable network or socket failure before a valid model response."""


RETRYABLE_HTTP_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524})


@dataclass(frozen=True)
class ModelConfig:
    provider: str
    protocol: str
    base_url: str
    credential: str = field(repr=False)
    base_url_source: str = "runtime_environment"
    credential_source: str = "runtime_environment"
    model: str = "gpt-5.5"
    reasoning_effort: str = "xhigh"
    text_verbosity: str = "low"
    store: bool = False
    timeout_seconds: int = 180
    max_transport_retries: int = 1
    max_output_tokens: int = 12000
    max_tool_rounds: int = 12
    max_tool_result_chars: int = 12000
    parallel_tool_calls: bool = True

    @property
    def endpoint(self) -> str:
        return f"{self.base_url.rstrip('/')}/responses"

    def public_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "protocol": self.protocol,
            "base_url_source": self.base_url_source,
            "credential_source": self.credential_source,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "text_verbosity": self.text_verbosity,
            "store": self.store,
            "timeout_seconds": self.timeout_seconds,
            "max_transport_retries": self.max_transport_retries,
            "max_output_tokens": self.max_output_tokens,
            "max_tool_rounds": self.max_tool_rounds,
            "parallel_tool_calls": self.parallel_tool_calls,
        }


def load_model_config(
    path: Path,
    *,
    environ: Mapping[str, str] | None = None,
    keychain_reader: Callable[[str, str], str | None] | None = None,
) -> ModelConfig:
    data = load_document(path)
    env = environ if environ is not None else os.environ
    if data.get("schema_version") != "resilience-agent-model.v1":
        raise ModelClientError(f"unsupported model config schema: {data.get('schema_version')}")
    if data.get("protocol") != "responses":
        raise ModelClientError("only the Responses API protocol is supported")
    base_env = str(data["base_url_env"])
    key_env = str(data["api_key_env"])
    model_env = str(data["model_env"])
    base_url = env.get(base_env, "").strip()
    base_url_source = f"env:{base_env}"
    if not base_url:
        base_url = str(data.get("base_url_default", "")).strip()
        base_url_source = "config:base_url_default"
    credential = env.get(key_env, "").strip()
    credential_source = f"env:{key_env}"
    keychain_service = str(data.get("api_key_keychain_service", "")).strip()
    if not credential and keychain_service:
        reader = keychain_reader or _read_macos_keychain
        account = env.get("USER", os.environ.get("USER", ""))
        credential = (reader(keychain_service, account) or "").strip()
        credential_source = f"keychain:{keychain_service}"
    model = env.get(model_env, "").strip() or str(data["model_default"])
    if not base_url:
        raise ModelClientError(f"missing runtime model configuration: {base_env}")
    if not credential:
        alternatives = key_env
        if keychain_service:
            alternatives += f" or macOS Keychain service '{keychain_service}'"
        raise ModelClientError(f"missing runtime model credential: {alternatives}")
    if not base_url.startswith(("https://", "http://")):
        raise ModelClientError("model base URL must use http:// or https://")
    return ModelConfig(
        provider=str(data["provider"]),
        protocol=str(data["protocol"]),
        base_url=base_url,
        credential=credential,
        base_url_source=base_url_source,
        credential_source=credential_source,
        model=model,
        reasoning_effort=str(data.get("reasoning_effort", "xhigh")),
        text_verbosity=str(data.get("text_verbosity", "low")),
        store=bool(data.get("store", False)),
        timeout_seconds=int(data.get("timeout_seconds", 180)),
        max_transport_retries=int(data.get("max_transport_retries", 1)),
        max_output_tokens=int(data.get("max_output_tokens", 12000)),
        max_tool_rounds=int(data.get("max_tool_rounds", 12)),
        max_tool_result_chars=int(data.get("max_tool_result_chars", 12000)),
        parallel_tool_calls=bool(data.get("parallel_tool_calls", True)),
    )


def _read_macos_keychain(service: str, account: str) -> str | None:
    if not service or not account:
        return None
    try:
        proc = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-a",
                account,
                "-s",
                service,
                "-w",
            ],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ModelTurn:
    response_id: str
    resolved_model: str
    status: str
    output_items: list[dict[str, Any]]
    tool_calls: list[ToolCall]
    final_text: str | None
    usage: dict[str, int]


class ReasoningModel(Protocol):
    config: ModelConfig

    def create_turn(
        self,
        *,
        instructions: str,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        output_schema: dict[str, Any],
        schema_name: str,
    ) -> ModelTurn: ...


HttpPost = Callable[[str, dict[str, str], bytes, int], dict[str, Any]]


def _default_http_post(
    url: str,
    headers: dict[str, str],
    body: bytes,
    timeout: int,
) -> dict[str, Any]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:2000]
        error = f"model HTTP {exc.code}: {detail}"
        if exc.code in RETRYABLE_HTTP_STATUS:
            raise ModelTransportError(error) from exc
        raise ModelClientError(error) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ModelTransportError(f"model transport failed: {exc}") from exc
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ModelClientError("model response is not valid JSON") from exc


class ResponsesModelClient:
    """Minimal OpenAI-compatible Responses API client."""

    def __init__(self, config: ModelConfig, *, http_post: HttpPost | None = None):
        self.config = config
        self._http_post = http_post or _default_http_post

    def create_turn(
        self,
        *,
        instructions: str,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        output_schema: dict[str, Any],
        schema_name: str,
    ) -> ModelTurn:
        request_body = {
            "model": self.config.model,
            "instructions": instructions,
            "input": input_items,
            "tools": tools,
            "tool_choice": "auto",
            "parallel_tool_calls": self.config.parallel_tool_calls,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "schema": output_schema,
                    "strict": True,
                },
                "verbosity": self.config.text_verbosity,
            },
            "reasoning": {"effort": self.config.reasoning_effort},
            "store": self.config.store,
            "max_output_tokens": self.config.max_output_tokens,
        }
        payload = self._http_post(
            self.config.endpoint,
            {
                "Authorization": f"Bearer {self.config.credential}",
                "Content-Type": "application/json",
                "User-Agent": "openai-python/1.0.0 resilience-agent/0.1",
            },
            json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
            self.config.timeout_seconds,
        )
        return self._parse_turn(payload)

    @staticmethod
    def _parse_turn(payload: dict[str, Any]) -> ModelTurn:
        if payload.get("error"):
            raise ModelClientError(f"model returned an error: {payload['error']}")
        status = str(payload.get("status", "unknown"))
        if status != "completed":
            raise ModelClientError(f"model response did not complete: status={status}")
        output = payload.get("output")
        if not isinstance(output, list):
            raise ModelClientError("model response has no output item list")
        tool_calls: list[ToolCall] = []
        text_parts: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "function_call":
                try:
                    arguments = json.loads(item.get("arguments") or "{}")
                except json.JSONDecodeError as exc:
                    raise ModelClientError(
                        f"tool call {item.get('name')} has invalid JSON arguments"
                    ) from exc
                if not isinstance(arguments, dict):
                    raise ModelClientError("tool call arguments must be an object")
                call_id = str(item.get("call_id") or item.get("id") or "")
                name = str(item.get("name") or "")
                if not call_id or not name:
                    raise ModelClientError("function call is missing call_id or name")
                tool_calls.append(
                    ToolCall(
                        call_id=call_id,
                        name=name,
                        arguments=arguments,
                    )
                )
            if item.get("type") == "message":
                for content in item.get("content", []):
                    if isinstance(content, dict) and content.get("type") == "output_text":
                        text_parts.append(str(content.get("text", "")))
        usage_raw = payload.get("usage") or {}
        usage = {
            "input_tokens": int(usage_raw.get("input_tokens", 0) or 0),
            "output_tokens": int(usage_raw.get("output_tokens", 0) or 0),
            "total_tokens": int(
                usage_raw.get("total_tokens")
                or (usage_raw.get("input_tokens", 0) or 0) + (usage_raw.get("output_tokens", 0) or 0)
            ),
        }
        return ModelTurn(
            response_id=str(payload.get("id", "")),
            resolved_model=str(payload.get("model", "")),
            status=status,
            output_items=[item for item in output if isinstance(item, dict)],
            tool_calls=tool_calls,
            final_text="".join(text_parts) or None,
            usage=usage,
        )
