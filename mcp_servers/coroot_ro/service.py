"""Controller-scoped read-only adapter for the documented Coroot HTTP API.

The adapter intentionally exposes only GET requests.  It uses Coroot's
application URL for trace and log reads, and the documented project Prometheus
dashboard panel range API for metrics.  The Controller injects every URL identity; callers
cannot select a project, an application, a namespace, or an upstream endpoint.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping, Protocol

from mcp_servers.common.scope import MAX_OBSERVATION_WINDOW_SECONDS, ObservationScope, ScopeError


COROOT_URL_ENV = "RESBENCH_COROOT_URL"
COROOT_PROJECT_ID_ENV = "RESBENCH_COROOT_PROJECT_ID"
COROOT_APPLICATION_ID_ENV = "RESBENCH_COROOT_APPLICATION_ID"
COROOT_ALLOWED_NAMESPACE_ENV = "RESBENCH_COROOT_ALLOWED_NAMESPACE"
COROOT_ALLOWED_SERVICES_ENV = "RESBENCH_COROOT_ALLOWED_SERVICES"
COROOT_TIMEOUT_ENV = "RESBENCH_COROOT_TIMEOUT_SECONDS"
COROOT_SESSION_COOKIE_ENV = "RESBENCH_COROOT_SESSION_COOKIE"
# "true" when the Controller runs Coroot without login. The service only
# issues fixed read-only GET requests, so it may then read anonymously.
COROOT_ALLOW_ANONYMOUS_ENV = "RESBENCH_COROOT_ALLOW_ANONYMOUS_READ"

MAX_RESPONSE_BYTES = 2_000_000
MAX_OUTPUT_CHARS = 25_000
MAX_LABELS = 12
MAX_LOG_ENTRIES = 100
MAX_TRACES = 100
COROOT_VIEWER_ROLE = "Viewer"
_METRIC_RE = re.compile(r"^[A-Za-z_:][A-Za-z0-9_:]{0,255}$")
_LABEL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_SERVICE_RE = re.compile(r"^[A-Za-z0-9_.:/@+=,\- ]{1,256}$")
_SESSION_COOKIE_RE = re.compile(r"^[A-Za-z0-9_=-]{1,4096}\.[A-Za-z0-9_=-]{1,4096}$")
_NAMESPACE_LABELS = frozenset({"namespace", "kubernetes_namespace", "exported_namespace"})


class CorootROError(ValueError):
    """Structured error safe for an MCP response."""

    def __init__(self, code: str, message: str, action: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.action = action

    def to_dict(self) -> dict[str, str]:
        """Return the stable error shape used by all Coroot tools."""

        return {"code": self.code, "message": self.message, "action": self.action}


def envelope(payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap a successful, bounded Coroot result."""

    return {"ok": True, "source": "coroot", **payload}


def error_envelope(exc: CorootROError | ScopeError) -> dict[str, Any]:
    """Wrap an expected scope or backend qualification failure."""

    return {"ok": False, "error": exc.to_dict()}


@dataclass(frozen=True)
class RuntimeConfig:
    """All Coroot identity and authorization inputs supplied by the Controller."""

    base_url: str
    project_id: str
    scope: ObservationScope
    allowed_services: frozenset[str]
    timeout_seconds: float = 5.0
    session_cookie: str | None = None
    allow_anonymous_read: bool = False

    @classmethod
    def from_env(cls) -> "RuntimeConfig":
        """Load process-only configuration; no caller parameter can override it."""

        base_url = _base_url(os.environ.get(COROOT_URL_ENV))
        project_id = _required_identifier(os.environ.get(COROOT_PROJECT_ID_ENV), COROOT_PROJECT_ID_ENV)
        application = _required_identifier(
            os.environ.get(COROOT_APPLICATION_ID_ENV), COROOT_APPLICATION_ID_ENV
        )
        namespace = (os.environ.get(COROOT_ALLOWED_NAMESPACE_ENV) or "").strip()
        if not namespace:
            raise CorootROError(
                "missing_namespace_scope",
                "coroot_ro requires one Controller-provided namespace.",
                f"Set {COROOT_ALLOWED_NAMESPACE_ENV} for this Trial.",
            )
        try:
            scope = ObservationScope(namespace=namespace, application=application)
        except ScopeError as exc:
            raise CorootROError(exc.code, exc.message, exc.action) from exc
        services = _services(os.environ.get(COROOT_ALLOWED_SERVICES_ENV))
        raw_timeout = os.environ.get(COROOT_TIMEOUT_ENV, "5")
        try:
            timeout = float(raw_timeout)
        except ValueError as exc:
            raise CorootROError("invalid_timeout", "Coroot timeout must be numeric.", "Use 0.5 to 30 seconds.") from exc
        if not 0.5 <= timeout <= 30:
            raise CorootROError("invalid_timeout", "Coroot timeout is outside the permitted range.", "Use 0.5 to 30 seconds.")
        allow_anonymous = (os.environ.get(COROOT_ALLOW_ANONYMOUS_ENV) or "").strip().lower() == "true"
        raw_cookie = os.environ.get(COROOT_SESSION_COOKIE_ENV)
        session_cookie = None if allow_anonymous and not raw_cookie else _session_cookie(raw_cookie)
        return cls(
            base_url=base_url,
            project_id=project_id,
            scope=scope,
            allowed_services=services,
            timeout_seconds=timeout,
            session_cookie=session_cookie,
            allow_anonymous_read=allow_anonymous,
        )


@dataclass(frozen=True)
class HttpResponse:
    """Bounded JSON response returned by a Coroot transport."""

    status_code: int
    json_data: Any


class CorootTransport(Protocol):
    """GET-only transport that permits deterministic fake Coroot backends in tests."""

    async def get_json(
        self,
        *,
        base_url: str,
        path: str,
        params: Mapping[str, Any],
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_bytes: int,
    ) -> HttpResponse:
        """Fetch a JSON response without making a mutation request."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        """Do not forward the scoped Coroot credential to a redirect target."""
        return None


class UrlLibCorootTransport:
    """Minimal stdlib implementation; all outbound requests are HTTP GET."""

    async def get_json(self, **kwargs: Any) -> HttpResponse:
        """Run the blocking standard-library request away from the event loop."""

        return await asyncio.to_thread(self._get_json_sync, **kwargs)

    @staticmethod
    def _get_json_sync(
        *,
        base_url: str,
        path: str,
        params: Mapping[str, Any],
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_bytes: int,
    ) -> HttpResponse:
        encoded = urllib.parse.urlencode(params, doseq=True)
        url = f"{base_url}{path}" + (f"?{encoded}" if encoded else "")
        request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json", **headers})
        try:
            with urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout_seconds) as response:
                body = response.read(max_bytes + 1)
                if len(body) > max_bytes:
                    raise CorootROError("response_too_large", "Coroot returned too much data.", "Use a narrower time window.")
                return HttpResponse(status_code=response.status, json_data=_decode_json(body))
        except urllib.error.HTTPError as exc:
            return HttpResponse(status_code=exc.code, json_data={})
        except urllib.error.URLError as exc:
            raise CorootROError("backend_unavailable", "Coroot is unavailable.", "Retry within the current observation window.") from exc


class CorootROService:
    """Safe projection of Coroot metrics, trace, and log observations."""

    def __init__(self, config: RuntimeConfig | None = None, transport: CorootTransport | None = None) -> None:
        # Read on first use, not at start-up: the server is registered for
        # every Trial, so a missing Coroot setting must fail the call that
        # needs it rather than the whole Trial.
        self._config = config
        self.transport = transport if transport is not None else UrlLibCorootTransport()

    @property
    def config(self) -> RuntimeConfig:
        if self._config is None:
            self._config = RuntimeConfig.from_env()
        return self._config

    async def metrics_range(self, *, metric: str, start: int, end: int, labels: Mapping[str, str] | None = None) -> dict[str, Any]:
        """Query Coroot's authenticated dashboard panel API with an injected namespace matcher."""

        self._window(start=start, end=end)
        query = _metric_query(metric=metric, namespace=self.config.scope.namespace, labels=labels or {})
        chart_response = await self._get(
            path=self._project_path("panel", "data"),
            params={
                "from": start * 1000,
                "to": end * 1000,
                "query": _dashboard_panel_query(query),
            },
        )
        series_response = await self._get(
            path=self._project_path("prom", "api", "v1", "series"),
            params={"match[]": query, "start": start, "end": end},
        )
        payload = _coroot_chart_matrix(chart_response, series_response)
        return envelope(
            {
                "metric": metric,
                "start": start,
                "end": end,
                "query_scope": {
                    "namespace_matcher_injected": True,
                    "application_path_scope": False,
                    "backend": "coroot_panel_data_with_prom_series_metadata",
                },
                "data": payload,
            }
        )

    async def traces_find(self, *, service: str, start: int, end: int, min_duration_ms: int = 0) -> dict[str, Any]:
        """Read Coroot's Controller-selected application trace view and retain matching spans."""

        self._window(start=start, end=end)
        _validate_service(service, self.config.allowed_services)
        if not 0 <= min_duration_ms <= 3_600_000:
            raise CorootROError("invalid_min_duration", "min_duration_ms must be between zero and one hour.", "Use a bounded duration threshold.")
        duration_seconds = min_duration_ms / 1000
        response = await self._get(
            path=self._application_path("tracing"),
            params={
                "from": start * 1000,
                "to": end * 1000,
                # Coroot's application trace view accepts source:id:timestamp-range:duration-range:span.
                "trace": f"::{start * 1000}-{end * 1000}:{duration_seconds:g}-",
            },
        )
        view = _coroot_view(response, "Coroot trace view")
        spans = _nullable_sequence(view.get("spans"), "Coroot trace spans")
        matched = [
            item
            for item in spans
            if isinstance(item, Mapping)
            and item.get("service") == service
            and _duration_ms(item.get("duration")) >= min_duration_ms
        ][:MAX_TRACES]
        return envelope(
            {
                "service": service,
                "start": start,
                "end": end,
                "min_duration_ms": min_duration_ms,
                "query_scope": {"namespace_matcher_injected": False, "application_path_scope": True},
                "backend_status": view.get("status"),
                "backend_message": view.get("message"),
                "backend_sources": view.get("sources"),
                "traces": matched,
                "truncated": len(spans) > len(matched) or len(matched) == MAX_TRACES,
            }
        )

    async def logs_range(self, *, service: str, start: int, end: int, pattern: str | None = None) -> dict[str, Any]:
        """Read bounded application logs and apply an optional local regular-expression filter."""

        self._window(start=start, end=end)
        _validate_service(service, self.config.allowed_services)
        matcher = _pattern(pattern)
        response = await self._get(
            path=self._application_path("logs"),
            params={
                "from": start * 1000,
                "to": end * 1000,
                "query": json.dumps({"view": "messages", "limit": MAX_LOG_ENTRIES}, separators=(",", ":")),
            },
        )
        view = _coroot_view(response, "Coroot log view")
        entries = _nullable_sequence(view.get("entries"), "Coroot log entries")
        filtered = [
            entry
            for entry in entries
            if isinstance(entry, Mapping)
            and (matcher is None or matcher.search(str(entry.get("message", ""))) is not None)
        ][:MAX_LOG_ENTRIES]
        return envelope(
            {
                "service": service,
                "start": start,
                "end": end,
                "pattern_applied": matcher is not None,
                "query_scope": {"namespace_matcher_injected": False, "application_path_scope": True},
                "backend_status": view.get("status"),
                "backend_message": view.get("message"),
                "backend_source": view.get("source"),
                "backend_sources": view.get("sources"),
                "entries": filtered,
                "truncated": len(entries) > len(filtered) or len(filtered) == MAX_LOG_ENTRIES,
            }
        )

    async def _get(self, *, path: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        if not self._anonymous_read():
            await self._verify_viewer_session()
        response = await self._request(path=path, params=params)
        if response.status_code in {401, 403}:
            raise CorootROError(
                "backend_authorization_unqualified",
                "Coroot denied the configured Viewer session.",
                "Refresh the Controller-managed Coroot session cookie before running this Trial.",
            )
        if response.status_code == 404:
            raise CorootROError(
                "backend_endpoint_unsupported",
                "This Coroot deployment does not expose the required read-only endpoint.",
                "Complete Coroot API qualification before using this observation service.",
            )
        if response.status_code < 200 or response.status_code >= 300:
            raise CorootROError("backend_error", "Coroot rejected the read-only query.", "Check the configured Coroot project and application scope.")
        if not isinstance(response.json_data, Mapping):
            raise CorootROError("invalid_backend_response", "Coroot returned an invalid JSON object.", "Complete Coroot API qualification before using this service.")
        return response.json_data

    def _anonymous_read(self) -> bool:
        return self.config.allow_anonymous_read and self.config.session_cookie is None

    async def _verify_viewer_session(self) -> None:
        response = await self._request(path="/api/user", params={})
        if response.status_code in {401, 403}:
            raise CorootROError(
                "backend_authorization_unqualified",
                "Coroot rejected the configured session cookie.",
                "Refresh the Controller-managed Coroot Viewer session before running this Trial.",
            )
        if response.status_code < 200 or response.status_code >= 300:
            raise CorootROError("backend_error", "Coroot rejected the identity verification request.", "Check the configured Coroot URL and session.")
        if not isinstance(response.json_data, Mapping):
            raise CorootROError("invalid_backend_response", "Coroot returned an invalid user response.", "Complete Coroot API qualification before using this service.")
        if response.json_data.get("anonymous") is not False:
            raise CorootROError(
                "anonymous_identity_forbidden",
                "Coroot did not prove a non-anonymous user identity.",
                "Disable anonymous Admin/Viewer mode for Stage2 qualification or use a real Viewer session.",
            )
        role = response.json_data.get("role")
        if role != COROOT_VIEWER_ROLE:
            raise CorootROError(
                "readonly_identity_unqualified",
                "Coroot session is not a non-anonymous Viewer identity.",
                "Provision a Controller-managed Coroot user with exactly the Viewer role.",
            )

    async def _request(self, *, path: str, params: Mapping[str, Any]) -> HttpResponse:
        headers: dict[str, str] = {}
        if not self._anonymous_read():
            session_cookie = _session_cookie(self.config.session_cookie)
            headers["Cookie"] = f"coroot_session={session_cookie}"
        return await self.transport.get_json(
            base_url=self.config.base_url,
            path=path,
            params=params,
            headers=headers,
            timeout_seconds=self.config.timeout_seconds,
            max_bytes=MAX_RESPONSE_BYTES,
        )

    def _window(self, *, start: int, end: int) -> None:
        try:
            self.config.scope.validate_window(start=start, end=end)
        except ScopeError as exc:
            raise CorootROError(exc.code, exc.message, exc.action) from exc

    def _project_path(self, *parts: str) -> str:
        return "/api/project/" + _path_part(self.config.project_id) + "/" + "/".join(_path_part(part) for part in parts)

    def _application_path(self, leaf: str) -> str:
        return self._project_path("app", self.config.scope.application, leaf)


def _base_url(value: str | None) -> str:
    raw = (value or "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(raw)
    if (parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query
            or parsed.fragment or parsed.username or parsed.password):
        raise CorootROError("invalid_coroot_url", "Coroot URL must be an HTTP(S) base URL without query data.", f"Set {COROOT_URL_ENV} to the Controller-managed Coroot URL.")
    return raw


def _required_identifier(value: str | None, env_name: str) -> str:
    candidate = (value or "").strip()
    if not candidate or len(candidate) > 512 or "\x00" in candidate:
        raise CorootROError("missing_coroot_scope", "A required Coroot scope value is absent or invalid.", f"Set {env_name} from the Controller.")
    return candidate


def _services(value: str | None) -> frozenset[str]:
    services = frozenset(item.strip() for item in (value or "").replace(",", " ").split() if item.strip())
    if not services or any(not _SERVICE_RE.fullmatch(service) for service in services):
        raise CorootROError("invalid_service_scope", "coroot_ro requires a non-empty Controller service allowlist.", f"Set {COROOT_ALLOWED_SERVICES_ENV} to bounded service names.")
    return services


def _session_cookie(value: str | None) -> str:
    if value is None or value == "":
        raise CorootROError(
            "missing_readonly_identity",
            "Coroot requires a configured non-anonymous Viewer session.",
            f"Set {COROOT_SESSION_COOKIE_ENV} to the Controller-managed coroot_session value.",
        )
    cookie = value
    if cookie != cookie.strip():
        raise CorootROError(
            "invalid_readonly_identity",
            "Coroot session must not contain leading or trailing whitespace.",
            f"Set only the raw coroot_session value in {COROOT_SESSION_COOKIE_ENV}.",
        )
    if "coroot_session" in cookie or any(char in cookie for char in "\r\n;\t ,"):
        raise CorootROError(
            "invalid_readonly_identity",
            "Coroot session must be a single coroot_session cookie value, not a Cookie header.",
            f"Set only the raw coroot_session value in {COROOT_SESSION_COOKIE_ENV}.",
        )
    if not _SESSION_COOKIE_RE.fullmatch(cookie):
        raise CorootROError(
            "invalid_readonly_identity",
            "Coroot session cookie value has an unexpected format.",
            "Refresh the Controller-managed Coroot Viewer session.",
        )
    return cookie


def _metric_query(*, metric: str, namespace: str, labels: Mapping[str, str]) -> str:
    if not _METRIC_RE.fullmatch(metric):
        raise CorootROError("invalid_metric", "metric must be a Prometheus metric identifier.", "Use a metric name without an expression.")
    if len(labels) > MAX_LABELS:
        raise CorootROError("too_many_labels", "Too many metric label filters were requested.", "Use at most twelve exact label filters.")
    if metric.startswith("container_"):
        # Coroot's node agent labels container series by container_id
        # ("/k8s/<namespace>/<pod>/<container>") and has no namespace label.
        matchers = [f'container_id=~"/k8s/{_prometheus_literal(namespace)}/.*"']
    else:
        matchers = [f'namespace="{_prometheus_literal(namespace)}"']
    for key, value in sorted(labels.items()):
        if key in _NAMESPACE_LABELS:
            raise CorootROError("namespace_override", "The namespace matcher is Controller-owned.", "Do not supply a namespace label; it is injected automatically.")
        if not _LABEL_RE.fullmatch(key) or not isinstance(value, str) or len(value) > 256:
            raise CorootROError("invalid_label", "Metric labels must be bounded exact string filters.", "Use valid label names and values.")
        matchers.append(f'{key}="{_prometheus_literal(value)}"')
    return f"{metric}" + "{" + ",".join(matchers) + "}"


def _dashboard_panel_query(promql: str) -> str:
    panel = {
        "name": "Stage2 read-only metric range",
        "description": "Controller-scoped read-only PromQL range query.",
        "source": {"metrics": {"queries": [{"datasource": "", "query": promql, "legend": "", "color": ""}]}},
        "widget": {"chart": {"display": "line", "stacked": False}},
        "box": {"x": 0, "y": 0, "w": 12, "h": 4},
    }
    return json.dumps(panel, separators=(",", ":"))


def _prometheus_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _validate_service(service: str, allowed: frozenset[str]) -> None:
    if not _SERVICE_RE.fullmatch(service):
        raise CorootROError("invalid_service", "service is invalid.", "Use a service from the current Controller scope.")
    if service not in allowed:
        raise CorootROError("service_out_of_scope", "The requested service is outside the Controller scope.", "Use one of the approved application services.")


def _pattern(value: str | None) -> re.Pattern[str] | None:
    if value is None or not value.strip():
        return None
    if len(value) > 200:
        raise CorootROError("pattern_too_long", "Log pattern is too long.", "Use a pattern of at most 200 characters.")
    try:
        return re.compile(value)
    except re.error as exc:
        raise CorootROError("invalid_pattern", "Log pattern is not a valid regular expression.", "Use a valid bounded regular expression.") from exc


def _path_part(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _decode_json(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CorootROError("invalid_backend_response", "Coroot returned invalid JSON.", "Complete Coroot API qualification before using this service.") from exc


def _sequence(value: Any, description: str) -> list[Any]:
    if not isinstance(value, list):
        raise CorootROError("invalid_backend_response", f"Coroot did not return {description}.", "Complete Coroot API qualification before using this service.")
    return value


def _nullable_sequence(value: Any, description: str) -> list[Any]:
    if value is None:
        return []
    return _sequence(value, description)


def _duration_ms(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return -1.0


def _coroot_view(payload: Mapping[str, Any], description: str) -> Mapping[str, Any]:
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise CorootROError(
            "invalid_backend_response",
            f"Coroot did not return a native {description} wrapper.",
            "Complete Coroot API qualification before using this service.",
        )
    return data


def _coroot_chart_matrix(payload: Mapping[str, Any], series_payload: Mapping[str, Any]) -> dict[str, Any]:
    chart = payload.get("chart")
    if chart is None:
        return {"resultType": "matrix", "result": [], "coroot_chart_context": None, "truncated": False}
    if not isinstance(chart, Mapping):
        raise CorootROError("invalid_backend_response", "Coroot returned an invalid chart.", "Complete Coroot API qualification before using this service.")
    ctx = chart.get("ctx")
    if not isinstance(ctx, Mapping):
        raise CorootROError("invalid_backend_response", "Coroot chart is missing context.", "Complete Coroot API qualification before using this service.")
    from_ms = _number(ctx.get("from"), "Coroot chart ctx.from")
    to_ms = _number(ctx.get("to"), "Coroot chart ctx.to") if ctx.get("to") is not None else None
    step_ms = _number(ctx.get("step"), "Coroot chart ctx.step")
    if step_ms <= 0:
        raise CorootROError("invalid_backend_response", "Coroot chart step is invalid.", "Complete Coroot API qualification before using this service.")
    series = _sequence(chart.get("series"), "Coroot chart series")
    metadata = _series_metadata_by_coroot_name(series_payload)
    result: list[dict[str, Any]] = []
    for item in series:
        if not isinstance(item, Mapping):
            raise CorootROError("invalid_backend_response", "Coroot chart contains an invalid series.", "Complete Coroot API qualification before using this service.")
        data = _sequence(item.get("data"), "Coroot chart series data")
        series_name = item.get("name")
        if series_name is not None and not isinstance(series_name, str):
            raise CorootROError("invalid_backend_response", "Coroot chart series name is invalid.", "Complete Coroot API qualification before using this service.")
        values = []
        for index, value in enumerate(data):
            timestamp_ms = from_ms + index * step_ms
            if to_ms is not None and timestamp_ms > to_ms:
                # Coroot fills its step grid from ``from`` and includes the
                # bucket that contains ``to``, so for a window that is not a
                # multiple of the step the last point lands less than one step
                # past ``to`` (a 44 s window over a 15 s step ends 1 s past
                # it). Drop that point; a point a full step or more past
                # ``to`` means the chart does not match the request.
                if timestamp_ms >= to_ms + step_ms:
                    raise CorootROError("invalid_backend_response", "Coroot chart series extends past ctx.to.", "Complete Coroot API qualification before using this service.")
                continue
            values.append([int(timestamp_ms / 1000), _chart_sample_value(value)])
        matched_metadata = metadata.get(series_name or "")
        result.append(
            {
                "metric": dict(matched_metadata) if matched_metadata is not None else {},
                "values": values,
                "coroot_series": {
                    key: value
                    for key in ("name", "title", "color", "fill", "threshold", "value")
                    if (value := item.get(key)) not in (None, "")
                },
            }
        )
    return {
        "resultType": "matrix",
        "context": {
            "from": int(from_ms / 1000),
            "to": int(to_ms / 1000) if to_ms is not None else None,
            "step": int(step_ms / 1000),
            "raw_step": int(_number(ctx.get("raw_step"), "Coroot chart ctx.raw_step") / 1000) if ctx.get("raw_step") is not None else None,
        },
        "truncated": bool(ctx.get("truncated")) if isinstance(ctx.get("truncated"), bool) else False,
        "result": result,
    }


def _number(value: Any, description: str) -> float:
    if isinstance(value, bool):
        raise CorootROError("invalid_backend_response", f"{description} is invalid.", "Complete Coroot API qualification before using this service.")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise CorootROError("invalid_backend_response", f"{description} is invalid.", "Complete Coroot API qualification before using this service.") from exc
    if not math.isfinite(number):
        raise CorootROError("invalid_backend_response", f"{description} is not finite.", "Complete Coroot API qualification before using this service.")
    return number


def _chart_sample_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        raise CorootROError("invalid_backend_response", "Coroot chart sample value is invalid.", "Complete Coroot API qualification before using this service.")
    if isinstance(value, (int, float)) and not math.isfinite(float(value)):
        raise CorootROError("invalid_backend_response", "Coroot chart sample value is not finite.", "Complete Coroot API qualification before using this service.")
    return value


def _series_metadata_by_coroot_name(payload: Mapping[str, Any]) -> dict[str, Mapping[str, str]]:
    if payload.get("status") != "success":
        raise CorootROError("invalid_backend_response", "Coroot series metadata query did not succeed.", "Complete Coroot API qualification before using this service.")
    rows = _sequence(payload.get("data"), "Coroot series metadata")
    by_name: dict[str, Mapping[str, str] | None] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise CorootROError("invalid_backend_response", "Coroot series metadata contains an invalid row.", "Complete Coroot API qualification before using this service.")
        labels: dict[str, str] = {}
        for key, value in row.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise CorootROError("invalid_backend_response", "Coroot series metadata labels must be strings.", "Complete Coroot API qualification before using this service.")
            labels[key] = value
        coroot_name = _coroot_labels_string(labels)
        if coroot_name in by_name:
            by_name[coroot_name] = None
        else:
            by_name[coroot_name] = labels
    return {name: labels for name, labels in by_name.items() if labels is not None}


def _coroot_labels_string(labels: Mapping[str, str]) -> str:
    if not labels:
        return ""
    return "{" + ",".join(f"{key}={labels[key]}" for key in sorted(labels)) + "}"
