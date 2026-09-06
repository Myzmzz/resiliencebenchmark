"""Loopback-only HTTP endpoint for BladeAI's constrained Kubernetes view.

BladeAI requires a Kubernetes API endpoint in its kubeconfig.  This process is
that endpoint: the Agent receives only a Trial proxy bearer token, while the
Controller-owned kubeconfig remains private to this process.  All request
scope validation and realtime MCP auditing stay in :mod:`.service`.
"""

from __future__ import annotations

import asyncio
import base64
import os
import secrets
import ssl
import stat
import tempfile
import urllib.error
import urllib.request
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import uvicorn
import yaml
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from mcp_servers.http_runtime import PolicyGate

from .service import BladeAIKubernetesProxy, KubernetesReadBackend, ProxyConfig, ProxyError


KUBECONFIG_ENV = "RESBENCH_BLADEAI_PROXY_KUBECONFIG"
NAMESPACE_ENV = "RESBENCH_BLADEAI_PROXY_NAMESPACE"
TOKEN_ENV = "RESBENCH_BLADEAI_PROXY_TOKEN"
HOST_ENV = "RESBENCH_BLADEAI_PROXY_HOST"
PORT_ENV = "RESBENCH_BLADEAI_PROXY_PORT"
TIMEOUT_ENV = "RESBENCH_BLADEAI_PROXY_TIMEOUT_SECONDS"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18481


class ProxyRuntimeError(RuntimeError):
    """The Controller-owned proxy configuration is missing or unsafe."""


class KubernetesBackendError(RuntimeError):
    """Safe upstream failure; never convert it into a successful response."""


@dataclass(frozen=True)
class ProxyRuntimeConfig:
    namespace: str
    token: str
    kubeconfig: Path
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if self.host != DEFAULT_HOST:
            raise ProxyRuntimeError("BladeAI proxy must bind 127.0.0.1 only")
        if not 1024 <= self.port <= 65535:
            raise ProxyRuntimeError("BladeAI proxy port is invalid")
        if not 1 <= self.timeout_seconds <= 30:
            raise ProxyRuntimeError("BladeAI proxy timeout must be between 1 and 30 seconds")
        # ProxyConfig validates namespace/token as the Agent-visible boundary.
        ProxyConfig(namespace=self.namespace, token=self.token, listen_host=self.host, listen_port=self.port)
        _private_regular_file(self.kubeconfig, KUBECONFIG_ENV)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ProxyRuntimeConfig":
        values = os.environ if env is None else env
        raw_path = _required(values, KUBECONFIG_ENV)
        try:
            port = int(values.get(PORT_ENV, str(DEFAULT_PORT)))
            timeout = float(values.get(TIMEOUT_ENV, "10"))
        except ValueError as exc:
            raise ProxyRuntimeError("BladeAI proxy port or timeout is invalid") from exc
        return cls(
            namespace=_required(values, NAMESPACE_ENV),
            token=_required(values, TOKEN_ENV),
            kubeconfig=Path(raw_path),
            host=values.get(HOST_ENV, DEFAULT_HOST),
            port=port,
            timeout_seconds=timeout,
        )

    def proxy_config(self) -> ProxyConfig:
        return ProxyConfig(
            namespace=self.namespace,
            token=self.token,
            listen_host=self.host,
            listen_port=self.port,
        )


class KubernetesKubeconfigBackend(KubernetesReadBackend):
    """A read-only HTTPS client using one Controller-owned kubeconfig.

    The parser intentionally accepts only direct token or client-certificate
    credentials.  Exec/auth-provider plugins would execute arbitrary commands
    in the proxy process and are therefore rejected.  Redirects are disabled
    so the cluster credential can never be forwarded to a different origin.
    """

    def __init__(self, kubeconfig: Path, *, timeout_seconds: float = 10.0) -> None:
        _private_regular_file(kubeconfig, KUBECONFIG_ENV)
        self._temporary = tempfile.TemporaryDirectory(prefix="blade-k8s-", dir="/tmp")
        try:
            server, authorization, ssl_context = _load_kubeconfig(kubeconfig, Path(self._temporary.name))
        except BaseException:
            self._temporary.cleanup()
            raise
        parsed = urlsplit(server)
        if (parsed.scheme != "https" or not parsed.netloc or parsed.path not in {"", "/"}
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            self._temporary.cleanup()
            raise ProxyRuntimeError("Controller kubeconfig server must be a clean https URL")
        self._server = server.rstrip("/")
        self._authorization = authorization
        self._ssl_context = ssl_context
        self._timeout_seconds = timeout_seconds

    async def request(self, method: str, path: str, headers: Mapping[str, str]) -> tuple[int, bytes, Mapping[str, str]]:
        if method != "GET":
            raise KubernetesBackendError("proxy backend only permits GET")
        return await asyncio.to_thread(self._request_sync, path, dict(headers))

    def close(self) -> None:
        self._temporary.cleanup()

    def _request_sync(self, path: str, headers: dict[str, str]) -> tuple[int, bytes, Mapping[str, str]]:
        upstream_headers = dict(headers)
        if self._authorization:
            upstream_headers["Authorization"] = self._authorization
        request = urllib.request.Request(
            self._server + path,
            headers=upstream_headers,
            method="GET",
        )
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=self._ssl_context),
            _NoRedirect(),
        )
        try:
            with opener.open(request, timeout=self._timeout_seconds) as response:
                return int(response.status), response.read(), {"Content-Type": response.headers.get_content_type()}
        except urllib.error.HTTPError as exc:
            return int(exc.code), exc.read(), {"Content-Type": exc.headers.get_content_type()}
        except (OSError, ssl.SSLError, urllib.error.URLError) as exc:
            raise KubernetesBackendError("Controller Kubernetes API is unavailable") from exc


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_: object, **__: object) -> None:
        return None


def create_app(
    *,
    config: ProxyRuntimeConfig,
    backend: KubernetesReadBackend | None = None,
    policy_gate: PolicyGate | None = None,
) -> Starlette:
    """Build the concrete loopback proxy app; injectable only for local tests."""
    owned_backend = backend is None
    active_backend = backend or KubernetesKubeconfigBackend(
        config.kubeconfig, timeout_seconds=config.timeout_seconds,
    )
    proxy = BladeAIKubernetesProxy(
        config.proxy_config(), active_backend,
        policy_gate or PolicyGate.from_env("k8s_ro"),
    )

    async def route(request: Request) -> Response:
        supplied = request.headers.get("authorization", "")
        expected = f"Bearer {config.token}"
        if not secrets.compare_digest(supplied, expected):
            return _error(401, "UNAUTHORIZED", "proxy token is invalid")
        raw_path = request.scope.get("raw_path", b"")
        try:
            path = bytes(raw_path).decode("ascii")
            query = bytes(request.scope.get("query_string", b"")).decode("ascii")
        except UnicodeDecodeError:
            return _error(400, "INVALID_REQUEST", "request target must be ASCII")
        target = path + ("?" + query if query else "")
        try:
            status, body, headers = await proxy.forward(
                method=request.method, target=target, token=config.token,
            )
        except ProxyError as exc:
            return _error(400, "PROXY_REJECTED", str(exc))
        except KubernetesBackendError as exc:
            return _error(503, "KUBERNETES_UNAVAILABLE", str(exc))
        # Never reflect upstream headers other than content type and the
        # Controller receipt; the proxy token and Kubernetes auth are absent.
        response_headers = {
            key: value for key, value in headers.items()
            if key.lower() in {"content-type", "x-resbench-controller-call-id"}
        }
        return Response(content=body, status_code=status, headers=response_headers)

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        try:
            yield
        finally:
            if owned_backend and isinstance(active_backend, KubernetesKubeconfigBackend):
                active_backend.close()

    return Starlette(
        routes=[Route("/", route, methods=["GET"]), Route("/{path:path}", route, methods=["GET"])],
        lifespan=lifespan,
    )


def main() -> None:
    config = ProxyRuntimeConfig.from_env()
    uvicorn.run(create_app(config=config), host=config.host, port=config.port, access_log=False)


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error": {"code": code, "message": message}}, status_code=status)


def _required(values: Mapping[str, str], name: str) -> str:
    value = values.get(name, "").strip()
    if not value:
        raise ProxyRuntimeError(f"{name} is required")
    return value


def _private_regular_file(path: Path, name: str) -> None:
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise ProxyRuntimeError(f"{name} must be an absolute regular file")
    if path.stat().st_mode & 0o077:
        raise ProxyRuntimeError(f"{name} must not be group/world accessible")


def _load_kubeconfig(path: Path, temp_root: Path) -> tuple[str, str, ssl.SSLContext]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ProxyRuntimeError("Controller kubeconfig cannot be read") from exc
    if not isinstance(document, Mapping):
        raise ProxyRuntimeError("Controller kubeconfig is invalid")
    context_name = document.get("current-context")
    context = _named(document.get("contexts"), context_name, "context")
    context_data = context.get("context")
    if not isinstance(context_data, Mapping):
        raise ProxyRuntimeError("Controller kubeconfig context is invalid")
    cluster = _named(document.get("clusters"), context_data.get("cluster"), "cluster")
    cluster_data = cluster.get("cluster")
    user = _named(document.get("users"), context_data.get("user"), "user")
    user_data = user.get("user")
    if not isinstance(cluster_data, Mapping) or not isinstance(user_data, Mapping):
        raise ProxyRuntimeError("Controller kubeconfig cluster or user is invalid")
    server = cluster_data.get("server")
    if not isinstance(server, str):
        raise ProxyRuntimeError("Controller kubeconfig has no server")
    if cluster_data.get("insecure-skip-tls-verify") is True:
        raise ProxyRuntimeError("insecure Kubernetes TLS is forbidden")
    if "exec" in user_data or "auth-provider" in user_data:
        raise ProxyRuntimeError("executable Kubernetes authentication is forbidden")
    context = ssl.create_default_context()
    ca_data = cluster_data.get("certificate-authority-data")
    ca_file = cluster_data.get("certificate-authority")
    if isinstance(ca_data, str):
        context.load_verify_locations(cadata=_decode_pem(ca_data, "certificate-authority-data").decode("utf-8"))
    elif isinstance(ca_file, str):
        ca_path = (path.parent / ca_file).resolve() if not Path(ca_file).is_absolute() else Path(ca_file)
        _private_regular_file(ca_path, "certificate-authority")
        context.load_verify_locations(cafile=str(ca_path))
    else:
        raise ProxyRuntimeError("Controller kubeconfig requires a CA")
    token = user_data.get("token")
    token_file = user_data.get("tokenFile")
    if isinstance(token, str) and token.strip():
        return server, "Bearer " + token.strip(), context
    if isinstance(token_file, str) and token_file:
        candidate = (path.parent / token_file).resolve() if not Path(token_file).is_absolute() else Path(token_file)
        _private_regular_file(candidate, "tokenFile")
        token_value = candidate.read_text(encoding="utf-8").strip()
        if token_value:
            return server, "Bearer " + token_value, context
    certificate = user_data.get("client-certificate-data")
    key = user_data.get("client-key-data")
    if isinstance(certificate, str) and isinstance(key, str):
        certificate_path = temp_root / "client.crt"
        key_path = temp_root / "client.key"
        certificate_path.write_bytes(_decode_pem(certificate, "client-certificate-data"))
        key_path.write_bytes(_decode_pem(key, "client-key-data"))
        certificate_path.chmod(0o600)
        key_path.chmod(0o600)
        context.load_cert_chain(certfile=str(certificate_path), keyfile=str(key_path))
        return server, "", context
    raise ProxyRuntimeError("Controller kubeconfig requires token or client certificate authentication")


def _named(values: Any, name: Any, kind: str) -> Mapping[str, Any]:
    if not isinstance(name, str) or not isinstance(values, list):
        raise ProxyRuntimeError(f"Controller kubeconfig {kind} selection is invalid")
    for item in values:
        if isinstance(item, Mapping) and item.get("name") == name:
            return item
    raise ProxyRuntimeError(f"Controller kubeconfig {kind} was not found")


def _decode_pem(value: str, name: str) -> bytes:
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise ProxyRuntimeError(f"Controller kubeconfig {name} is invalid") from exc
