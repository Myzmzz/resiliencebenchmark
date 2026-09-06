"""Trial-scoped inference relay for untrusted Harness processes.

The relay deliberately exposes only the three request protocols used by the
current Stage-2 Harness templates.  It is not a LiteLLM proxy replacement and
never exposes upstream credentials, model administration, or request history.
"""

from __future__ import annotations

import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route


RELAY_HOST = "127.0.0.1"
RELAY_PORT = 18090
MAX_REQUEST_BYTES = 4 * 1024 * 1024
ALLOWED_POST_PATHS = frozenset(
    {"/v1/chat/completions", "/v1/responses", "/v1/messages"}
)
FORBIDDEN_TOP_LEVEL_ROUTING_FIELDS = frozenset({
    "api_base", "base_url", "api_key", "custom_api_key", "authorization",
    "extra_headers", "headers", "custom_llm_provider", "litellm_params",
    "fallbacks", "proxy_server_request", "router", "deployment_id",
    "api_version", "azure_endpoint", "azure_ad_token", "endpoint", "url",
    "model_list", "provider", "aws_access_key_id", "aws_secret_access_key",
    "aws_session_token", "region_name",
})


@dataclass(frozen=True)
class TrialRelayConfig:
    trial_id: str
    model_alias: str
    upstream_base_url: str
    upstream_api_key: str
    relay_token: str
    harness_name: str = "unknown"
    gateway_config_sha256: str = ""
    request_ids: list[str] = field(default_factory=list, compare=False)
    request_timeout_seconds: float = 180.0
    max_request_bytes: int = MAX_REQUEST_BYTES
    host: str = RELAY_HOST
    port: int = RELAY_PORT
    allow_ephemeral_port_for_tests: bool = False

    @classmethod
    def issue(
        cls,
        *,
        trial_id: str,
        model_alias: str,
        upstream_base_url: str,
        upstream_api_key: str,
        harness_name: str = "unknown",
        gateway_config_sha256: str = "",
        relay_token: str | None = None,
        request_timeout_seconds: float = 180.0,
        max_request_bytes: int = MAX_REQUEST_BYTES,
    ) -> "TrialRelayConfig":
        if not trial_id or not model_alias:
            raise ValueError("trial_id and model_alias are required")
        if not upstream_base_url.startswith(("http://", "https://")):
            raise ValueError("upstream_base_url must be an absolute HTTP(S) URL")
        if not upstream_api_key:
            raise ValueError("upstream_api_key is required")
        return cls(
            trial_id=trial_id,
            model_alias=model_alias,
            upstream_base_url=upstream_base_url.rstrip("/"),
            upstream_api_key=upstream_api_key,
            relay_token=relay_token or secrets.token_urlsafe(32),
            harness_name=harness_name,
            gateway_config_sha256=gateway_config_sha256,
            request_timeout_seconds=request_timeout_seconds,
            max_request_bytes=max_request_bytes,
        )

    def agent_environment(self) -> dict[str, str]:
        """The only model settings supplied to the untrusted Harness process."""
        return {
            "RESBENCH_LLM_BASE_URL": f"http://{self.host}:{self.port}/v1",
            "RESBENCH_LLM_API_KEY": self.relay_token,
        }


def create_trial_relay_app(
    config: TrialRelayConfig,
    *,
    client_factory: Callable[[], httpx.AsyncClient] | None = None,
) -> Starlette:
    """Create one relay instance per Trial; do not share it across Trials."""
    if config.host != RELAY_HOST or (
        config.port != RELAY_PORT
        and not (config.allow_ephemeral_port_for_tests and config.port == 0)
    ):
        raise ValueError("the inference relay must bind only 127.0.0.1:18090")
    factory = client_factory or (lambda: httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(config.request_timeout_seconds),
    ))

    async def infer(request: Request) -> Response:
        if not _authorized(request, config.relay_token):
            return JSONResponse({"error": {"message": "unauthorized"}}, status_code=401)
        # The pinned Claude client uses this exact inference URL. This is not
        # permission to forward arbitrary query-based routing or credentials.
        if request.url.query and not (
            request.url.path == "/v1/messages" and request.url.query == "beta=true"
        ):
            return _error(400, "query_parameters_not_allowed")
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > config.max_request_bytes:
                    return _error(413, "request_too_large")
            except ValueError:
                return _error(400, "invalid_content_length")
        chunks: list[bytes] = []
        total_bytes = 0
        async for chunk in request.stream():
            total_bytes += len(chunk)
            if total_bytes > config.max_request_bytes:
                return _error(413, "request_too_large")
            chunks.append(chunk)
        body = b"".join(chunks)
        try:
            # Starlette's Request.json is async and consumes the same cached body;
            # parse via stdlib to keep validation independent of content headers.
            import json
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return _error(400, "invalid_json")
        if not isinstance(payload, dict) or payload.get("model") != config.model_alias:
            return _error(403, "model_not_authorized")
        if _has_forbidden_routing_field(payload):
            return _error(403, "request_routing_parameter_forbidden")
        upstream_url = _upstream_url(config.upstream_base_url, request.url.path)
        if request.url.query:
            upstream_url += "?beta=true"
        request_id = secrets.token_hex(16)
        config.request_ids.append(request_id)
        headers = {
            "authorization": f"Bearer {config.upstream_api_key}",
            "content-type": "application/json",
            "accept": request.headers.get("accept", "application/json"),
            "x-resbench-trial-id": config.trial_id,
            "x-resbench-harness": config.harness_name,
            "x-resbench-model-alias": config.model_alias,
            "x-resbench-request-id": request_id,
            "x-resbench-gateway-config-sha256": config.gateway_config_sha256,
        }
        if request.url.path == "/v1/messages":
            for name in ("anthropic-version", "anthropic-beta"):
                if name in request.headers:
                    headers[name] = request.headers[name]
        client = factory()
        try:
            upstream = await client.send(
                client.build_request("POST", upstream_url, content=body, headers=headers),
                stream=True,
            )
        except httpx.TimeoutException:
            await client.aclose()
            return _error(504, "upstream_timeout")
        except httpx.RequestError:
            await client.aclose()
            return _error(502, "upstream_unavailable")
        if 300 <= upstream.status_code < 400:
            await upstream.aclose()
            await client.aclose()
            return _error(502, "upstream_redirect_rejected")

        async def stream_body() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_raw():
                    if await request.is_disconnected():
                        break
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        response_headers = {
            key: value
            for key, value in upstream.headers.items()
            if key.lower() in {"content-type", "cache-control", "x-request-id"}
        }
        return StreamingResponse(
            stream_body(), status_code=upstream.status_code, headers=response_headers
        )

    return Starlette(
        routes=[Route(path, infer, methods=["POST"]) for path in sorted(ALLOWED_POST_PATHS)]
    )


def relay_uvicorn_config(config: TrialRelayConfig):
    """Controller entry point for a per-Trial loopback-only uvicorn process."""
    import uvicorn

    return uvicorn.Config(
        create_trial_relay_app(config),
        host=RELAY_HOST,
        port=RELAY_PORT,
        access_log=False,
        log_level="warning",
    )


class TrialRelay:
    """Own a loopback relay for exactly one Trial in the Controller process."""

    def __init__(
        self,
        config: TrialRelayConfig,
        *,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        startup_timeout_seconds: float = 5.0,
        shutdown_timeout_seconds: float = 5.0,
    ) -> None:
        self.config = config
        self.client_factory = client_factory
        self.startup_timeout_seconds = startup_timeout_seconds
        self.shutdown_timeout_seconds = shutdown_timeout_seconds
        self._socket: socket.socket | None = None
        self._server = None
        self._thread: threading.Thread | None = None
        self._bound_port: int | None = None

    def __enter__(self) -> "TrialRelay":
        return self.start()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def start(self) -> "TrialRelay":
        if self._thread is not None:
            raise RuntimeError("Trial relay has already been started")
        # Binding before uvicorn starts makes an occupied port a definite
        # startup failure, rather than accidentally using a foreign relay.
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.config.host, self.config.port))
            sock.listen(128)
        except OSError as exc:
            sock.close()
            raise RuntimeError("Trial relay loopback bind failed") from exc
        self._socket = sock
        self._bound_port = int(sock.getsockname()[1])
        import uvicorn

        try:
            app = create_trial_relay_app(self.config, client_factory=self.client_factory)
            self._server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host=self.config.host,
                    port=self._bound_port,
                    access_log=False,
                    log_level="warning",
                )
            )
        except Exception:
            self._close_socket()
            raise
        self._thread = threading.Thread(
            target=self._server.run,
            kwargs={"sockets": [sock]},
            name=f"trial-llm-relay-{self.config.trial_id}",
            daemon=True,
        )
        self._thread.start()
        deadline = time.monotonic() + self.startup_timeout_seconds
        while time.monotonic() < deadline:
            if self._server.started:
                return self
            if not self._thread.is_alive():
                self._close_socket()
                raise RuntimeError("Trial relay terminated during startup")
            time.sleep(0.01)
        self._server.should_exit = True
        self._thread.join(timeout=self.shutdown_timeout_seconds)
        self._close_socket()
        raise RuntimeError("Trial relay startup timed out")

    def close(self) -> None:
        if self._thread is None:
            self._close_socket()
            return
        if self._server is not None:
            self._server.should_exit = True
        self._thread.join(timeout=self.shutdown_timeout_seconds)
        self._close_socket()
        if self._thread.is_alive():
            raise RuntimeError("Trial relay did not terminate before the next Trial")
        self._thread = None
        self._server = None
        self._bound_port = None

    @property
    def port(self) -> int:
        if self._bound_port is None:
            raise RuntimeError("Trial relay is not running")
        return self._bound_port

    def agent_environment(self) -> dict[str, str]:
        return {
            "RESBENCH_LLM_BASE_URL": f"http://{RELAY_HOST}:{self.port}/v1",
            "RESBENCH_LLM_API_KEY": self.config.relay_token,
        }

    def _close_socket(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None


def _authorized(request: Request, token: str) -> bool:
    value = request.headers.get("authorization")
    expected = f"Bearer {token}"
    return value is not None and secrets.compare_digest(value, expected)


def _error(status_code: int, code: str) -> JSONResponse:
    return JSONResponse({"error": {"message": code}}, status_code=status_code)


def _upstream_url(base_url: str, request_path: str) -> str:
    """Preserve an existing `/v1` gateway prefix exactly once."""
    base = base_url.rstrip("/")
    suffix = request_path
    if base.endswith("/v1") and request_path.startswith("/v1/"):
        suffix = request_path[3:]
    return f"{base}{suffix}"


def _has_forbidden_routing_field(payload: dict[str, object]) -> bool:
    return any(str(key).lower() in FORBIDDEN_TOP_LEVEL_ROUTING_FIELDS for key in payload)
