"""Controlled replacement for the ``blade`` binary used by BladeAI.

It supports only the two CLI operations that the L4 path needs.  It never
executes a native binary, invokes kubectl, or accepts a kubeconfig/endpoint
from command-line arguments.  Discovery and mutations are MCP calls made with
the Trial token, so the existing policy gate, ledger, baseline and cleanup
contracts remain authoritative.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class BladeShimError(ValueError):
    """A requested CLI operation is outside the controlled shim contract."""


class ToolClient(Protocol):
    def call(self, tool: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        """Call an authenticated MCP tool and return its JSON object result."""


@dataclass(frozen=True)
class BladeCreate:
    namespace: str
    target_name: str
    fault_type: str
    duration_seconds: int
    intensity: dict[str, Any]


_FAULT_TYPES = {
    ("pod", "cpu", "fullload"): "cpu-load",
    ("pod", "cpu", "load"): "cpu-load",
    ("pod", "memory", "load"): "memory-stress",
    ("pod", "mem", "load"): "memory-stress",
    ("pod", "network", "delay"): "network-delay",
    ("pod", "network", "loss"): "network-loss",
    ("pod", "network", "drop"): "network-loss",
}
_SAFE_FLAG = re.compile(r"^--[a-z0-9-]+$")
_UUID = re.compile(r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")


class BladeShim:
    def __init__(
        self,
        client: ToolClient,
        *,
        namespace: str,
        state_file: Path | None = None,
        max_duration_seconds: int = 1200,
        kubeconfig_path: str | None = None,
    ) -> None:
        self.client = client
        self.namespace = namespace
        self.state_file = state_file
        self.max_duration_seconds = max_duration_seconds
        self.kubeconfig_path = kubeconfig_path

    def run(self, argv: Sequence[str]) -> tuple[int, str, str]:
        try:
            if tuple(argv) in {("version",), ("--version",), ("-v",)}:
                return 0, "ChaosBlade controlled shim\n", ""
            if any(value in {"-h", "--help"} for value in argv):
                return 0, _help_text(), ""
            if len(argv) >= 4 and tuple(argv[:2]) == ("create", "k8s"):
                return self._create(argv)
            if len(argv) == 2 and argv[0] == "destroy":
                return self._destroy(argv[1])
            if argv and argv[0] == "status":
                return self._status(argv[1:])
            if len(argv) >= 3 and tuple(argv[:3]) == ("query", "k8s", "create"):
                return self._query(argv[3:])
            raise BladeShimError("only controlled create, destroy, status, and query k8s create operations are permitted")
        except BladeShimError as exc:
            return 1, "", f"Error: {exc}\n"

    def _create(self, argv: Sequence[str]) -> tuple[int, str, str]:
        create = parse_create(argv, namespace=self.namespace, max_duration_seconds=self.max_duration_seconds,
                              expected_kubeconfig=self.kubeconfig_path)
        discovered = self.client.call(
            "k8s_get_resource",
            {"namespace": create.namespace, "resource": "pods", "name": create.target_name},
        )
        target_uid = _pod_uid(discovered)
        plan = {
            "namespace": create.namespace,
            "target_name": create.target_name,
            "target_uid": target_uid,
            "fault_type": create.fault_type,
            "duration_seconds": create.duration_seconds,
            "intensity": create.intensity,
        }
        validation = self.client.call("chaos_validate_plan", plan)
        _require_ok(validation, "chaos_validate_plan")
        created = self.client.call("chaos_create_experiment", plan)
        _require_ok(created, "chaos_create_experiment")
        handle = _cleanup_handle(created)
        blade_uid = str(uuid.uuid4())
        self._remember(blade_uid, handle)
        # The SDK parser requires a UUID-shaped result.  This is a shim-local
        # opaque alias; it maps back to the Controller cleanup handle below.
        return 0, json.dumps({"code": 200, "success": True, "result": blade_uid}, ensure_ascii=False) + "\n", ""

    def _destroy(self, blade_uid: str) -> tuple[int, str, str]:
        cleanup_handle = self._cleanup_handle_for_uid(blade_uid)
        destroyed = self.client.call("chaos_destroy_experiment", {"cleanup_handle": cleanup_handle})
        _require_ok(destroyed, "chaos_destroy_experiment")
        return 0, json.dumps({"success": True, "result": cleanup_handle}, ensure_ascii=False) + "\n", ""

    def _status(self, values: Sequence[str]) -> tuple[int, str, str]:
        blade_uid = _status_uid(values)
        if not blade_uid:
            return 0, json.dumps({"code": 200, "success": True, "result": []}) + "\n", ""
        result = self.client.call("chaos_operation_status", {"operation_id": self._cleanup_handle_for_uid(blade_uid)})
        _require_ok(result, "chaos_operation_status")
        status = _blade_status_from_operation(result)
        return 0, json.dumps({"code": 200, "success": True, "result": {"Uid": blade_uid, "Status": status, "status": status}}, ensure_ascii=False) + "\n", ""

    def _query(self, values: Sequence[str]) -> tuple[int, str, str]:
        if not values:
            raise BladeShimError("blade query k8s create requires the experiment UID")
        blade_uid = values[0]
        _only_kubeconfig_flags(values[1:])
        result = self.client.call("chaos_operation_status", {"operation_id": self._cleanup_handle_for_uid(blade_uid)})
        _require_ok(result, "chaos_operation_status")
        status = _blade_status_from_operation(result)
        target_name = str(result.get("target_name") or "")
        namespace = str(result.get("namespace") or self.namespace)
        statuses = [] if not target_name else [{"state": status, "kind": "pod", "identifier": f"{namespace}/{target_name}"}]
        return 0, json.dumps({"code": 200, "success": True, "result": {"uid": blade_uid, "statuses": statuses}}, ensure_ascii=False) + "\n", ""

    def _remember(self, blade_uid: str, cleanup_handle: str) -> None:
        if self.state_file is None:
            raise BladeShimError("controlled blade shim state file is required")
        self.state_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        state = self._read_state()
        state[blade_uid] = cleanup_handle
        temporary = self.state_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.state_file)

    def _cleanup_handle_for_uid(self, blade_uid: str) -> str:
        if not _UUID.fullmatch(blade_uid):
            raise BladeShimError("experiment UID is invalid")
        try:
            return self._read_state()[blade_uid]
        except KeyError as exc:
            raise BladeShimError("experiment UID is not owned by this Trial") from exc

    def _read_state(self) -> dict[str, str]:
        if self.state_file is None or not self.state_file.is_file():
            return {}
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BladeShimError("controlled blade shim state is unreadable") from exc
        if not isinstance(raw, Mapping) or not all(_UUID.fullmatch(str(key)) and isinstance(value, str) for key, value in raw.items()):
            raise BladeShimError("controlled blade shim state is invalid")
        return dict(raw)


def parse_create(argv: Sequence[str], *, namespace: str, max_duration_seconds: int,
                 expected_kubeconfig: str | None = None) -> BladeCreate:
    if len(argv) < 5 or tuple(argv[:2]) != ("create", "k8s"):
        raise BladeShimError("expected 'blade create k8s <scope>-<target> <action>'")
    try:
        scope, target = argv[2].split("-", 1)
    except ValueError as exc:
        raise BladeShimError("fault scenario must use '<scope>-<target>'") from exc
    fault_type = _FAULT_TYPES.get((scope, target, argv[3]))
    if fault_type is None:
        raise BladeShimError("requested ChaosBlade scenario is not authorized for this Trial")
    flags = _parse_flags(argv[4:])
    kubeconfig = flags.pop("--kubeconfig", None)
    if kubeconfig is not None and (
        not isinstance(kubeconfig, str) or not expected_kubeconfig or kubeconfig != expected_kubeconfig
    ):
        raise BladeShimError("--kubeconfig must be the injected loopback proxy path")
    target_name = flags.pop("--names", None)
    if not isinstance(target_name, str) or not target_name or "," in target_name:
        raise BladeShimError("exactly one --names target is required")
    selected_namespace = flags.pop("--namespace", namespace)
    if selected_namespace != namespace:
        raise BladeShimError("--namespace must equal the Controller-bound Trial namespace")
    duration_raw = flags.pop("--timeout", flags.pop("--duration", None))
    if duration_raw is None:
        raise BladeShimError("--timeout in seconds is required")
    if not isinstance(duration_raw, str) or not duration_raw.isdigit():
        raise BladeShimError("duration must be an integer number of seconds; units are not converted")
    duration_seconds = int(duration_raw)
    if not 1 <= duration_seconds <= max_duration_seconds:
        raise BladeShimError("duration is outside the Trial safety limit")
    return BladeCreate(
        namespace=namespace,
        target_name=target_name,
        fault_type=fault_type,
        duration_seconds=duration_seconds,
        intensity=canonical_native_intensity(fault_type, flags),
    )


def canonical_native_intensity(fault_type: str, flags: Mapping[str, Any]) -> dict[str, int]:
    """Map only documented native numeric knobs to Controller canonical fields."""
    rules = {
        "network-delay": ("--time", "delay_ms"),
        "network-loss": ("--percent", "loss_percent"),
        "cpu-load": ("--cpu-percent", "cpu_percent"),
        "memory-stress": ("--mem-percent", "mem_percent"),
    }
    try:
        native_key, canonical = rules[fault_type]
    except KeyError as exc:
        raise BladeShimError("native fault has no Controller-equivalent intensity mapping") from exc
    if fault_type in {"network-delay", "network-loss"}:
        interface = flags.pop("--interface", "eth0")
        if interface != "eth0":
            raise BladeShimError("network interface must be Controller-fixed eth0")
    if set(flags) != {native_key}:
        raise BladeShimError("native fault parameters are not exactly representable by Controller policy")
    value = flags[native_key]
    # CLI flags are strings; the SDK's structured proposal may contain JSON
    # integers. Both serialize to the same native argument without conversion.
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).isdigit() or int(value) <= 0:
        raise BladeShimError("native fault intensity must be a positive integer without units")
    return {canonical: int(value)}


def _parse_flags(values: Sequence[str]) -> dict[str, str | bool]:
    parsed: dict[str, str | bool] = {}
    index = 0
    while index < len(values):
        key = values[index]
        if not _SAFE_FLAG.fullmatch(key) or key in parsed:
            raise BladeShimError("invalid or duplicate CLI flag")
        if index + 1 < len(values) and not values[index + 1].startswith("--"):
            parsed[key] = values[index + 1]
            index += 2
        else:
            parsed[key] = True
            index += 1
    return parsed


def _pod_uid(result: Mapping[str, Any]) -> str:
    try:
        uid = result["object"]["metadata"]["uid"]
    except (KeyError, TypeError) as exc:
        raise BladeShimError("controlled pod discovery did not return a Pod UID") from exc
    if not isinstance(uid, str) or not uid:
        raise BladeShimError("controlled pod discovery returned an invalid Pod UID")
    return uid


def _cleanup_handle(result: Mapping[str, Any]) -> str:
    value = result.get("cleanup_handle") or result.get("operation_id")
    if not isinstance(value, str) or not value:
        raise BladeShimError("controlled create did not return a cleanup handle")
    return value


def _require_ok(result: Mapping[str, Any], tool: str) -> None:
    if result.get("ok") is not True:
        raise BladeShimError(f"{tool} was denied by the controlled MCP service")


def _help_text() -> str:
    return "controlled blade shim: create k8s, destroy, status, and query k8s create only\n"


def _status_uid(values: Sequence[str]) -> str:
    if not values:
        return ""
    if len(values) in {2, 4} and values[0] == "--uid":
        blade_uid = values[1]
        _only_kubeconfig_flags(values[2:])
        return blade_uid
    raise BladeShimError("blade status only accepts optional --uid and --kubeconfig")


def _only_kubeconfig_flags(values: Sequence[str]) -> None:
    if not values:
        return
    if len(values) != 2 or values[0] != "--kubeconfig" or not values[1].startswith("/"):
        raise BladeShimError("only the SDK-injected --kubeconfig flag is permitted")


def _blade_status_from_operation(value: Mapping[str, Any]) -> str:
    state = str(value.get("state") or "").lower()
    live = value.get("live")
    if state == "destroyed" or value.get("operation_outcome") == "absent":
        return "Destroyed"
    if value.get("operation_outcome") == "applied" or (isinstance(live, Mapping) and live.get("found") is True):
        return "Success"
    return "Error"


class McpToolClient:
    """Synchronous MCP client used only by the shim executable."""

    def __init__(self, *, k8s_url: str, chaos_url: str, token: str) -> None:
        if not all(url.startswith("http://127.0.0.1:") for url in (k8s_url, chaos_url)):
            raise BladeShimError("MCP URLs must be loopback endpoints")
        self.k8s_url, self.chaos_url, self.token = k8s_url, chaos_url, token

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "McpToolClient":
        values = os.environ if env is None else env
        try:
            return cls(
                k8s_url=str(values["RESBENCH_BLADEAI_K8S_MCP_SSE_URL"]),
                chaos_url=str(values["RESBENCH_BLADEAI_CHAOS_CONTROL_MCP_SSE_URL"]),
                token=str(values["RESBENCH_MCP_TOKEN"]),
            )
        except KeyError as exc:
            raise BladeShimError("required loopback MCP endpoint is missing") from exc

    def call(self, tool: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        url = self.k8s_url if tool.startswith("k8s_") else self.chaos_url
        return asyncio.run(self._call(url, tool, dict(arguments)))

    async def _call(self, url: str, tool: str, arguments: dict[str, Any]) -> Mapping[str, Any]:
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        async with sse_client(url, headers={"Authorization": f"Bearer {self.token}"}) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                result = await session.call_tool(tool, arguments)
        if result.isError:
            raise BladeShimError(f"{tool} MCP call failed")
        body = "".join(str(item.text) for item in result.content if getattr(item, "type", None) == "text")
        try:
            value = json.loads(body)
        except json.JSONDecodeError as exc:
            raise BladeShimError(f"{tool} MCP response was not JSON") from exc
        if not isinstance(value, Mapping):
            raise BladeShimError(f"{tool} MCP response was not an object")
        return value


def main(argv: Sequence[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    namespace = os.environ.get("RESBENCH_TRIAL_NAMESPACE", "")
    state_file = os.environ.get("RESBENCH_BLADE_SHIM_STATE_FILE", "")
    try:
        if not state_file:
            raise BladeShimError("RESBENCH_BLADE_SHIM_STATE_FILE is required")
        shim = BladeShim(McpToolClient.from_env(), namespace=namespace, state_file=Path(state_file),
                         kubeconfig_path=os.environ.get("BLADE_AI_KUBECONFIG_PATH"))
        code, stdout, stderr = shim.run(values)
    except BladeShimError as exc:
        code, stdout, stderr = 1, "", f"Error: {exc}\n"
    sys.stdout.write(stdout)
    sys.stderr.write(stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
