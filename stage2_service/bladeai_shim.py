"""Controlled replacement for the ``blade`` binary used by BladeAI.

It supports only the BladeAI CLI forms that can be bound to the Controller's
Trial-scoped MCP tools.  It never executes a native binary, invokes kubectl, or
accepts a kubeconfig/endpoint from command-line arguments.  Discovery and
mutations are MCP calls made with the Trial token, so the existing policy gate,
ledger, baseline and cleanup contracts remain authoritative.
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


@dataclass(frozen=True)
class BladeRecord:
    cleanup_handle: str
    namespace: str
    target_name: str
    target_uid: str
    fault_type: str
    duration_seconds: int
    intensity: dict[str, Any]
    status: str = "Created"


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
        evidence_file: Path | None = None,
    ) -> None:
        self.client = client
        self.namespace = namespace
        self.state_file = state_file
        self.max_duration_seconds = max_duration_seconds
        self.kubeconfig_path = kubeconfig_path
        self.evidence_file = evidence_file or _default_evidence_file(state_file)

    def run(self, argv: Sequence[str]) -> tuple[int, str, str]:
        try:
            if tuple(argv) in {("version",), ("--version",), ("-v",)}:
                return 0, "ChaosBlade controlled shim\n", ""
            if any(value in {"-h", "--help"} for value in argv):
                return 0, _help_text(), ""
            if len(argv) >= 4 and tuple(argv[:2]) == ("create", "k8s"):
                return self._create(argv)
            if argv and argv[0] == "destroy":
                return self._destroy(argv[1:])
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
        unknown_handle = _unknown_outcome_cleanup_handle(created)
        unknown_outcome = bool(unknown_handle)
        if unknown_outcome:
            handle = unknown_handle
        else:
            _require_ok(created, "chaos_create_experiment")
            handle = _cleanup_handle(created)
        blade_uid = str(uuid.uuid4())
        self._remember(
            blade_uid,
            BladeRecord(
                cleanup_handle=handle,
                namespace=create.namespace,
                target_name=create.target_name,
                target_uid=target_uid,
                fault_type=create.fault_type,
                duration_seconds=create.duration_seconds,
                intensity=create.intensity,
            ),
        )
        # The SDK parser requires a UUID-shaped result.  This is a shim-local
        # opaque alias; it maps back to the Controller cleanup handle below.
        evidence = self._evidence(
            "create",
            blade_uid=blade_uid,
            record=self._record_for_uid(blade_uid),
            responses=[
                ("k8s_get_resource", discovered),
                ("chaos_validate_plan", validation),
                ("chaos_create_experiment", created),
            ],
        )
        self._append_evidence(evidence)
        if unknown_outcome:
            return 1, json.dumps(
                {
                    "code": 54000,
                    "success": False,
                    "error": _unknown_outcome_error_payload(created),
                    "result": {"uid": blade_uid, "operation_id": handle},
                    "_resbench": evidence,
                },
                ensure_ascii=False,
            ) + "\n", ""
        return 0, json.dumps(
            {"code": 200, "success": True, "result": blade_uid, "_resbench": evidence},
            ensure_ascii=False,
        ) + "\n", ""

    def _destroy(self, values: Sequence[str]) -> tuple[int, str, str]:
        if not values:
            raise BladeShimError("blade destroy requires the experiment UID")
        blade_uid = values[0]
        _only_kubeconfig_flags(values[1:], self.kubeconfig_path)
        record = self._record_for_uid(blade_uid)
        destroyed = self.client.call("chaos_destroy_experiment", {"cleanup_handle": record.cleanup_handle})
        _require_ok(destroyed, "chaos_destroy_experiment")
        self._update_status(blade_uid, "Destroyed")
        evidence = self._evidence(
            "destroy",
            blade_uid=blade_uid,
            record=self._record_for_uid(blade_uid),
            responses=[("chaos_destroy_experiment", destroyed)],
        )
        self._append_evidence(evidence)
        return 0, json.dumps(
            {"code": 200, "success": True, "result": blade_uid, "_resbench": evidence},
            ensure_ascii=False,
        ) + "\n", ""

    def _status(self, values: Sequence[str]) -> tuple[int, str, str]:
        blade_uid = _status_uid(values, self.kubeconfig_path)
        if not blade_uid:
            return self._status_all()
        record = self._record_for_uid(blade_uid)
        result = self.client.call("chaos_operation_status", {"operation_id": record.cleanup_handle})
        _require_ok(result, "chaos_operation_status")
        status = _blade_status_from_operation(result)
        self._update_status(blade_uid, status)
        evidence = self._evidence(
            "status",
            blade_uid=blade_uid,
            record=self._record_for_uid(blade_uid),
            responses=[("chaos_operation_status", result)],
        )
        self._append_evidence(evidence)
        operation = _operation_result_metadata(result)
        return 0, json.dumps(
            {
                "code": 200,
                "success": True,
                "result": {"Uid": blade_uid, "Status": status, "status": status, **operation},
                "_resbench": evidence,
            },
            ensure_ascii=False,
        ) + "\n", ""

    def _query(self, values: Sequence[str]) -> tuple[int, str, str]:
        if not values:
            raise BladeShimError("blade query k8s create requires the experiment UID")
        blade_uid = values[0]
        _only_kubeconfig_flags(values[1:], self.kubeconfig_path)
        record = self._record_for_uid(blade_uid)
        result = self.client.call("chaos_operation_status", {"operation_id": record.cleanup_handle})
        _require_ok(result, "chaos_operation_status")
        status = _blade_status_from_operation(result)
        self._update_status(blade_uid, status)
        target_name = str(result.get("target_name") or record.target_name)
        namespace = str(result.get("namespace") or record.namespace)
        target_uid = str(result.get("target_uid") or record.target_uid)
        operation = _operation_result_metadata(result)
        statuses = [] if not target_name else [{
            "state": status,
            "kind": "pod",
            "identifier": f"{namespace}/{target_name}",
            "uid": target_uid,
            **operation,
        }]
        evidence = self._evidence(
            "query",
            blade_uid=blade_uid,
            record=self._record_for_uid(blade_uid),
            responses=[("chaos_operation_status", result)],
        )
        self._append_evidence(evidence)
        return 0, json.dumps(
            {
                "code": 200,
                "success": True,
                "result": {"uid": blade_uid, "statuses": statuses, **operation},
                "_resbench": evidence,
            },
            ensure_ascii=False,
        ) + "\n", ""

    def _status_all(self) -> tuple[int, str, str]:
        results: list[dict[str, str]] = []
        for blade_uid, record in sorted(self._read_state().items()):
            response = self.client.call("chaos_operation_status", {"operation_id": record.cleanup_handle})
            _require_ok(response, "chaos_operation_status")
            status = _blade_status_from_operation(response)
            self._update_status(blade_uid, status)
            operation = _operation_result_metadata(response)
            evidence = self._evidence(
                "status",
                blade_uid=blade_uid,
                record=self._record_for_uid(blade_uid),
                responses=[("chaos_operation_status", response)],
            )
            self._append_evidence(evidence)
            results.append({"Uid": blade_uid, "Status": status, "status": status, **operation})
        return 0, json.dumps({"code": 200, "success": True, "result": results}) + "\n", ""

    def _remember(self, blade_uid: str, record: BladeRecord) -> None:
        if self.state_file is None:
            raise BladeShimError("controlled blade shim state file is required")
        self.state_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        state = self._read_state()
        state[blade_uid] = record
        self._write_state(state)

    def _update_status(self, blade_uid: str, status: str) -> None:
        state = self._read_state()
        record = state.get(blade_uid)
        if record is None:
            raise BladeShimError("experiment UID is not owned by this Trial")
        state[blade_uid] = BladeRecord(
            cleanup_handle=record.cleanup_handle,
            namespace=record.namespace,
            target_name=record.target_name,
            target_uid=record.target_uid,
            fault_type=record.fault_type,
            duration_seconds=record.duration_seconds,
            intensity=record.intensity,
            status=status,
        )
        self._write_state(state)

    def _write_state(self, state: Mapping[str, BladeRecord]) -> None:
        if self.state_file is None:
            raise BladeShimError("controlled blade shim state file is required")
        payload = {
            uid: {
                "cleanup_handle": record.cleanup_handle,
                "namespace": record.namespace,
                "target_name": record.target_name,
                "target_uid": record.target_uid,
                "fault_type": record.fault_type,
                "duration_seconds": record.duration_seconds,
                "intensity": record.intensity,
                "status": record.status,
            }
            for uid, record in state.items()
        }
        temporary = self.state_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.state_file)

    def _evidence(
        self,
        operation: str,
        *,
        blade_uid: str,
        record: BladeRecord,
        responses: Sequence[tuple[str, Mapping[str, Any]]],
    ) -> dict[str, Any]:
        return {
            "schema_version": "resbench.blade_shim_evidence.v1",
            "shim_operation": operation,
            "blade_uid": blade_uid,
            "operation_id": record.cleanup_handle,
            "cleanup_handle": record.cleanup_handle,
            "namespace": record.namespace,
            "target_name": record.target_name,
            "target_uid": record.target_uid,
            "fault_type": record.fault_type,
            "mcp_calls": [
                evidence
                for tool, response in responses
                if (evidence := _mcp_response_evidence(tool, response))
            ],
        }

    def _append_evidence(self, evidence: Mapping[str, Any]) -> None:
        if self.evidence_file is None:
            return
        try:
            self.evidence_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with self.evidence_file.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(evidence, ensure_ascii=False, separators=(",", ":")) + "\n")
            self.evidence_file.chmod(0o600)
        except OSError as exc:
            raise BladeShimError("controlled blade shim evidence is unwritable") from exc

    def _record_for_uid(self, blade_uid: str) -> BladeRecord:
        if not _UUID.fullmatch(blade_uid):
            raise BladeShimError("experiment UID is invalid")
        try:
            return self._read_state()[blade_uid]
        except KeyError as exc:
            raise BladeShimError("experiment UID is not owned by this Trial") from exc

    def _read_state(self) -> dict[str, BladeRecord]:
        if self.state_file is None or not self.state_file.is_file():
            return {}
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BladeShimError("controlled blade shim state is unreadable") from exc
        if not isinstance(raw, Mapping):
            raise BladeShimError("controlled blade shim state is invalid")
        parsed: dict[str, BladeRecord] = {}
        for key, value in raw.items():
            if not _UUID.fullmatch(str(key)) or not isinstance(value, Mapping):
                raise BladeShimError("controlled blade shim state is invalid")
            cleanup_handle = value.get("cleanup_handle")
            namespace = value.get("namespace")
            target_name = value.get("target_name")
            target_uid = value.get("target_uid")
            fault_type = value.get("fault_type")
            duration_seconds = value.get("duration_seconds")
            intensity = value.get("intensity")
            status = value.get("status", "Created")
            if (
                not isinstance(cleanup_handle, str)
                or not cleanup_handle
                or not isinstance(namespace, str)
                or namespace != self.namespace
                or not isinstance(target_name, str)
                or not target_name
                or not isinstance(target_uid, str)
                or not target_uid
                or not isinstance(fault_type, str)
                or fault_type not in {"network-delay", "network-loss", "cpu-load", "memory-stress"}
                or not isinstance(duration_seconds, int)
                or not isinstance(intensity, Mapping)
                or not isinstance(status, str)
            ):
                raise BladeShimError("controlled blade shim state is invalid")
            parsed[str(key)] = BladeRecord(
                cleanup_handle=cleanup_handle,
                namespace=namespace,
                target_name=target_name,
                target_uid=target_uid,
                fault_type=fault_type,
                duration_seconds=duration_seconds,
                intensity=dict(intensity),
                status=status,
            )
        return parsed


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
        intensity=canonical_native_intensity(fault_type, flags, action=argv[3]),
    )


def canonical_native_intensity(fault_type: str, flags: Mapping[str, Any], *, action: str) -> dict[str, int]:
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
    if fault_type == "network-loss" and action == "drop":
        if flags:
            raise BladeShimError("network drop maps only to Controller 100 percent loss")
        return {"loss_percent": 100}
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


def _unknown_outcome_cleanup_handle(result: Mapping[str, Any]) -> str | None:
    error = result.get("error")
    if not isinstance(error, Mapping) or error.get("code") != "OPERATION_OUTCOME_UNKNOWN":
        return None
    details = error.get("details")
    if not isinstance(details, Mapping):
        raise BladeShimError("operation outcome is unknown but no operation_id was returned")
    value = details.get("cleanup_handle") or details.get("operation_id")
    if not isinstance(value, str) or not value:
        raise BladeShimError("operation outcome is unknown but no cleanup handle was returned")
    return value


def _unknown_outcome_error_payload(result: Mapping[str, Any]) -> dict[str, Any]:
    error = result.get("error")
    if not isinstance(error, Mapping):
        return {"code": "OPERATION_OUTCOME_UNKNOWN"}
    payload: dict[str, Any] = {"code": "OPERATION_OUTCOME_UNKNOWN"}
    message = error.get("message")
    next_step = error.get("next_step")
    if isinstance(message, str) and message:
        payload["message"] = message
    if isinstance(next_step, str) and next_step:
        payload["next_step"] = next_step
    return payload


def _default_evidence_file(state_file: Path | None) -> Path | None:
    if state_file is None:
        return None
    return state_file.with_name(f"{state_file.stem}.evidence.jsonl")


def _evidence_file_from_env() -> Path | None:
    value = os.environ.get("RESBENCH_BLADE_SHIM_EVIDENCE_FILE")
    if value:
        return Path(value)
    return None


def _mcp_response_evidence(tool: str, response: Mapping[str, Any]) -> dict[str, Any]:
    evidence: dict[str, Any] = {"tool": tool}
    error = response.get("error")
    details = error.get("details") if isinstance(error, Mapping) else None
    for source_key, output_key in (
        ("controller_call_id", "controller_call_id"),
        ("call_id", "call_id"),
        ("server_call_id", "server_call_id"),
        ("request_id", "request_id"),
        ("cleanup_handle", "cleanup_handle"),
        ("operation_id", "operation_id"),
        ("operation_outcome", "operation_outcome"),
        ("ledger_operation_outcome", "ledger_operation_outcome"),
        ("state", "ledger_state"),
    ):
        value = response.get(source_key)
        if not value and isinstance(details, Mapping):
            value = details.get(source_key)
        if isinstance(value, str) and value:
            evidence[output_key] = value
    ok = response.get("ok")
    if isinstance(ok, bool):
        evidence["ok"] = ok
    if isinstance(error, Mapping) and isinstance(error.get("code"), str):
        evidence["error"] = {"code": error["code"]}
    return evidence


def _operation_result_metadata(value: Mapping[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for source_key, output_key in (
        ("operation_id", "operation_id"),
        ("operation_outcome", "operation_outcome"),
        ("ledger_operation_outcome", "ledger_operation_outcome"),
        ("state", "ledger_state"),
    ):
        field = value.get(source_key)
        if isinstance(field, str) and field:
            result[output_key] = field
    return result


def _require_ok(result: Mapping[str, Any], tool: str) -> None:
    if result.get("ok") is not True:
        raise BladeShimError(f"{tool} was denied by the controlled MCP service")


def _help_text() -> str:
    return "controlled blade shim: create k8s, destroy, status, and query k8s create only\n"


def _status_uid(values: Sequence[str], expected_kubeconfig: str | None) -> str:
    if not values:
        return ""
    if len(values) in {2, 4} and values[0] == "--uid":
        blade_uid = values[1]
        _only_kubeconfig_flags(values[2:], expected_kubeconfig)
        return blade_uid
    if len(values) in {2, 4} and values[0] == "--type" and values[1] == "create":
        _only_kubeconfig_flags(values[2:], expected_kubeconfig)
        return ""
    if len(values) in {1, 3}:
        blade_uid = values[0]
        _only_kubeconfig_flags(values[1:], expected_kubeconfig)
        return blade_uid
    raise BladeShimError("blade status only accepts optional --uid and --kubeconfig")


def _only_kubeconfig_flags(values: Sequence[str], expected_kubeconfig: str | None) -> None:
    if not values:
        return
    if (
        len(values) != 2
        or values[0] != "--kubeconfig"
        or not expected_kubeconfig
        or values[1] != expected_kubeconfig
    ):
        raise BladeShimError("only the SDK-injected --kubeconfig flag is permitted")


def _blade_status_from_operation(value: Mapping[str, Any]) -> str:
    state = str(value.get("state") or "").lower()
    outcome = str(value.get("operation_outcome") or "").lower()
    live = value.get("live")
    if state in {"destroyed", "expired_cleaned"}:
        return "Destroyed"
    if outcome == "absent":
        return "Absent"
    if outcome == "applied" or (isinstance(live, Mapping) and live.get("found") is True):
        return "Success"
    if state in {"created", "pending", "initializing"}:
        return "Created"
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
        try:
            return asyncio.run(self._call(url, tool, dict(arguments)))
        except BladeShimError:
            raise
        except Exception as exc:
            raise BladeShimError(f"{tool} MCP call failed") from exc

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
        if tuple(values) in {("version",), ("--version",), ("-v",)}:
            code, stdout, stderr = 0, "ChaosBlade controlled shim\n", ""
        elif any(value in {"-h", "--help"} for value in values):
            code, stdout, stderr = 0, _help_text(), ""
        else:
            if not namespace:
                raise BladeShimError("RESBENCH_TRIAL_NAMESPACE is required")
            if not state_file:
                raise BladeShimError("RESBENCH_BLADE_SHIM_STATE_FILE is required")
            shim = BladeShim(McpToolClient.from_env(), namespace=namespace, state_file=Path(state_file),
                             kubeconfig_path=os.environ.get("BLADE_AI_KUBECONFIG_PATH"),
                             evidence_file=_evidence_file_from_env())
            code, stdout, stderr = shim.run(values)
    except BladeShimError as exc:
        code, stdout, stderr = 1, "", f"Error: {exc}\n"
    sys.stdout.write(stdout)
    sys.stderr.write(stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
