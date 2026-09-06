"""BladeAI task-mode request and Harness-confirmation boundary.

This module deliberately contains no Controller-selected fault.  Task mode is
the evaluated-agent path: the only task input that carries user intent is the
verbatim prompt.  The L4 SDK may plan a target itself, but every eventual
write is still checked by the controlled ``blade`` shim.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


TASK_MODE = "task"
MANAGED_MODE = "managed"
_ALLOWED_MODES = frozenset({TASK_MODE, MANAGED_MODE})


class BladeTaskError(ValueError):
    """A Worker request would violate the BladeAI task-mode boundary."""


class HarnessConfirmationClient(Protocol):
    """Minimal synchronous bridge used from the L4 SDK interrupt callback."""

    def confirm(self, plan: Mapping[str, Any]) -> Mapping[str, Any]:
        """Return the authenticated Harness-channel confirmation response."""


class TargetUIDResolver(Protocol):
    """Resolve an Agent-selected Pod name through the controlled read path."""

    def pod_uid(self, *, namespace: str, name: str) -> str:
        """Return the current Pod UID, never a Controller-preselected UID."""


class NativeProposalCapture:
    """Stores the exact SDK confirmation interrupt payload for one Trial."""

    def __init__(self) -> None:
        self._proposal: dict[str, Any] | None = None
        self._state_fields: dict[str, Any] = {}

    def record(self, proposal: Mapping[str, Any]) -> None:
        current = dict(self._state_fields)
        current.update(dict(proposal))
        self._proposal = current

    def record_state(self, state: Mapping[str, Any]) -> None:
        """Retain only the Agent-owned duration from FaultSpec state."""
        # LangGraph re-enters this node while resuming an approved interrupt.
        # That resumed capture is not consumed by require_approval again, so
        # every node entry must discard it before inspecting the next plan.
        self._state_fields = {}
        self._proposal = None
        fault_spec = state.get("fault_spec")
        if not isinstance(fault_spec, Mapping):
            return
        duration = fault_spec.get("duration_seconds")
        if isinstance(duration, int) and not isinstance(duration, bool) and duration > 0:
            self._state_fields["duration_seconds"] = duration

    def take(self) -> dict[str, Any]:
        if self._proposal is None:
            raise BladeTaskError("BladeAI did not expose a confirmation proposal to the Runtime")
        proposal = dict(self._proposal)
        self._proposal = None
        self._state_fields = {}
        return proposal


@dataclass(frozen=True)
class BladeTaskRequest:
    trial_id: str
    intent: str
    namespace: str
    kubeconfig: str
    mode: str = TASK_MODE
    managed_fault: dict[str, Any] | None = None
    target: dict[str, Any] | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "BladeTaskRequest":
        mode = _required_text(value.get("mode", TASK_MODE), "mode")
        if mode not in _ALLOWED_MODES:
            raise BladeTaskError("mode must be 'task' or 'managed'")
        request = cls(
            trial_id=_required_text(value.get("trial_id"), "trial_id"),
            intent=_required_text(value.get("intent"), "intent"),
            namespace=_required_text(value.get("namespace") or _namespace_from_target(value), "namespace"),
            kubeconfig=_required_absolute_path(value.get("kubeconfig"), "kubeconfig"),
            mode=mode,
            managed_fault=_mapping_or_none(value.get("managed_fault"), "managed_fault"),
            target=_mapping_or_none(value.get("target"), "target"),
        )
        request._validate()
        return request

    def _validate(self) -> None:
        if self.mode == TASK_MODE:
            if self.target is not None:
                raise BladeTaskError("task mode must not preselect target")
            if self.managed_fault is not None:
                raise BladeTaskError("task mode must not inject managed_fault")
        elif self.managed_fault is None:
            raise BladeTaskError("managed mode requires managed_fault")
        elif self.target is None:
            raise BladeTaskError("managed mode requires target")

    def l4_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "namespace": self.namespace,
            "kubeconfig": self.kubeconfig,
            "direct": self.mode == MANAGED_MODE,
            "auto_recover": True,
        }
        if self.mode == MANAGED_MODE:
            assert self.target is not None
            payload["target_names"] = [_required_text(self.target.get("name"), "target.name")]
            payload.update(self.managed_fault or {})
        return payload

    def l4_target(self) -> str | None:
        if self.mode == TASK_MODE:
            return None
        assert self.target is not None
        return _required_text(self.target.get("name"), "target.name")


class McpHarnessConfirmationClient:
    """A one-call MCP client for the authenticated Harness channel.

    The L4 SDK invokes ``require_approval`` synchronously while processing a
    graph interrupt.  The SDK itself owns an event loop for its graph, so this
    bridge runs the MCP request on a small dedicated thread rather than trying
    to nest ``asyncio.run`` in that loop.
    """

    def __init__(self, *, url: str, token: str) -> None:
        if not url.startswith("http://127.0.0.1:"):
            raise BladeTaskError("Harness channel URL must be loopback HTTP")
        if not token or token != token.strip():
            raise BladeTaskError("Harness channel token is required")
        self.url = url
        self.token = token

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "McpHarnessConfirmationClient":
        values = os.environ if env is None else env
        return cls(
            url=_required_text(values.get("RESBENCH_BLADEAI_HARNESS_CHANNEL_MCP_SSE_URL"), "RESBENCH_BLADEAI_HARNESS_CHANNEL_MCP_SSE_URL"),
            token=_required_text(values.get("RESBENCH_HARNESS_CHANNEL_TOKEN"), "RESBENCH_HARNESS_CHANNEL_TOKEN"),
        )

    def confirm(self, plan: Mapping[str, Any]) -> Mapping[str, Any]:
        return _run_coroutine_in_thread(self._confirm_async(dict(plan)))

    async def _confirm_async(self, plan: dict[str, Any]) -> Mapping[str, Any]:
        # BladeAI 0.6.2 itself uses this SDK transport.  Keeping the same
        # transport avoids a private HTTP shortcut around MCP authentication.
        return await _mcp_json_call(
            url=self.url,
            token=self.token,
            tool="harness_confirm",
            arguments={"plan": plan},
        )


class McpTargetUIDResolver:
    """Resolve a target through the same loopback k8s_ro policy gate."""

    def __init__(self, *, url: str, token: str) -> None:
        if not url.startswith("http://127.0.0.1:"):
            raise BladeTaskError("k8s_ro URL must be loopback HTTP")
        if not token or token != token.strip():
            raise BladeTaskError("k8s_ro token is required")
        self.url = url
        self.token = token

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "McpTargetUIDResolver":
        values = os.environ if env is None else env
        return cls(
            url=_required_text(values.get("RESBENCH_BLADEAI_K8S_MCP_SSE_URL"), "RESBENCH_BLADEAI_K8S_MCP_SSE_URL"),
            token=_required_text(values.get("RESBENCH_MCP_TOKEN"), "RESBENCH_MCP_TOKEN"),
        )

    def pod_uid(self, *, namespace: str, name: str) -> str:
        value = _run_coroutine_in_thread(self._get_pod(namespace, name))
        try:
            uid = value["object"]["metadata"]["uid"]
        except (KeyError, TypeError) as exc:
            raise BladeTaskError("controlled Pod discovery did not return metadata.uid") from exc
        if not isinstance(uid, str) or not uid:
            raise BladeTaskError("controlled Pod discovery returned an invalid UID")
        return uid

    async def _get_pod(self, namespace: str, name: str) -> Mapping[str, Any]:
        return await _mcp_json_call(
            url=self.url,
            token=self.token,
            tool="k8s_get_resource",
            arguments={"namespace": namespace, "resource": "pods", "name": name},
        )


def partial_plan_from_native_proposal(
    proposal: Mapping[str, Any],
    *,
    target_uid_resolver: TargetUIDResolver,
) -> dict[str, Any]:
    """Translate the current SDK confirmation payload into a partial plan.

    The SDK owns target, fault and parameter selection.  This adapter only
    copies those evidenced fields and obtains the Pod UID through ``k8s_ro``;
    it intentionally leaves effect/recovery conditions absent.  The shared
    Harness policy then decides whether the prompt level may supply them.
    """

    target = proposal.get("target")
    if not isinstance(target, Mapping):
        raise BladeTaskError("BladeAI proposal is missing structured target")
    namespace = _required_text(target.get("namespace"), "proposal.target.namespace")
    names = target.get("names")
    if not isinstance(names, list) or len(names) != 1:
        raise BladeTaskError("BladeAI proposal must identify exactly one target name")
    name = _required_text(names[0], "proposal.target.names[0]")
    fault = proposal.get("fault_intent")
    if not isinstance(fault, Mapping):
        raise BladeTaskError("BladeAI proposal is missing structured fault_intent")
    action = _required_text(fault.get("action"), "proposal.fault_intent.action")
    fault_type = _canonical_fault_type(
        _required_text(fault.get("scope"), "proposal.fault_intent.scope"),
        _required_text(fault.get("target"), "proposal.fault_intent.target"),
        action,
    )
    params = proposal.get("params")
    if not isinstance(params, Mapping) or not params:
        raise BladeTaskError("BladeAI proposal is missing evidenced fault parameters")
    from .bladeai_shim import BladeShimError, canonical_native_intensity
    native_params = {
        "--" + str(key).replace("_", "-"): value
        for key, value in params.items()
        if str(key) != "timeout"
    }
    try:
        intensity = canonical_native_intensity(fault_type, native_params, action=action)
    except BladeShimError as exc:
        raise BladeTaskError(str(exc)) from exc
    partial: dict[str, Any] = {
        "target": {
            "namespace": namespace,
            "name": name,
            "uid": target_uid_resolver.pod_uid(namespace=namespace, name=name),
        },
        "fault_type": fault_type,
        "intensity": intensity,
    }
    timeout = params.get("timeout")
    duration = proposal.get("duration_seconds")
    timeout_seconds = _strict_agent_seconds(timeout) if timeout is not None else None
    duration_seconds = _strict_agent_seconds(duration) if duration is not None else None
    if timeout_seconds is not None and duration_seconds is not None and timeout_seconds != duration_seconds:
        raise BladeTaskError("SDK proposal duration and timeout disagree")
    if duration_seconds is not None:
        partial["safety_ttl_seconds"] = duration_seconds
    elif timeout_seconds is not None:
        partial["safety_ttl_seconds"] = timeout_seconds
    return partial


def _canonical_fault_type(scope: str, target: str, action: str) -> str:
    key = (scope.lower(), target.lower(), action.lower())
    aliases = {
        ("pod", "network", "delay"): "network-delay",
        ("pod", "network", "loss"): "network-loss",
        ("pod", "network", "drop"): "network-loss",
        ("pod", "cpu", "load"): "cpu-load",
        ("pod", "cpu", "fullload"): "cpu-load",
        ("pod", "memory", "load"): "memory-stress",
        ("pod", "mem", "load"): "memory-stress",
    }
    try:
        return aliases[key]
    except KeyError as exc:
        raise BladeTaskError("BladeAI fault_intent is outside the authorized Stage-2 fault space") from exc


def _strict_agent_number(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise BladeTaskError(f"{field} must be a numeric Agent-provided value")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise BladeTaskError(f"{field} must be a numeric Agent-provided value") from exc
    if not number >= 0 or number == float("inf"):
        raise BladeTaskError(f"{field} is invalid")
    return number


def _strict_agent_seconds(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).isdigit():
        raise BladeTaskError("proposal.params.timeout must be an integer number of seconds")
    seconds = int(value)
    if seconds < 1:
        raise BladeTaskError("proposal.params.timeout must be positive")
    return seconds


def confirmation_granted(response: Mapping[str, Any]) -> bool:
    """Accept only an explicit successful Harness confirmation."""

    return response.get("ok") is True and response.get("allowed") is True


async def _mcp_json_call(
    *, url: str, token: str, tool: str, arguments: Mapping[str, Any]
) -> Mapping[str, Any]:
    """Make one authenticated SDK MCP call and decode its JSON text result."""

    from mcp import ClientSession
    from mcp.client.sse import sse_client

    async with sse_client(url, headers={"Authorization": f"Bearer {token}"}) as streams:
        read_stream, write_stream = streams
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool(tool, dict(arguments))
    if result.isError:
        raise BladeTaskError(f"{tool} MCP call failed")
    text = "".join(
        str(item.text)
        for item in result.content
        if getattr(item, "type", None) == "text" and hasattr(item, "text")
    )
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BladeTaskError(f"{tool} returned non-JSON MCP content") from exc
    if not isinstance(value, Mapping):
        raise BladeTaskError(f"{tool} returned an invalid response")
    return value


def _run_coroutine_in_thread(coro):
    import threading

    result: list[Any] = []
    error: list[BaseException] = []

    def run() -> None:
        try:
            result.append(asyncio.run(coro))
        except BaseException as exc:  # surfaced as a safe failed confirmation.
            error.append(exc)

    thread = threading.Thread(target=run, name="bladeai-harness-confirm", daemon=True)
    thread.start()
    thread.join()
    if error:
        raise BladeTaskError(f"harness_confirm unavailable: {type(error[0]).__name__}") from error[0]
    return result[0]


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BladeTaskError(f"{field} is required")
    return value.strip()


def _required_absolute_path(value: object, field: str) -> str:
    text = _required_text(value, field)
    if not Path(text).is_absolute():
        raise BladeTaskError(f"{field} must be an absolute path")
    return text


def _mapping_or_none(value: object, field: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise BladeTaskError(f"{field} must be an object")
    return dict(value)


def _namespace_from_target(value: Mapping[str, Any]) -> object:
    target = value.get("target")
    return target.get("namespace") if isinstance(target, Mapping) else None
