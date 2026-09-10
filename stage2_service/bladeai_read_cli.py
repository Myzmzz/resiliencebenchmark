"""Strict read-only kubectl replacement for BladeAI SDK observation commands."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode


PROXY_SERVER = "http://127.0.0.1:18481"
MAX_RESPONSE_BYTES = 1_000_000
_K8S_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")


class ReadCliError(ValueError):
    """Safe CLI rejection. Messages must not include proxy tokens."""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ReadCliError(message)


class ReadTransport(Protocol):
    def get(self, path: str, token: str) -> bytes:
        """Fetch one already validated Kubernetes API path from the loopback proxy."""


class HTTPProxyTransport:
    """Tiny HTTP client that refuses redirects and reads bounded bodies."""

    def get(self, path: str, token: str) -> bytes:
        conn = http.client.HTTPConnection("127.0.0.1", 18481, timeout=10)
        try:
            conn.request("GET", path, headers={"Authorization": "Bearer " + token})
            response = conn.getresponse()
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ReadCliError("proxy response exceeds the bounded output limit")
            if 300 <= response.status < 400:
                raise ReadCliError("proxy redirects are not accepted")
            if response.status >= 400:
                raise ReadCliError(f"proxy rejected Kubernetes read with status {response.status}")
            return body
        except OSError as exc:
            raise ReadCliError("proxy Kubernetes read failed") from exc
        finally:
            conn.close()


@dataclass(frozen=True)
class KubeConfig:
    path: Path
    namespace: str
    current_context: str
    server: str
    token: str


@dataclass
class CommonOptions:
    kubeconfig: str | None = None
    namespace: str | None = None
    output: str | None = None
    no_headers: bool = False
    label_selector: str | None = None
    field_selector: str | None = None
    limit: str | None = None
    continue_token: str | None = None
    resource_version: str | None = None
    timeout_seconds: str | None = None


@dataclass
class LogOptions(CommonOptions):
    container: str | None = None
    tail_lines: str | None = None
    since: str | None = None
    since_time: str | None = None
    previous: bool = False
    timestamps: bool = False
    limit_bytes: str | None = None


@dataclass(frozen=True)
class Command:
    verb: str
    resource: str | None = None
    name: str | None = None
    options: CommonOptions = field(default_factory=CommonOptions)
    exec_namespace: str | None = None
    inner_command: tuple[str, ...] = ()


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: ReadTransport | None = None,
    blade_runner: Any | None = None,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    command = _parse(args)
    kubeconfig = _load_kubeconfig(command.options.kubeconfig)

    if command.verb == "config":
        print(kubeconfig.current_context)
        return 0
    if command.verb == "exec":
        stdout, stderr = _run_controlled_blade_exec(command, kubeconfig, blade_runner)
        sys.stdout.write(stdout)
        sys.stderr.write(stderr)
        return 0

    client = transport or HTTPProxyTransport()
    if command.verb == "get":
        body = client.get(_get_path(command, kubeconfig), kubeconfig.token)
        print(_render_get(body, command))
        return 0
    if command.verb == "top":
        print(_render_top(client, command, kubeconfig))
        return 0
    if command.verb == "logs":
        body = client.get(_logs_path(command, kubeconfig), kubeconfig.token)
        print(body.decode("utf-8", errors="replace"))
        return 0
    raise ReadCliError("only read-only get, top, logs, exec blade, and config current-context are supported")


def _parse(argv: list[str]) -> Command:
    if not argv:
        raise ReadCliError("only read-only get, top, logs, exec blade, and config current-context are supported")
    verb_index = _find_verb(argv)
    if verb_index is None:
        raise ReadCliError("only read-only get, top, logs, exec blade, and config current-context are supported")
    verb = argv[verb_index]
    rest = argv[:verb_index] + argv[verb_index + 1 :]
    if verb == "config":
        return _parse_config(rest)
    if verb == "get":
        return _parse_get(rest)
    if verb == "top":
        return _parse_top(rest)
    if verb == "logs":
        return _parse_logs(rest)
    if verb == "exec":
        return _parse_exec(rest)
    raise ReadCliError("only read-only get, top, logs, exec blade, and config current-context are supported")


def _find_verb(argv: list[str]) -> int | None:
    value_flags = {"--kubeconfig", "--namespace", "-n", "-o", "--output"}
    verbs = {"get", "top", "logs", "exec", "config"}
    i = 0
    while i < len(argv):
        token = argv[i]
        if token in verbs:
            return i
        if token in value_flags:
            i += 2
            continue
        if token.startswith("--kubeconfig=") or token.startswith("--namespace=") or token.startswith("--output="):
            i += 1
            continue
        if token.startswith("-n") and token != "-n":
            i += 1
            continue
        if token.startswith("-o") and token != "-o":
            i += 1
            continue
        if token.startswith("-"):
            raise ReadCliError(f"unsupported flag before kubectl verb: {token}")
        return None
    return None


def _common_parser(*, output: bool = True, filters: bool = False, no_headers: bool = True) -> _ArgumentParser:
    parser = _ArgumentParser(prog="kubectl", add_help=False, allow_abbrev=False)
    parser.add_argument("--kubeconfig")
    parser.add_argument("-n", "--namespace", dest="namespace")
    if output:
        parser.add_argument("-o", "--output", dest="output")
    if filters:
        parser.add_argument("-l", "--selector", dest="label_selector")
        parser.add_argument("--field-selector", dest="field_selector")
        parser.add_argument("--limit", dest="limit")
        parser.add_argument("--continue", dest="continue_token")
        parser.add_argument("--resource-version", dest="resource_version")
        parser.add_argument("--request-timeout", dest="request_timeout")
    if no_headers:
        parser.add_argument("--no-headers", action="store_true", dest="no_headers")
    return parser


def _parse_get(tokens: list[str]) -> Command:
    parser = _common_parser(filters=True)
    parser.add_argument("resource")
    parser.add_argument("name", nargs="?")
    ns, extra = parser.parse_known_args(tokens)
    if extra:
        raise ReadCliError(f"unsupported get argument: {extra[0]}")
    options = CommonOptions(
        kubeconfig=ns.kubeconfig,
        namespace=ns.namespace,
        output=ns.output,
        no_headers=ns.no_headers,
        label_selector=ns.label_selector,
        field_selector=ns.field_selector,
        limit=ns.limit,
        continue_token=ns.continue_token,
        resource_version=ns.resource_version,
        timeout_seconds=_request_timeout_seconds(getattr(ns, "request_timeout", None)),
    )
    return Command("get", resource=ns.resource, name=ns.name, options=options)


def _parse_top(tokens: list[str]) -> Command:
    parser = _common_parser(output=False, filters=True)
    parser.add_argument("resource")
    parser.add_argument("name", nargs="?")
    ns, extra = parser.parse_known_args(tokens)
    if extra:
        raise ReadCliError(f"unsupported top argument: {extra[0]}")
    options = CommonOptions(
        kubeconfig=ns.kubeconfig,
        namespace=ns.namespace,
        no_headers=ns.no_headers,
        label_selector=ns.label_selector,
        field_selector=ns.field_selector,
        limit=ns.limit,
        continue_token=ns.continue_token,
        resource_version=ns.resource_version,
        timeout_seconds=_request_timeout_seconds(getattr(ns, "request_timeout", None)),
    )
    return Command("top", resource=ns.resource, name=ns.name, options=options)


def _parse_logs(tokens: list[str]) -> Command:
    parser = _common_parser(output=False, no_headers=False)
    parser.add_argument("-c", "--container", dest="container")
    parser.add_argument("--tail", dest="tail_lines")
    parser.add_argument("--since", dest="since")
    parser.add_argument("--since-time", dest="since_time")
    parser.add_argument("--previous", action="store_true")
    parser.add_argument("--timestamps", action="store_true")
    parser.add_argument("--limit-bytes", dest="limit_bytes")
    parser.add_argument("name")
    ns, extra = parser.parse_known_args(tokens)
    if extra:
        raise ReadCliError(f"unsupported logs argument: {extra[0]}")
    options = LogOptions(
        kubeconfig=ns.kubeconfig,
        namespace=ns.namespace,
        no_headers=False,
        container=ns.container,
        tail_lines=ns.tail_lines,
        since=ns.since,
        since_time=ns.since_time,
        previous=ns.previous,
        timestamps=ns.timestamps,
        limit_bytes=ns.limit_bytes,
    )
    return Command("logs", name=ns.name, options=options)


def _parse_config(tokens: list[str]) -> Command:
    parser = _common_parser(output=False)
    parser.add_argument("config_command")
    ns, extra = parser.parse_known_args(tokens)
    if extra:
        raise ReadCliError(f"unsupported config argument: {extra[0]}")
    if ns.config_command != "current-context":
        raise ReadCliError("only config current-context is supported")
    return Command("config", options=CommonOptions(kubeconfig=ns.kubeconfig, namespace=ns.namespace))


def _parse_exec(tokens: list[str]) -> Command:
    try:
        separator = tokens.index("--")
    except ValueError as exc:
        raise ReadCliError("kubectl exec is supported only for '-- blade ...' commands") from exc
    prefix = tokens[:separator]
    inner = tokens[separator + 1:]
    if not inner or Path(inner[0]).name != "blade":
        raise ReadCliError("kubectl exec is supported only for controlled blade commands")
    pod_name: str | None = None
    kubeconfig: str | None = None
    namespace: str | None = None
    index = 0
    while index < len(prefix):
        token = prefix[index]
        if token == "--kubeconfig":
            if index + 1 >= len(prefix):
                raise ReadCliError("--kubeconfig requires a value")
            kubeconfig = prefix[index + 1]
            index += 2
            continue
        if token.startswith("--kubeconfig="):
            kubeconfig = token.split("=", 1)[1]
            index += 1
            continue
        if token in {"-n", "--namespace"}:
            if index + 1 >= len(prefix):
                raise ReadCliError("namespace flag requires a value")
            namespace = prefix[index + 1]
            index += 2
            continue
        if token.startswith("--namespace="):
            namespace = token.split("=", 1)[1]
            index += 1
            continue
        if token.startswith("-n") and token != "-n":
            namespace = token[2:].removeprefix("=")
            index += 1
            continue
        if token.startswith("-"):
            raise ReadCliError(f"unsupported exec argument: {token}")
        if pod_name is not None:
            raise ReadCliError("kubectl exec accepts exactly one tool Pod name")
        _validate_resource_name(token)
        pod_name = token
        index += 1
    if not pod_name:
        raise ReadCliError("kubectl exec requires a tool Pod name")
    tool_namespace = os.environ.get("RESBENCH_BLADEAI_TOOL_NAMESPACE", "chaosblade")
    if namespace != tool_namespace:
        raise ReadCliError("kubectl exec is supported only for the controlled BladeAI tool namespace")
    return Command(
        "exec",
        name=pod_name,
        options=CommonOptions(kubeconfig=kubeconfig, namespace=namespace),
        exec_namespace=namespace,
        inner_command=tuple(inner),
    )


def _run_controlled_blade_exec(
    command: Command,
    kubeconfig: KubeConfig,
    blade_runner: Any | None,
) -> tuple[str, str]:
    if not command.inner_command or Path(command.inner_command[0]).name != "blade":
        raise ReadCliError("kubectl exec is supported only for controlled blade commands")
    if blade_runner is None:
        from .bladeai_shim import BladeShim, BladeShimError, McpToolClient

        namespace = os.environ.get("RESBENCH_TRIAL_NAMESPACE", "")
        state_file = os.environ.get("RESBENCH_BLADE_SHIM_STATE_FILE", "")
        evidence_file = os.environ.get("RESBENCH_BLADE_SHIM_EVIDENCE_FILE", "")
        if not namespace:
            raise ReadCliError("RESBENCH_TRIAL_NAMESPACE is required for controlled blade exec")
        if not state_file:
            raise ReadCliError("RESBENCH_BLADE_SHIM_STATE_FILE is required for controlled blade exec")
        try:
            blade_runner = BladeShim(
                McpToolClient.from_env(),
                namespace=namespace,
                state_file=Path(state_file),
                kubeconfig_path=str(kubeconfig.path),
                evidence_file=Path(evidence_file) if evidence_file else None,
            ).run
        except BladeShimError as exc:
            raise ReadCliError(str(exc)) from exc
    code, stdout, stderr = blade_runner(list(command.inner_command[1:]))
    if code != 0:
        message = (stderr or stdout or "controlled blade command failed").strip()
        raise ReadCliError(message.removeprefix("Error: ").strip())
    return stdout, stderr


def _load_kubeconfig(provided: str | None) -> KubeConfig:
    env_path = os.environ.get("BLADE_AI_KUBECONFIG_PATH")
    if not env_path:
        raise ReadCliError("BLADE_AI_KUBECONFIG_PATH is required")
    if provided and Path(provided) != Path(env_path):
        raise ReadCliError("provided kubeconfig does not match the injected BladeAI kubeconfig")
    path = Path(provided or env_path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ReadCliError("cannot read injected BladeAI kubeconfig") from exc
    except json.JSONDecodeError as exc:
        raise ReadCliError("injected BladeAI kubeconfig must be JSON from the loopback proxy") from exc
    current_context = str(raw.get("current-context") or "")
    contexts = raw.get("contexts") if isinstance(raw, Mapping) else None
    clusters = raw.get("clusters") if isinstance(raw, Mapping) else None
    users = raw.get("users") if isinstance(raw, Mapping) else None
    if not current_context or not isinstance(contexts, list) or not isinstance(clusters, list) or not isinstance(users, list):
        raise ReadCliError("injected BladeAI kubeconfig is incomplete")
    context = _named_item(contexts, current_context).get("context", {})
    cluster_name = str(context.get("cluster") or "")
    user_name = str(context.get("user") or "")
    namespace = str(context.get("namespace") or "default")
    server = str(_named_item(clusters, cluster_name).get("cluster", {}).get("server") or "")
    token = str(_named_item(users, user_name).get("user", {}).get("token") or "")
    if server != PROXY_SERVER:
        raise ReadCliError("injected BladeAI kubeconfig must use the exact loopback proxy endpoint")
    if not token:
        raise ReadCliError("injected BladeAI kubeconfig has no proxy token")
    return KubeConfig(path=path, namespace=namespace, current_context=current_context, server=server, token=token)


def _named_item(items: list[Any], name: str) -> Mapping[str, Any]:
    for item in items:
        if isinstance(item, Mapping) and item.get("name") == name:
            return item
    raise ReadCliError("injected BladeAI kubeconfig reference is invalid")


def _namespace(command: Command, kubeconfig: KubeConfig) -> str:
    ns = command.options.namespace or kubeconfig.namespace
    if not _K8S_NAME.fullmatch(ns):
        raise ReadCliError("namespace is invalid")
    return ns


def _get_path(command: Command, kubeconfig: KubeConfig) -> str:
    resource = _resource(command.resource)
    ns = _namespace(command, kubeconfig)
    if resource in {"pods", "endpoints", "events"}:
        path = f"/api/v1/namespaces/{ns}/{resource}"
        if command.name:
            if resource != "pods":
                raise ReadCliError("named get is supported only for Pods and Nodes")
            _validate_resource_name(command.name)
            _reject_list_only_options(command.options)
            path += f"/{command.name}"
        return _append_query(path, _list_query(command.options, allow_resource_version=bool(command.name)))
    if resource == "nodes":
        if not command.name:
            raise ReadCliError("get node requires an explicit node name")
        _validate_resource_name(command.name)
        _reject_list_only_options(command.options)
        return f"/api/v1/nodes/{command.name}"
    raise ReadCliError("resource is outside BladeAI read contract")


def _logs_path(command: Command, kubeconfig: KubeConfig) -> str:
    if not command.name:
        raise ReadCliError("logs requires a Pod name")
    _validate_resource_name(command.name)
    ns = _namespace(command, kubeconfig)
    query = _log_query(command.options)
    return _append_query(f"/api/v1/namespaces/{ns}/pods/{command.name}/log", query)


def _top_path(command: Command, kubeconfig: KubeConfig, name: str | None = None) -> str:
    resource = _resource(command.resource)
    ns = _namespace(command, kubeconfig)
    target = name or command.name
    if resource == "pods":
        path = f"/apis/metrics.k8s.io/v1beta1/namespaces/{ns}/pods"
        if target:
            _validate_resource_name(target)
            _reject_list_only_options(command.options)
            path += f"/{target}"
        return _append_query(path, _list_query(command.options, allow_resource_version=bool(target)))
    if resource == "nodes":
        if not target:
            raise ReadCliError("top node requires an explicit node name or namespace Pod-derived nodes")
        _validate_resource_name(target)
        return f"/apis/metrics.k8s.io/v1beta1/nodes/{target}"
    raise ReadCliError("only top pod or top node is supported")


def _reject_list_only_options(options: CommonOptions) -> None:
    if options.label_selector or options.field_selector or options.limit or options.continue_token or options.timeout_seconds:
        raise ReadCliError("list-only filters are not supported for named resource reads")


def _validate_resource_name(value: str) -> None:
    if not _K8S_NAME.fullmatch(value):
        raise ReadCliError("resource name is invalid")


def _resource(value: str | None) -> str:
    aliases = {
        "pod": "pods",
        "po": "pods",
        "pods": "pods",
        "endpoint": "endpoints",
        "endpoints": "endpoints",
        "ep": "endpoints",
        "event": "events",
        "events": "events",
        "node": "nodes",
        "nodes": "nodes",
    }
    key = value or ""
    if key not in aliases:
        raise ReadCliError("resource is outside BladeAI read contract")
    return aliases[key]


def _list_query(options: CommonOptions, *, allow_resource_version: bool) -> list[tuple[str, str]]:
    query: list[tuple[str, str]] = []
    if options.label_selector:
        query.append(("labelSelector", options.label_selector))
    if options.field_selector:
        query.append(("fieldSelector", options.field_selector))
    if options.limit:
        query.append(("limit", options.limit))
    if options.continue_token:
        query.append(("continue", options.continue_token))
    if options.timeout_seconds:
        query.append(("timeoutSeconds", options.timeout_seconds))
    if options.resource_version:
        if not allow_resource_version:
            query.append(("resourceVersion", options.resource_version))
        else:
            query.append(("resourceVersion", options.resource_version))
    return query


def _log_query(options: CommonOptions) -> list[tuple[str, str]]:
    log_options = options if isinstance(options, LogOptions) else LogOptions()
    query: list[tuple[str, str]] = []
    if log_options.container:
        query.append(("container", log_options.container))
    if log_options.tail_lines:
        query.append(("tailLines", log_options.tail_lines))
    if log_options.since:
        query.append(("sinceSeconds", _duration_seconds(log_options.since)))
    if log_options.since_time:
        query.append(("sinceTime", log_options.since_time))
    if log_options.previous:
        query.append(("previous", "true"))
    if log_options.timestamps:
        query.append(("timestamps", "true"))
    if log_options.limit_bytes:
        query.append(("limitBytes", log_options.limit_bytes))
    return query


def _append_query(path: str, query: list[tuple[str, str]]) -> str:
    if not query:
        return path
    return path + "?" + urlencode(query)


def _request_timeout_seconds(value: str | None) -> str | None:
    if not value:
        return None
    return _duration_seconds(value)


def _duration_seconds(value: str) -> str:
    if value.isdigit():
        return value
    suffixes = {"s": 1, "m": 60, "h": 3600}
    suffix = value[-1]
    if suffix not in suffixes or not value[:-1].isdigit():
        raise ReadCliError("duration value is outside the supported bounded syntax")
    seconds = int(value[:-1]) * suffixes[suffix]
    return str(seconds)


def _render_get(body: bytes, command: Command) -> str:
    fmt = _format(command.options.output)
    if fmt == "json":
        _ensure_json(body)
        return body.decode("utf-8", errors="replace")
    obj = _ensure_json(body)
    if fmt == "name":
        return "\n".join(_resource_name(item) for item in _items_or_self(obj) if _resource_name(item))
    if fmt.startswith("jsonpath="):
        return _jsonpath(obj, fmt.removeprefix("jsonpath="))
    if command.options.no_headers:
        return "\n".join(_table_row(item) for item in _items_or_self(obj))
    rows = ["NAME\tREADY\tSTATUS\tRESTARTS\tAGE"]
    rows.extend(_table_row(item) for item in _items_or_self(obj))
    return "\n".join(row for row in rows if row)


def _render_top(client: ReadTransport, command: Command, kubeconfig: KubeConfig) -> str:
    resource = _resource(command.resource)
    if resource == "nodes" and not command.name:
        pods = _ensure_json(client.get(f"/api/v1/namespaces/{_namespace(command, kubeconfig)}/pods", kubeconfig.token))
        nodes = sorted({
            str(item.get("spec", {}).get("nodeName") or "")
            for item in _items_or_self(pods)
            if isinstance(item, Mapping) and item.get("spec", {}).get("nodeName")
        })
        metrics = [_ensure_json(client.get(_top_path(command, kubeconfig, node), kubeconfig.token)) for node in nodes]
    else:
        obj = _ensure_json(client.get(_top_path(command, kubeconfig), kubeconfig.token))
        metrics = _items_or_self(obj)
    rows = [_metric_row(item) for item in metrics]
    if command.options.no_headers:
        return "\n".join(row for row in rows if row)
    header = "NAME\tCPU(cores)\tMEMORY(bytes)"
    return "\n".join([header, *[row for row in rows if row]])


def _format(output: str | None) -> str:
    if output is None:
        return "table"
    if output in {"json", "name", "wide"} or output.startswith("jsonpath="):
        return output
    raise ReadCliError("output format is unsupported")


def _ensure_json(body: bytes) -> Any:
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise ReadCliError("proxy returned non-JSON data for a structured Kubernetes read") from exc


def _items_or_self(obj: Any) -> list[Mapping[str, Any]]:
    if isinstance(obj, Mapping):
        items = obj.get("items")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, Mapping)]
        return [obj]
    return []


def _resource_name(item: Mapping[str, Any]) -> str:
    metadata = item.get("metadata", {})
    kind = str(item.get("kind") or "pod").lower()
    name = str(metadata.get("name") or "") if isinstance(metadata, Mapping) else ""
    if not name:
        return ""
    if kind.endswith("list"):
        kind = kind[:-4]
    if kind in {"podmetrics", "pod"}:
        kind = "pod"
    if kind in {"nodemetrics", "node"}:
        kind = "node"
    return f"{kind}/{name}"


def _table_row(item: Mapping[str, Any]) -> str:
    metadata = item.get("metadata", {})
    status = item.get("status", {})
    if not isinstance(metadata, Mapping):
        return ""
    name = str(metadata.get("name") or "")
    if not name:
        return ""
    phase = str(status.get("phase") or "") if isinstance(status, Mapping) else ""
    ready, restarts = _ready_and_restarts(status if isinstance(status, Mapping) else {})
    return f"{name}\t{ready}\t{phase}\t{restarts}\t<unknown>"


def _ready_and_restarts(status: Mapping[str, Any]) -> tuple[str, str]:
    statuses = status.get("containerStatuses")
    if not isinstance(statuses, list) or not statuses:
        return "", "0"
    ready = sum(1 for item in statuses if isinstance(item, Mapping) and item.get("ready"))
    restarts = sum(int(item.get("restartCount") or 0) for item in statuses if isinstance(item, Mapping))
    return f"{ready}/{len(statuses)}", str(restarts)


def _metric_row(item: Mapping[str, Any]) -> str:
    metadata = item.get("metadata", {})
    if not isinstance(metadata, Mapping):
        return ""
    usage = item.get("usage", {})
    if not isinstance(usage, Mapping) or not usage:
        containers = item.get("containers", [])
        usage = _sum_container_usage(containers if isinstance(containers, list) else [])
    name = str(metadata.get("name") or "")
    cpu = str(usage.get("cpu") or "")
    memory = str(usage.get("memory") or "")
    if not name:
        return ""
    return f"{name}\t{cpu}\t{memory}"


def _sum_container_usage(containers: list[Any]) -> dict[str, str]:
    cpu_m = 0
    memory_ki = 0
    for container in containers:
        if not isinstance(container, Mapping):
            continue
        usage = container.get("usage", {})
        if not isinstance(usage, Mapping):
            continue
        cpu_m += _cpu_millicores(str(usage.get("cpu") or "0"))
        memory_ki += _memory_kib(str(usage.get("memory") or "0"))
    return {"cpu": f"{cpu_m}m", "memory": f"{memory_ki}Ki"}


def _cpu_millicores(value: str) -> int:
    if value.endswith("m"):
        return int(value[:-1] or 0)
    if value.endswith("n"):
        return int(int(value[:-1] or 0) / 1_000_000)
    return int(float(value or "0") * 1000)


def _memory_kib(value: str) -> int:
    units = {"Ki": 1, "Mi": 1024, "Gi": 1024 * 1024}
    for suffix, multiplier in units.items():
        if value.endswith(suffix):
            return int(float(value.removesuffix(suffix)) * multiplier)
    return int(int(value or 0) / 1024)


def _jsonpath(obj: Any, expression: str) -> str:
    expression = expression.strip("'\"")
    if expression == "{range .items[*]}{.metadata.name} {end}":
        return " ".join(str(item.get("metadata", {}).get("name") or "") for item in _items_or_self(obj)).strip()
    if expression.startswith("{") and expression.endswith("}"):
        expression = expression[1:-1]
    if expression.startswith(".items[0]."):
        items = _items_or_self(obj)
        return _value_at_path(items[0] if items else {}, expression.removeprefix(".items[0]."))
    if expression.startswith(".items[*]."):
        key_path = expression.removeprefix(".items[*].")
        return " ".join(_value_at_path(item, key_path) for item in _items_or_self(obj)).strip()
    if expression.startswith("."):
        return _value_at_path(obj, expression.removeprefix("."))
    raise ReadCliError("jsonpath expression is unsupported")


def _value_at_path(obj: Any, path: str) -> str:
    current = obj
    for part in path.split("."):
        if "[" in part and part.endswith("]"):
            name, index_text = part[:-1].split("[", 1)
            current = current.get(name, []) if isinstance(current, Mapping) else []
            if index_text == "*":
                if not isinstance(current, list):
                    return ""
                return " ".join(str(item) for item in current)
            index = int(index_text)
            if not isinstance(current, list) or index >= len(current):
                return ""
            current = current[index]
            continue
        if not isinstance(current, Mapping):
            return ""
        current = current.get(part)
        if current is None:
            return ""
    if isinstance(current, (dict, list)):
        return json.dumps(current, separators=(",", ":"))
    return str(current)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReadCliError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
