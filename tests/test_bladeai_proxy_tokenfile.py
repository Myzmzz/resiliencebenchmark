"""Exercise the real proxy backend against Kubernetes-style rotating token files."""

from __future__ import annotations

import asyncio
import base64
import json
from email.message import Message
from pathlib import Path
from unittest.mock import Mock

import pytest

from mcp_servers.bladeai_k8s_proxy import server


@pytest.fixture
def projected_backend(tmp_path, monkeypatch):
    """Only TLS/network are substituted; credential loading is production code."""
    volume = tmp_path / "serviceaccount"
    volume.mkdir()
    for version, value in (("..first", "first-token"), ("..second", "second-token")):
        generation = volume / version
        generation.mkdir()
        (generation / "token").write_text(value)
        (generation / "token").chmod(0o640)
    (volume / "..data").symlink_to("..first", target_is_directory=True)
    token = volume / "token"
    token.symlink_to("..data/token")
    # Match deployment: the private kubeconfig and mounted token live in
    # separate directory trees; only the token's own volume bounds its links.
    config_root = tmp_path / "controller-private"
    config_root.mkdir(mode=0o700)
    config = config_root / "controller.kubeconfig"
    config.write_text(json.dumps({
        "current-context": "controller",
        "contexts": [{"name": "controller", "context": {"cluster": "cluster", "user": "controller"}}],
        "clusters": [{"name": "cluster", "cluster": {
            "server": "https://kubernetes.test", "certificate-authority-data": base64.b64encode(b"test-ca").decode(),
        }}],
        "users": [{"name": "controller", "user": {"tokenFile": str(token)}}],
    }))
    config.chmod(0o600)
    monkeypatch.setattr(server.ssl, "create_default_context", lambda: Mock())
    calls = []

    class Response:
        status = 200
        headers = Message()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"items":[]}'

    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            return Response()

    monkeypatch.setattr(server.urllib.request, "build_opener", lambda *_args: Opener())
    return config, volume, calls


def test_proxy_reads_projected_token_and_reloads_after_atomic_rotation(projected_backend):
    config, volume, calls = projected_backend
    backend = server.KubernetesKubeconfigBackend(config)
    try:
        asyncio.run(backend.request("GET", "/api/v1/namespaces/otel-demo/pods", {}))
        (volume / "..next").symlink_to("..second", target_is_directory=True)
        (volume / "..next").replace(volume / "..data")
        asyncio.run(backend.request("GET", "/api/v1/namespaces/otel-demo/pods", {}))
        assert [request.get_header("Authorization") for request in calls] == [
            "Bearer first-token", "Bearer second-token",
        ]
    finally:
        backend.close()


@pytest.mark.parametrize("mode", [0o644, 0o660, 0o666])
def test_proxy_rejects_public_or_group_writable_token(projected_backend, mode):
    config, volume, calls = projected_backend
    (volume / "..first/token").chmod(mode)
    with pytest.raises(server.ProxyRuntimeError, match="tokenFile"):
        server.KubernetesKubeconfigBackend(config)
    assert calls == []


def test_proxy_rejects_token_symlink_outside_credential_directory(projected_backend, tmp_path):
    config, volume, calls = projected_backend
    outside = tmp_path / "outside-token"
    outside.write_text("never-use-this-token")
    outside.chmod(0o600)
    (volume / "token").unlink()
    (volume / "token").symlink_to(outside)
    with pytest.raises(server.ProxyRuntimeError, match="tokenFile"):
        server.KubernetesKubeconfigBackend(config)
    assert calls == []


def test_proxy_does_not_keep_using_stale_token_if_rotation_becomes_unreadable(projected_backend):
    config, volume, calls = projected_backend
    backend = server.KubernetesKubeconfigBackend(config)
    try:
        (volume / "..first/token").write_text("\n")
        with pytest.raises(server.KubernetesBackendError, match="credential is unavailable"):
            asyncio.run(backend.request("GET", "/api/v1/namespaces/otel-demo/pods", {}))
        assert calls == []
    finally:
        backend.close()
