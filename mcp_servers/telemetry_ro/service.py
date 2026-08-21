from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence


PROMETHEUS_URL_ENV = "RESBENCH_PROMETHEUS_URL"
JAEGER_URL_ENV = "RESBENCH_JAEGER_URL"
LOKI_URL_ENV = "RESBENCH_LOKI_URL"
TIMEOUT_ENV = "RESBENCH_TELEMETRY_TIMEOUT_SECONDS"
NAMESPACE_ALLOWLIST_ENV = "RESBENCH_TELEMETRY_ALLOWED_NAMESPACES"
JAEGER_SERVICE_ALLOWLIST_ENV = "RESBENCH_JAEGER_ALLOWED_SERVICES"

MAX_OUTPUT_CHARS = 25_000
MAX_RESPONSE_BYTES = 2_000_000
DEFAULT_LIMIT = 50
MAX_LIMIT = 500
MAX_TRACE_LIMIT = 100
MAX_LOG_LIMIT = 500
MAX_QUERY_LENGTH = 2_000
MAX_LABEL_LENGTH = 128
MAX_SERVICE_LENGTH = 256
MAX_TIME_WINDOW_SECONDS = 6 * 60 * 60
MIN_STEP_SECONDS = 1
MAX_STEP_SECONDS = 3_600
NAMESPACE_LABEL_KEYS = ("namespace", "kubernetes_namespace", "exported_namespace")
SCOPE_WARNING = (
    "Some telemetry items were removed because they did not carry an allowed namespace label. "
    "Prometheus and Loki queries used by agents must preserve one of: namespace, "
    "kubernetes_namespace, exported_namespace."
)

_NAME_RE = re.compile(r"^[A-Za-z0-9_.:/@+=,\- ]{1,256}$")
_LABEL_RE = re.compile(r"^[A-Za-z_:][A-Za-z0-9_:]{0,127}$")
_NAMESPACE_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_TRACE_ID_RE = re.compile(r"^[A-Fa-f0-9]{1,32}$")
_SECRET_HINT_RE = re.compile(r"(token|secret|password|passwd|apikey|api_key|access_key)", re.IGNORECASE)
_SECRET_KEY_RE = re.compile(r"(token|secret|password|passwd|apikey|api_key|access_key)", re.IGNORECASE)
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)
_SK_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}")
_ASSIGNMENT_SECRET_RE = re.compile(
    r"(?i)\b(password|token|secret|api_key|apikey|access_key)\b(\s*[:=]\s*)([^\s,;\"'{}\]]+)"
)


class TelemetryROError(ValueError):
    """Structured user-facing error for telemetry_ro tools."""

    def __init__(self, code: str, message: str, action: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.action = action

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "action": self.action}


@dataclass(frozen=True)
class ScopedItems:
    items: list[Any]
    scoped_out_count: int


def envelope(payload: dict[str, Any]) -> dict[str, Any]:
    return _cap_envelope(_redact_payload({"ok": True, **payload}))


def error_envelope(exc: TelemetryROError) -> dict[str, Any]:
    return _redact_payload({"ok": False, "error": exc.to_dict()})


@dataclass(frozen=True)
class RuntimeConfig:
    prometheus_url: str | None = None
    jaeger_url: str | None = None
    loki_url: str | None = None
    namespace_allowlist: frozenset[str] = frozenset()
    jaeger_service_allowlist: frozenset[str] = frozenset()
    timeout_seconds: float = 5.0

    @classmethod
    def from_env(cls) -> "RuntimeConfig":
        timeout_raw = os.environ.get(TIMEOUT_ENV, "5")
        try:
            timeout = float(timeout_raw)
        except ValueError as exc:
            raise TelemetryROError(
                "invalid_timeout",
                f"{TIMEOUT_ENV} must be a number of seconds.",
                "Set a numeric timeout between 0.5 and 30 seconds.",
            ) from exc
        if timeout < 0.5 or timeout > 30:
            raise TelemetryROError(
                "invalid_timeout",
                f"{TIMEOUT_ENV} must be between 0.5 and 30 seconds.",
                "Use a bounded HTTP timeout so telemetry calls cannot hang the agent.",
            )
        return cls(
            prometheus_url=_clean_base_url(os.environ.get(PROMETHEUS_URL_ENV), PROMETHEUS_URL_ENV),
            jaeger_url=_clean_base_url(os.environ.get(JAEGER_URL_ENV), JAEGER_URL_ENV),
            loki_url=_clean_base_url(os.environ.get(LOKI_URL_ENV), LOKI_URL_ENV),
            namespace_allowlist=_parse_namespace_allowlist(os.environ.get(NAMESPACE_ALLOWLIST_ENV)),
            jaeger_service_allowlist=_parse_service_allowlist(os.environ.get(JAEGER_SERVICE_ALLOWLIST_ENV)),
            timeout_seconds=timeout,
        )


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    json_data: Any


class TelemetryTransport(Protocol):
    async def get_json(
        self,
        *,
        base_url: str,
        path: str,
        params: Mapping[str, Any],
        timeout_seconds: float,
        max_bytes: int,
    ) -> HttpResponse:
        ...


class UrlLibTelemetryTransport:
    """Small GET-only transport using Python stdlib so tests can inject fakes."""

    async def get_json(
        self,
        *,
        base_url: str,
        path: str,
        params: Mapping[str, Any],
        timeout_seconds: float,
        max_bytes: int,
    ) -> HttpResponse:
        return await asyncio.to_thread(
            self._get_json_sync,
            base_url=base_url,
            path=path,
            params=params,
            timeout_seconds=timeout_seconds,
            max_bytes=max_bytes,
        )

    def _get_json_sync(
        self,
        *,
        base_url: str,
        path: str,
        params: Mapping[str, Any],
        timeout_seconds: float,
        max_bytes: int,
    ) -> HttpResponse:
        query = urllib.parse.urlencode(params, doseq=True)
        url = f"{base_url}{path}"
        if query:
            url = f"{url}?{query}"
        request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                raw = response.read(max_bytes + 1)
                status = int(response.status)
        except urllib.error.HTTPError as exc:
            raw = exc.read(max_bytes + 1)
            status = int(exc.code)
        except TimeoutError as exc:
            raise TelemetryROError(
                "upstream_timeout",
                "telemetry upstream request timed out.",
                "Use a narrower time window or verify the telemetry service is reachable from the MCP host.",
            ) from exc
        except OSError as exc:
            raise TelemetryROError(
                "upstream_unreachable",
                "telemetry upstream request failed before receiving a response.",
                "Verify the configured telemetry endpoint and network path on the MCP host.",
            ) from exc

        if len(raw) > max_bytes:
            raise TelemetryROError(
                "upstream_response_too_large",
                f"telemetry upstream response exceeded {max_bytes} bytes.",
                "Reduce limit, narrow the time window, or add a more selective query.",
            )
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TelemetryROError(
                "invalid_upstream_json",
                "telemetry upstream did not return valid JSON.",
                "Verify the configured endpoint points at the expected Prometheus, Jaeger, or Loki API.",
            ) from exc
        if status >= 400:
            raise TelemetryROError(
                "upstream_http_error",
                f"telemetry upstream returned HTTP {status}.",
                "Check the query syntax and telemetry service health; raw endpoint URLs are intentionally hidden.",
            )
        return HttpResponse(status_code=status, json_data=payload)


class TelemetryROService:
    def __init__(
        self,
        config: RuntimeConfig | None = None,
        transport: TelemetryTransport | None = None,
    ) -> None:
        self.config = config if config is not None else RuntimeConfig.from_env()
        self.transport = transport if transport is not None else UrlLibTelemetryTransport()

    async def prometheus_query_instant(self, *, query: str, time: int, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
        _validate_query(query)
        time = _bounded_time(time, "time")
        limit = _bounded_limit(limit)
        data = await self._request("prometheus", "/api/v1/query", {"query": query, "time": time})
        prometheus_data = _expect_mapping(data.get("data"), "Prometheus data")
        scoped = _scope_filter_metric_items(prometheus_data.get("result"), self.config.namespace_allowlist)
        result = _limited_result(scoped.items, limit)
        return envelope(
            {
                "service": "prometheus",
                "operation": "query_instant",
                "time": time,
                "status": data.get("status"),
                "resultType": prometheus_data.get("resultType"),
                "returned": len(result["items"]),
                "limit": limit,
                "hasMore": result["hasMore"],
                "scopedOutCount": scoped.scoped_out_count,
                "scopeWarning": SCOPE_WARNING if scoped.scoped_out_count else None,
                "result": result["items"],
            }
        )

    async def prometheus_query_range(
        self,
        *,
        query: str,
        start: int,
        end: int,
        step: int,
        limit: int = DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        _validate_query(query)
        start, end = _bounded_window(start, end)
        step = _bounded_step(step)
        limit = _bounded_limit(limit)
        data = await self._request(
            "prometheus",
            "/api/v1/query_range",
            {"query": query, "start": start, "end": end, "step": step},
        )
        prometheus_data = _expect_mapping(data.get("data"), "Prometheus data")
        scoped = _scope_filter_metric_items(prometheus_data.get("result"), self.config.namespace_allowlist)
        result = _limited_result(scoped.items, limit)
        return envelope(
            {
                "service": "prometheus",
                "operation": "query_range",
                "start": start,
                "end": end,
                "step": step,
                "status": data.get("status"),
                "resultType": prometheus_data.get("resultType"),
                "returned": len(result["items"]),
                "limit": limit,
                "hasMore": result["hasMore"],
                "scopedOutCount": scoped.scoped_out_count,
                "scopeWarning": SCOPE_WARNING if scoped.scoped_out_count else None,
                "result": result["items"],
            }
        )

    async def prometheus_list_labels(
        self,
        *,
        start: int,
        end: int,
        match: Sequence[str] | None = None,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> dict[str, Any]:
        start, end = _bounded_window(start, end)
        limit = _bounded_limit(limit)
        offset = _bounded_offset(offset)
        params: dict[str, Any] = {"start": start, "end": end}
        matches = _bounded_matchers(match)
        if matches:
            params["match[]"] = matches
        data = await self._request("prometheus", "/api/v1/labels", params)
        page = _page_items(_expect_sequence(data.get("data"), "Prometheus labels"), limit, offset)
        return envelope({"service": "prometheus", "operation": "labels", "start": start, "end": end, **page})

    async def prometheus_list_series(
        self,
        *,
        match: Sequence[str],
        start: int,
        end: int,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> dict[str, Any]:
        start, end = _bounded_window(start, end)
        limit = _bounded_limit(limit)
        offset = _bounded_offset(offset)
        matches = _bounded_matchers(match, required=True)
        data = await self._request("prometheus", "/api/v1/series", {"match[]": matches, "start": start, "end": end})
        scoped = _scope_filter_label_mappings(data.get("data"), self.config.namespace_allowlist)
        page = _page_items(scoped.items, limit, offset)
        return envelope(
            {
                "service": "prometheus",
                "operation": "series",
                "start": start,
                "end": end,
                "scopedOutCount": scoped.scoped_out_count,
                "scopeWarning": SCOPE_WARNING if scoped.scoped_out_count else None,
                **page,
            }
        )

    async def jaeger_list_services(self, *, limit: int = DEFAULT_LIMIT, offset: int = 0) -> dict[str, Any]:
        limit = _bounded_limit(limit)
        offset = _bounded_offset(offset)
        data = await self._request("jaeger", "/api/services", {})
        services = _expect_sequence(data.get("data"), "Jaeger services")
        scoped_items = [item for item in services if isinstance(item, str) and item in self.config.jaeger_service_allowlist]
        page = _page_items(scoped_items, limit, offset)
        return envelope(
            {
                "service": "jaeger",
                "operation": "services",
                "scopedOutCount": len(services) - len(scoped_items),
                "scopeWarning": "Only Jaeger services in the configured allowlist are returned.",
                **page,
            }
        )

    async def jaeger_list_operations(
        self,
        *,
        service: str,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> dict[str, Any]:
        _validate_name(service, "service")
        _ensure_allowed_jaeger_service(service, self.config.jaeger_service_allowlist)
        limit = _bounded_limit(limit)
        offset = _bounded_offset(offset)
        data = await self._request(
            "jaeger",
            f"/api/services/{urllib.parse.quote(service, safe='')}/operations",
            {},
        )
        page = _page_items(_expect_sequence(data.get("data"), "Jaeger operations"), limit, offset)
        return envelope({"service": "jaeger", "operation": "operations", "targetService": service, **page})

    async def jaeger_find_traces(
        self,
        *,
        service: str,
        start: int,
        end: int,
        operation: str | None = None,
        tags: str | None = None,
        min_duration: str | None = None,
        max_duration: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        _validate_name(service, "service")
        _ensure_allowed_jaeger_service(service, self.config.jaeger_service_allowlist)
        if operation is not None:
            _validate_name(operation, "operation")
        if tags is not None:
            _validate_tags(tags)
        start, end = _bounded_window(start, end)
        limit = _bounded_limit(limit, cap=MAX_TRACE_LIMIT)
        params: dict[str, Any] = {"service": service, "start": start * 1_000_000, "end": end * 1_000_000, "limit": limit}
        if operation:
            params["operation"] = operation
        if tags:
            params["tags"] = tags
        if min_duration:
            params["minDuration"] = _validate_duration(min_duration, "min_duration")
        if max_duration:
            params["maxDuration"] = _validate_duration(max_duration, "max_duration")
        data = await self._request("jaeger", "/api/traces", params)
        scoped = _scope_filter_traces(data.get("data"), self.config.jaeger_service_allowlist)
        traces = _limited_result(scoped.items, limit)
        return envelope(
            {
                "service": "jaeger",
                "operation": "find_traces",
                "targetService": service,
                "start": start,
                "end": end,
                "returned": len(traces["items"]),
                "limit": limit,
                "hasMore": traces["hasMore"],
                "scopedOutCount": scoped.scoped_out_count,
                "scopeWarning": "Traces containing services outside the Jaeger allowlist are removed."
                if scoped.scoped_out_count
                else None,
                "traces": traces["items"],
            }
        )

    async def jaeger_get_trace(self, *, trace_id: str) -> dict[str, Any]:
        if not _TRACE_ID_RE.fullmatch(trace_id):
            raise TelemetryROError(
                "invalid_trace_id",
                "trace_id must be 1 to 32 hexadecimal characters.",
                "Pass the traceID exactly as returned by jaeger_find_traces.",
            )
        data = await self._request("jaeger", f"/api/traces/{trace_id.lower()}", {})
        traces = _expect_sequence(data.get("data"), "Jaeger trace data")
        scoped = _scope_filter_traces(traces, self.config.jaeger_service_allowlist)
        if scoped.scoped_out_count:
            raise TelemetryROError(
                "trace_outside_service_scope",
                "trace contains a service outside the configured Jaeger allowlist.",
                "Use telemetry_jaeger_find_traces for an allowed service and do not fetch trace ids from another scope.",
            )
        return envelope({"service": "jaeger", "operation": "get_trace", "traceId": trace_id.lower(), "traces": traces})

    async def loki_list_labels(
        self,
        *,
        start: int,
        end: int,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> dict[str, Any]:
        start, end = _bounded_window(start, end)
        limit = _bounded_limit(limit)
        offset = _bounded_offset(offset)
        data = await self._request("loki", "/loki/api/v1/labels", {"start": _seconds_to_ns(start), "end": _seconds_to_ns(end)})
        page = _page_items(_expect_sequence(data.get("data"), "Loki labels"), limit, offset)
        return envelope({"service": "loki", "operation": "labels", "start": start, "end": end, **page})

    async def loki_query(
        self,
        *,
        query: str,
        time: int,
        limit: int = DEFAULT_LIMIT,
        direction: str = "backward",
    ) -> dict[str, Any]:
        _validate_query(query)
        time = _bounded_time(time, "time")
        limit = _bounded_limit(limit, cap=MAX_LOG_LIMIT)
        direction = _bounded_direction(direction)
        data = await self._request(
            "loki",
            "/loki/api/v1/query",
            {"query": query, "time": _seconds_to_ns(time), "limit": limit, "direction": direction},
        )
        loki_data = _expect_mapping(data.get("data"), "Loki data")
        scoped = _scope_filter_loki_items(loki_data.get("result"), self.config.namespace_allowlist)
        result = _limited_result(scoped.items, limit)
        return envelope(
            {
                "service": "loki",
                "operation": "query",
                "time": time,
                "resultType": loki_data.get("resultType"),
                "returned": len(result["items"]),
                "limit": limit,
                "hasMore": result["hasMore"],
                "scopedOutCount": scoped.scoped_out_count,
                "scopeWarning": SCOPE_WARNING if scoped.scoped_out_count else None,
                "result": result["items"],
            }
        )

    async def loki_query_range(
        self,
        *,
        query: str,
        start: int,
        end: int,
        step: int,
        limit: int = DEFAULT_LIMIT,
        direction: str = "backward",
    ) -> dict[str, Any]:
        _validate_query(query)
        start, end = _bounded_window(start, end)
        step = _bounded_step(step)
        limit = _bounded_limit(limit, cap=MAX_LOG_LIMIT)
        direction = _bounded_direction(direction)
        data = await self._request(
            "loki",
            "/loki/api/v1/query_range",
            {
                "query": query,
                "start": _seconds_to_ns(start),
                "end": _seconds_to_ns(end),
                "step": step,
                "limit": limit,
                "direction": direction,
            },
        )
        loki_data = _expect_mapping(data.get("data"), "Loki data")
        scoped = _scope_filter_loki_items(loki_data.get("result"), self.config.namespace_allowlist)
        result = _limited_result(scoped.items, limit)
        return envelope(
            {
                "service": "loki",
                "operation": "query_range",
                "start": start,
                "end": end,
                "step": step,
                "resultType": loki_data.get("resultType"),
                "returned": len(result["items"]),
                "limit": limit,
                "hasMore": result["hasMore"],
                "scopedOutCount": scoped.scoped_out_count,
                "scopeWarning": SCOPE_WARNING if scoped.scoped_out_count else None,
                "result": result["items"],
            }
        )

    async def _request(self, service: str, path: str, params: Mapping[str, Any]) -> dict[str, Any]:
        base_url = self._base_url_for(service)
        response = await self.transport.get_json(
            base_url=base_url,
            path=path,
            params=params,
            timeout_seconds=self.config.timeout_seconds,
            max_bytes=MAX_RESPONSE_BYTES,
        )
        if response.status_code >= 400:
            raise TelemetryROError(
                "upstream_http_error",
                f"{service} returned HTTP {response.status_code}.",
                "Check query syntax and service health; URLs and credentials are intentionally hidden.",
            )
        if not isinstance(response.json_data, dict):
            raise TelemetryROError(
                "invalid_upstream_json",
                f"{service} response was not a JSON object.",
                "Verify the configured endpoint points at the expected telemetry API.",
            )
        return response.json_data

    def _base_url_for(self, service: str) -> str:
        value = {
            "prometheus": self.config.prometheus_url,
            "jaeger": self.config.jaeger_url,
            "loki": self.config.loki_url,
        }[service]
        if value:
            return value
        env_name = {
            "prometheus": PROMETHEUS_URL_ENV,
            "jaeger": JAEGER_URL_ENV,
            "loki": LOKI_URL_ENV,
        }[service]
        raise TelemetryROError(
            "missing_telemetry_endpoint",
            f"{service} endpoint is not configured.",
            f"Set {env_name} in the MCP runtime environment; do not pass URLs to tools.",
        )


def _clean_base_url(value: str | None, env_name: str) -> str | None:
    if value is None or not value.strip():
        return None
    parsed = urllib.parse.urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise TelemetryROError(
            "invalid_telemetry_endpoint",
            f"{env_name} must be an http(s) URL.",
            "Configure a base URL such as http://prometheus.monitoring.svc:9090 without credentials.",
        )
    if parsed.username or parsed.password:
        raise TelemetryROError(
            "invalid_telemetry_endpoint",
            f"{env_name} must not contain userinfo.",
            "Put credentials in runtime secret mechanisms, not inside telemetry endpoint URLs.",
        )
    clean = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))
    return clean


def _parse_namespace_allowlist(value: str | None) -> frozenset[str]:
    namespaces = _parse_allowlist(value, NAMESPACE_ALLOWLIST_ENV)
    invalid = [item for item in namespaces if not _NAMESPACE_RE.fullmatch(item)]
    if invalid:
        raise TelemetryROError(
            "invalid_namespace_allowlist",
            f"{NAMESPACE_ALLOWLIST_ENV} contains an invalid Kubernetes namespace.",
            "Use comma or whitespace separated Kubernetes namespace names.",
        )
    return frozenset(namespaces)


def _parse_service_allowlist(value: str | None) -> frozenset[str]:
    services = _parse_allowlist(value, JAEGER_SERVICE_ALLOWLIST_ENV)
    for service in services:
        _validate_name(service, "service")
    return frozenset(services)


def _parse_allowlist(value: str | None, env_name: str) -> list[str]:
    if value is None or not value.strip():
        raise TelemetryROError(
            "missing_scope_allowlist",
            f"{env_name} is required.",
            "Configure an explicit allowlist before starting telemetry_ro; shared clusters must fail closed.",
        )
    items = [part for part in re.split(r"[\s,]+", value.strip()) if part]
    if not items:
        raise TelemetryROError(
            "missing_scope_allowlist",
            f"{env_name} is required.",
            "Configure an explicit allowlist before starting telemetry_ro.",
        )
    if len(items) > 100:
        raise TelemetryROError(
            "invalid_scope_allowlist",
            f"{env_name} contains too many entries.",
            "Use a narrow benchmark-specific allowlist.",
        )
    if len(set(items)) != len(items):
        raise TelemetryROError(
            "invalid_scope_allowlist",
            f"{env_name} contains duplicate entries.",
            "Remove duplicates so the runtime scope is auditable.",
        )
    return items


def _validate_query(query: str) -> None:
    if not query or not query.strip():
        raise TelemetryROError("invalid_query", "query must not be empty.", "Pass a concrete PromQL or LogQL query.")
    if len(query) > MAX_QUERY_LENGTH:
        raise TelemetryROError(
            "invalid_query",
            f"query exceeds {MAX_QUERY_LENGTH} characters.",
            "Use a narrower query expression.",
        )
    if "\x00" in query:
        raise TelemetryROError("invalid_query", "query must not contain NUL bytes.", "Pass a text query.")
    if _SECRET_HINT_RE.search(query):
        raise TelemetryROError(
            "sensitive_query_rejected",
            "query appears to include a credential-like key.",
            "Do not search for or pass secrets through telemetry MCP tools.",
        )


def _validate_name(value: str, field: str) -> None:
    if not value or len(value) > MAX_SERVICE_LENGTH or not _NAME_RE.fullmatch(value):
        raise TelemetryROError(
            f"invalid_{field}",
            f"{field} contains unsupported characters or length.",
            f"Use the exact {field} value returned by the telemetry service list tool.",
        )
    if _SECRET_HINT_RE.search(value):
        raise TelemetryROError(
            f"sensitive_{field}_rejected",
            f"{field} appears to contain a credential-like token.",
            f"Use service metadata only, never credentials, as {field}.",
        )


def _ensure_allowed_jaeger_service(service: str, allowlist: frozenset[str]) -> None:
    if service not in allowlist:
        raise TelemetryROError(
            "jaeger_service_outside_scope",
            "requested Jaeger service is outside the configured allowlist.",
            "Use telemetry_jaeger_list_services and select one of the returned services.",
        )


def _validate_tags(tags: str) -> None:
    if len(tags) > 1_000 or "\x00" in tags:
        raise TelemetryROError(
            "invalid_tags",
            "tags must be a compact Jaeger JSON tag filter string.",
            "Use a short tag filter such as {\"http.status_code\":\"500\"}.",
        )
    try:
        parsed = json.loads(tags)
    except json.JSONDecodeError as exc:
        raise TelemetryROError(
            "invalid_tags",
            "tags must be valid JSON.",
            "Pass a JSON object string accepted by Jaeger, for example {\"error\":\"true\"}.",
        ) from exc
    if not isinstance(parsed, dict):
        raise TelemetryROError("invalid_tags", "tags must decode to a JSON object.", "Use a JSON object string.")


def _validate_duration(value: str, field: str) -> str:
    if not re.fullmatch(r"[1-9][0-9]{0,5}(ns|us|ms|s|m|h)", value):
        raise TelemetryROError(
            f"invalid_{field}",
            f"{field} must use a bounded Jaeger duration such as 500ms, 2s, or 1m.",
            "Use a numeric duration with unit ns, us, ms, s, m, or h.",
        )
    return value


def _bounded_time(value: int, field: str) -> int:
    if not isinstance(value, int):
        raise TelemetryROError(f"invalid_{field}", f"{field} must be unix seconds as an integer.", "Pass controller-provided unix seconds.")
    if value < 0:
        raise TelemetryROError(f"invalid_{field}", f"{field} must be non-negative unix seconds.", "Pass controller-provided unix seconds.")
    return value


def _bounded_window(start: int, end: int) -> tuple[int, int]:
    start = _bounded_time(start, "start")
    end = _bounded_time(end, "end")
    if end < start:
        raise TelemetryROError("invalid_time_window", "end must be greater than or equal to start.", "Swap start/end or use a valid run window.")
    if end - start > MAX_TIME_WINDOW_SECONDS:
        raise TelemetryROError(
            "time_window_too_large",
            f"time window exceeds {MAX_TIME_WINDOW_SECONDS} seconds.",
            "Use a narrower benchmark episode window.",
        )
    return start, end


def _bounded_step(step: int) -> int:
    if not isinstance(step, int) or step < MIN_STEP_SECONDS or step > MAX_STEP_SECONDS:
        raise TelemetryROError(
            "invalid_step",
            f"step must be between {MIN_STEP_SECONDS} and {MAX_STEP_SECONDS} seconds.",
            "Use a bounded resolution to avoid excessive telemetry responses.",
        )
    return step


def _bounded_limit(limit: int | None, *, cap: int = MAX_LIMIT) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    if not isinstance(limit, int) or limit < 1:
        raise TelemetryROError("invalid_pagination", "limit must be at least 1.", "Use a positive integer limit.")
    return min(limit, cap)


def _bounded_offset(offset: int | None) -> int:
    if offset is None:
        return 0
    if not isinstance(offset, int) or offset < 0:
        raise TelemetryROError("invalid_pagination", "offset must not be negative.", "Use offset >= 0.")
    return offset


def _bounded_matchers(match: Sequence[str] | None, *, required: bool = False) -> list[str]:
    if not match:
        if required:
            raise TelemetryROError("invalid_match", "at least one match expression is required.", "Pass one or more Prometheus series matchers.")
        return []
    if len(match) > 20:
        raise TelemetryROError("invalid_match", "too many match expressions.", "Use at most 20 match expressions.")
    matches = []
    for item in match:
        _validate_query(item)
        matches.append(item)
    return matches


def _bounded_direction(direction: str) -> str:
    if direction not in {"forward", "backward"}:
        raise TelemetryROError("invalid_direction", "direction must be 'forward' or 'backward'.", "Use Loki's supported query direction values.")
    return direction


def _expect_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TelemetryROError(
            "invalid_upstream_json",
            f"{label} was not a JSON object.",
            "Verify the configured telemetry endpoint and response schema.",
        )
    return value


def _expect_sequence(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise TelemetryROError(
            "invalid_upstream_json",
            f"{label} was not a JSON array.",
            "Verify the configured telemetry endpoint and response schema.",
        )
    return value


def _namespace_value(labels: Mapping[str, Any]) -> str | None:
    for key in NAMESPACE_LABEL_KEYS:
        value = labels.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _in_namespace_scope(labels: Mapping[str, Any], allowlist: frozenset[str]) -> bool:
    namespace = _namespace_value(labels)
    return namespace in allowlist if namespace is not None else False


def _scope_filter_label_mappings(value: Any, allowlist: frozenset[str]) -> ScopedItems:
    items = _expect_sequence(value or [], "telemetry scoped result")
    scoped: list[Any] = []
    for item in items:
        if isinstance(item, Mapping) and _in_namespace_scope(item, allowlist):
            scoped.append(item)
    return ScopedItems(scoped, len(items) - len(scoped))


def _scope_filter_metric_items(value: Any, allowlist: frozenset[str]) -> ScopedItems:
    items = _expect_sequence(value or [], "telemetry result")
    scoped: list[Any] = []
    for item in items:
        labels = item.get("metric") if isinstance(item, Mapping) else None
        if isinstance(labels, Mapping) and _in_namespace_scope(labels, allowlist):
            scoped.append(item)
    return ScopedItems(scoped, len(items) - len(scoped))


def _scope_filter_loki_items(value: Any, allowlist: frozenset[str]) -> ScopedItems:
    items = _expect_sequence(value or [], "Loki result")
    scoped: list[Any] = []
    for item in items:
        labels = None
        if isinstance(item, Mapping):
            labels = item.get("stream") if isinstance(item.get("stream"), Mapping) else item.get("metric")
        if isinstance(labels, Mapping) and _in_namespace_scope(labels, allowlist):
            scoped.append(item)
    return ScopedItems(scoped, len(items) - len(scoped))


def _trace_service_names(trace: Any) -> set[str]:
    if not isinstance(trace, Mapping):
        return set()
    names: set[str] = set()
    processes = trace.get("processes")
    if isinstance(processes, Mapping):
        for process in processes.values():
            if isinstance(process, Mapping):
                name = process.get("serviceName")
                if isinstance(name, str) and name:
                    names.add(name)
    for span in trace.get("spans", []) if isinstance(trace.get("spans"), list) else []:
        if isinstance(span, Mapping):
            process = span.get("process")
            if isinstance(process, Mapping):
                name = process.get("serviceName")
                if isinstance(name, str) and name:
                    names.add(name)
            references = span.get("references")
            if isinstance(references, list):
                for reference in references:
                    if isinstance(reference, Mapping):
                        name = reference.get("serviceName")
                        if isinstance(name, str) and name:
                            names.add(name)
    return names


def _scope_filter_traces(value: Any, allowlist: frozenset[str]) -> ScopedItems:
    traces = _expect_sequence(value or [], "Jaeger trace data")
    scoped: list[Any] = []
    for trace in traces:
        names = _trace_service_names(trace)
        if names and names.issubset(allowlist):
            scoped.append(trace)
    return ScopedItems(scoped, len(traces) - len(scoped))


def _redact_payload(value: Any, *, parent_key: str | None = None) -> Any:
    if parent_key is not None and _SECRET_KEY_RE.search(parent_key):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {str(key): _redact_payload(item, parent_key=str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_payload(item, parent_key=parent_key) for item in value]
    if isinstance(value, tuple):
        return [_redact_payload(item, parent_key=parent_key) for item in value]
    if isinstance(value, str):
        return _redact_string(value)
    return value


def _redact_string(value: str) -> str:
    redacted = _BEARER_RE.sub("Bearer <redacted>", value)
    redacted = _SK_KEY_RE.sub("sk-<redacted>", redacted)
    return _ASSIGNMENT_SECRET_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}<redacted>", redacted)


def _page_items(items: Sequence[Any], limit: int, offset: int) -> dict[str, Any]:
    total = len(items)
    page = list(items[offset : offset + limit])
    next_offset = offset + len(page) if offset + len(page) < total else None
    return {
        "total": total,
        "returned": len(page),
        "limit": limit,
        "offset": offset,
        "hasMore": next_offset is not None,
        "nextOffset": next_offset,
        "items": page,
    }


def _limited_result(value: Any, limit: int) -> dict[str, Any]:
    items = _expect_sequence(value or [], "telemetry result")
    limited = list(items[:limit])
    return {"items": limited, "hasMore": len(items) > len(limited)}


def _seconds_to_ns(seconds: int) -> str:
    return str(seconds * 1_000_000_000)


def _cap_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    serialized = json.dumps(payload, ensure_ascii=True, sort_keys=True)
    if len(serialized) <= MAX_OUTPUT_CHARS:
        return payload
    capped = dict(payload)
    capped["truncated"] = True
    capped["truncationMessage"] = (
        f"Response exceeded {MAX_OUTPUT_CHARS} characters. "
        "Use a lower limit, narrower time window, or more selective query."
    )
    for key in ("result", "traces", "items"):
        if isinstance(capped.get(key), list):
            capped[key] = capped[key][: max(1, len(capped[key]) // 2)]
            capped["returned"] = len(capped[key])
            break
    if len(json.dumps(capped, ensure_ascii=True, sort_keys=True)) > MAX_OUTPUT_CHARS:
        for key in ("result", "traces", "items"):
            if isinstance(capped.get(key), list):
                capped[key] = []
                capped["returned"] = 0
        capped["truncationMessage"] = (
            f"Response exceeded {MAX_OUTPUT_CHARS} characters even after item truncation. "
            "No result items are returned; make the query narrower."
        )
    return capped
