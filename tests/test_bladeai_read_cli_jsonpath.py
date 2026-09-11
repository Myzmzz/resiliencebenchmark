"""kubectl shim: jsonpath templates and ``cluster-info``.

Both defects were confirmed on the live cluster on 2026-09-10: a four-field
jsonpath template printed an empty line, and bladeai's startup probe
(``kubectl cluster-info``) was rejected, so bladeai concluded Kubernetes was
unavailable although reads worked through the proxy.
"""

from __future__ import annotations

import asyncio
import json
import runpy
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from mcp_servers.bladeai_k8s_proxy.service import BladeAIKubernetesProxy, ProxyConfig
from mcp_servers.http_runtime import PolicyGate
from stage2_service import bladeai_read_cli
from stage2_service.bladeai_read_cli import ReadCliError, main


pytestmark = pytest.mark.usefixtures("kubeconfig_path")

PROXY_TOKEN = "x" * 32
POD_PATH = "/api/v1/namespaces/otel-demo/pods/cart-abc"
POD_LIST_PATH = "/api/v1/namespaces/otel-demo/pods"
CLUSTER_INFO_PATH = "/api/v1/namespaces/otel-demo/pods?limit=1"
REACHABLE = (
    "Kubernetes control plane is reachable through the Stage-2 controlled read-only proxy (namespace otel-demo).\n"
)
KUBECTL_WRAPPER = Path(__file__).resolve().parents[1] / "harness" / "bladeai" / "kubectl-shim" / "kubectl"
# The four-field expression bladeai sent on 2026-09-10, shell quotes included.
BLADEAI_FOUR_FIELD_EXPRESSION = (
    """'{.metadata.name} {.metadata.uid} {.status.phase} {.status.conditions[?(@.type=="Ready")].status}'"""
)

CART_POD: dict[str, Any] = {
    "kind": "Pod",
    "metadata": {"name": "cart-abc", "uid": "uid-1", "labels": {"app": "cart"}},
    "spec": {"nodeName": "worker-a", "containers": [{"name": "cart", "image": "cart:1.0"}]},
    "status": {
        "phase": "Running",
        # "Ready" is neither the first condition nor the only "True" one, so a filter
        # that ignores its predicate (first item, or every item) cannot pass.
        "conditions": [
            {"type": "ContainersReady", "status": "Unknown"},
            {"type": "Ready", "status": "True"},
            {"type": "PodScheduled", "status": "True"},
        ],
        "containerStatuses": [{"name": "cart", "ready": True, "restartCount": 2}],
    },
}
CHECKOUT_POD: dict[str, Any] = {
    "kind": "Pod",
    "metadata": {"name": "checkout-def", "uid": "uid-2"},
    "spec": {"containers": [{"name": "checkout", "image": "checkout:2.0"}, {"name": "envoy", "image": "envoy:1.30"}]},
    "status": {"phase": "Pending"},
}
POD_LIST: dict[str, Any] = {"kind": "PodList", "items": [CART_POD, CHECKOUT_POD]}
EMPTY_POD_LIST: dict[str, Any] = {"kind": "PodList", "items": []}


class FakeTransport:
    """Serves canned API bodies by path and records every ``(path, token)`` read."""

    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str]] = []

    def get(self, path: str, token: str) -> bytes:
        self.calls.append((path, token))
        value = self.responses[path]
        return value if isinstance(value, bytes) else json.dumps(value).encode("utf-8")


class RefusingTransport:
    """Fails every read the way ``HTTPProxyTransport`` reports a proxy refusal."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, path: str, token: str) -> bytes:
        self.calls.append(path)
        raise ReadCliError("proxy rejected Kubernetes read with status 403")


class ProxyPolicyTransport:
    """Routes reads through the real BladeAI proxy policy before a fake API server answers."""

    def __init__(self, body: object) -> None:
        self.backend_calls: list[tuple[str, str]] = []
        self._body = json.dumps(body).encode("utf-8")
        self._proxy = BladeAIKubernetesProxy(
            ProxyConfig(namespace="otel-demo", token=PROXY_TOKEN),
            self,
            PolicyGate(server_name="k8s_ro", policy_file=None),
        )

    async def request(self, method: str, path: str, headers: dict[str, str]) -> tuple[int, bytes, dict[str, str]]:
        self.backend_calls.append((method, path))
        return 200, self._body, {"Content-Type": "application/json"}

    def get(self, path: str, token: str) -> bytes:
        status, body, _headers = asyncio.run(self._proxy.forward(method="GET", target=path, token=token))
        if status >= 400:
            raise ReadCliError(f"proxy rejected Kubernetes read with status {status}")
        return body


@pytest.fixture
def kubeconfig_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The per-trial proxy kubeconfig the harness injects through BLADE_AI_KUBECONFIG_PATH."""
    path = tmp_path / "bladeai-kubeconfig.json"
    path.write_text(
        json.dumps(
            {
                "apiVersion": "v1",
                "kind": "Config",
                "clusters": [{"name": "bladeai-loopback", "cluster": {"server": "http://127.0.0.1:18481"}}],
                "users": [{"name": "bladeai-trial", "user": {"token": PROXY_TOKEN}}],
                "contexts": [
                    {
                        "name": "bladeai-trial",
                        "context": {"cluster": "bladeai-loopback", "user": "bladeai-trial", "namespace": "otel-demo"},
                    }
                ],
                "current-context": "bladeai-trial",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BLADE_AI_KUBECONFIG_PATH", str(path))
    return path


def render_pod(template: str, capsys: pytest.CaptureFixture[str]) -> str:
    """Output of ``kubectl get pod cart-abc -o jsonpath=<template>`` as bladeai sees it."""
    transport = FakeTransport({POD_PATH: CART_POD})
    main(["get", "pod", "cart-abc", "-n", "otel-demo", "-o", f"jsonpath={template}"], transport=transport)
    return capsys.readouterr().out


def render_pod_list(template: str, capsys: pytest.CaptureFixture[str], pod_list: dict[str, Any] = POD_LIST) -> str:
    """Output of ``kubectl get pods -o jsonpath=<template>`` as bladeai sees it."""
    transport = FakeTransport({POD_LIST_PATH: pod_list})
    main(["get", "pods", "-n", "otel-demo", "-o", f"jsonpath={template}"], transport=transport)
    return capsys.readouterr().out


def run_kubectl_wrapper(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    proxy_get: Callable[[Any, str, str], bytes],
) -> int | str | None:
    """Run the in-container ``kubectl`` wrapper with the real transport class and return its exit code."""
    monkeypatch.setattr(sys, "argv", ["kubectl", *argv])
    monkeypatch.setattr(bladeai_read_cli.HTTPProxyTransport, "get", proxy_get)
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(KUBECTL_WRAPPER), run_name="__main__")
    return exit_info.value.code


# --- jsonpath -------------------------------------------------------------------


def test_bladeai_uid_expression_keeps_working_with_its_shell_quotes(capsys: pytest.CaptureFixture[str]) -> None:
    assert render_pod("'{.metadata.uid}'", capsys) == "uid-1\n"


def test_bladeai_four_field_expression_prints_every_field(capsys: pytest.CaptureFixture[str]) -> None:
    assert render_pod(BLADEAI_FOUR_FIELD_EXPRESSION, capsys) == "cart-abc uid-1 Running True\n"


@pytest.mark.parametrize("ready_filter", ["[?(@.type=='Ready')]", '[?( @.type == "Ready" )]'])
def test_equality_filter_accepts_single_quotes_and_spaces(
    capsys: pytest.CaptureFixture[str], ready_filter: str
) -> None:
    assert render_pod("{.status.conditions" + ready_filter + ".status}", capsys) == "True\n"


def test_range_with_quoted_literals_prints_one_line_per_item(capsys: pytest.CaptureFixture[str]) -> None:
    template = r"""{range .items[*]}{.metadata.name}{'\t'}{.status.phase}{"\n"}{end}"""
    assert render_pod_list(template, capsys) == "cart-abc\tRunning\ncheckout-def\tPending\n"


def test_nested_range_resolves_inner_paths_against_each_item(capsys: pytest.CaptureFixture[str]) -> None:
    # Same shape as bladeai's preflight operator check, including its {'\n'} literal.
    template = r"{range .items[*]}{.metadata.name}|{range .spec.containers[*]}{.image},{end}{'\n'}{end}"
    assert render_pod_list(template, capsys) == "cart-abc|cart:1.0,\ncheckout-def|checkout:2.0,envoy:1.30,\n"


def test_items_wildcard_joins_results_with_one_space(capsys: pytest.CaptureFixture[str]) -> None:
    assert render_pod_list("{.items[*].metadata.name}", capsys) == "cart-abc checkout-def\n"
    # checkout-def has no nodeName: like kubectl, it contributes no result rather than an empty slot.
    assert render_pod_list("{.items[*].spec.nodeName}", capsys) == "worker-a\n"


def test_missing_keys_render_empty(capsys: pytest.CaptureFixture[str]) -> None:
    assert render_pod("{.metadata.deletionTimestamp}", capsys) == "\n"
    assert render_pod("{.metadata.name}|{.spec.missing.deeper}|{.status.phase}", capsys) == "cart-abc||Running\n"


def test_legacy_pod_name_range_keeps_its_historical_output(capsys: pytest.CaptureFixture[str]) -> None:
    assert render_pod_list("'{range .items[*]}{.metadata.name} {end}'", capsys) == "cart-abc checkout-def\n"


def test_values_print_like_kubectl(capsys: pytest.CaptureFixture[str]) -> None:
    # bladeai's baseline_capture compares this output with the string "true".
    assert render_pod("{.status.containerStatuses[0].ready}", capsys) == "true\n"
    assert render_pod("{.status.containerStatuses[-1].restartCount}", capsys) == "2\n"
    assert render_pod("{.metadata.labels}", capsys) == '{"app":"cart"}\n'


def test_label_keys_with_dots_can_be_escaped_or_bracketed() -> None:
    # Every otel-demo label key is app.kubernetes.io/...; kubectl reads them either way.
    pod = {"metadata": {"labels": {"app.kubernetes.io/name": "cart", "app": "legacy"}}}
    for template in (
        r"{.metadata.labels.app\.kubernetes\.io/name}",
        "{.metadata.labels['app.kubernetes.io/name']}",
        '{.metadata.labels["app.kubernetes.io/name"]}',
    ):
        assert bladeai_read_cli._jsonpath(pod, template) == "cart"
    assert bladeai_read_cli._jsonpath(pod, "{.metadata.labels['app']}") == "legacy"
    assert bladeai_read_cli._jsonpath(pod, "{.metadata.labels['missing']}") == ""


def test_index_out_of_bounds_is_an_error_like_kubectl(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(ReadCliError, match="array index out of bounds: index 0, length 0"):
        render_pod_list("{.items[0].metadata.name}", capsys, EMPTY_POD_LIST)


@pytest.mark.parametrize(
    ("template", "detail"),
    [
        ('{.status.conditions[?(@.status!="True")].type}', "selector"),  # other filter operators
        ("{.status.conditions[?(@.status)].type}", "selector"),  # existence filter
        ("{.spec.containers[0:1].name}", "selector"),  # slices
        ("{..name}", "recursive descent"),
        ("{.spec.containers.length()}", "near"),  # functions
        ("{@}", "not a path"),
        ("{.metadata.name", "unbalanced '{'"),
        ("{.metadata.name}}", "unbalanced '}'"),
        ("{range .items[*]}{.metadata.name}", "without a matching {end}"),
        ("{.metadata.name}{end}", "without a matching {range}"),
        (r'{"\x41"}', "escape"),  # escapes other than \n, \t, \\ and quotes
        ("''", "empty template"),
    ],
)
def test_unsupported_syntax_is_an_error_not_a_blank_line(
    capsys: pytest.CaptureFixture[str], template: str, detail: str
) -> None:
    with pytest.raises(ReadCliError, match="^jsonpath expression is unsupported: ") as error:
        render_pod(template, capsys)
    assert detail in str(error.value)
    assert capsys.readouterr().out == ""


# --- cluster-info ---------------------------------------------------------------


def test_cluster_info_answers_the_bladeai_startup_probe(
    kubeconfig_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``env_info._check_k8s_available`` runs ``<kubectl> cluster-info --kubeconfig P [--context C]``."""
    transport = FakeTransport({CLUSTER_INFO_PATH: EMPTY_POD_LIST})
    argv = ["cluster-info", "--kubeconfig", str(kubeconfig_path), "--context", "bladeai-trial"]
    assert main(argv, transport=transport) == 0
    assert capsys.readouterr().out == REACHABLE
    assert transport.calls == [(CLUSTER_INFO_PATH, PROXY_TOKEN)]


@pytest.mark.parametrize(
    "flags_before_verb",
    [[], ["--context", "bladeai-trial"], ["--context=bladeai-trial", "-n", "otel-demo"]],
)
def test_cluster_info_accepts_global_flags_before_the_verb(
    capsys: pytest.CaptureFixture[str], flags_before_verb: list[str]
) -> None:
    transport = FakeTransport({CLUSTER_INFO_PATH: EMPTY_POD_LIST})
    assert main([*flags_before_verb, "cluster-info"], transport=transport) == 0
    assert capsys.readouterr().out == REACHABLE


def test_cluster_info_read_is_authorised_by_the_proxy_policy(capsys: pytest.CaptureFixture[str]) -> None:
    transport = ProxyPolicyTransport(EMPTY_POD_LIST)
    assert main(["cluster-info"], transport=transport) == 0
    assert capsys.readouterr().out == REACHABLE
    assert transport.backend_calls == [("GET", CLUSTER_INFO_PATH)]


def test_cluster_info_fails_when_the_proxy_refuses_the_read(
    kubeconfig_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    transport = RefusingTransport()
    with pytest.raises(ReadCliError, match="not reachable through .* proxy: proxy rejected .* status 403"):
        main(["cluster-info", "--kubeconfig", str(kubeconfig_path)], transport=transport)
    assert transport.calls == [CLUSTER_INFO_PATH]
    assert capsys.readouterr().out == ""


def test_cluster_info_rejects_bad_answers_foreign_contexts_and_subcommands() -> None:
    with pytest.raises(ReadCliError, match="not reachable.*non-JSON"):
        main(["cluster-info"], transport=FakeTransport({CLUSTER_INFO_PATH: b"<html>sign in</html>"}))
    unused = FakeTransport({})
    with pytest.raises(ReadCliError, match="context does not match"):
        main(["cluster-info", "--context", "prod-admin"], transport=unused)
    with pytest.raises(ReadCliError, match="unsupported cluster-info argument: dump"):
        main(["cluster-info", "dump"], transport=unused)
    assert unused.calls == []


def test_kubectl_wrapper_exit_code_follows_the_proxy_read(
    kubeconfig_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """bladeai reads only the exit code: 0 means Kubernetes is available."""

    def healthy_proxy(_transport: Any, path: str, token: str) -> bytes:
        assert (path, token) == (CLUSTER_INFO_PATH, PROXY_TOKEN)
        return json.dumps(EMPTY_POD_LIST).encode("utf-8")

    def unreachable_proxy(_transport: Any, path: str, token: str) -> bytes:
        raise ReadCliError("proxy Kubernetes read failed")

    probe_argv = ["cluster-info", "--kubeconfig", str(kubeconfig_path)]
    assert run_kubectl_wrapper(monkeypatch, probe_argv, healthy_proxy) == 0
    assert capsys.readouterr().out == REACHABLE
    assert run_kubectl_wrapper(monkeypatch, probe_argv, unreachable_proxy) == 1
    assert capsys.readouterr().err.startswith("Error: Kubernetes control plane is not reachable")


def test_rejection_message_lists_cluster_info() -> None:
    with pytest.raises(ReadCliError, match="exec blade, cluster-info, and config current-context"):
        main(["delete", "pod", "cart-abc"], transport=FakeTransport({}))
