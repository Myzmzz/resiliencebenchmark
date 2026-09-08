"""BladeAI task-mode request and Harness-confirmation boundary.

This module deliberately contains no Controller-selected fault.  Task mode is
the evaluated-agent path: the only task input that carries user intent is the
verbatim prompt.  The L4 SDK may plan a target itself, but every eventual
write is still checked by the controlled ``blade`` shim.
"""

from __future__ import annotations

import asyncio
import ast
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


TASK_MODE = "task"
MANAGED_MODE = "managed"
WP8_QUALIFICATION_TYPE = "BLADEAI_WP8_FULL_CHAIN_QUALIFICATION"
_ALLOWED_MODES = frozenset({TASK_MODE, MANAGED_MODE})


_PLAN_BLOCK_RE = re.compile(
    r"```(?:stage2|yaml|yml|text)?\s*\n?(?P<body>.*?)```",
    re.IGNORECASE | re.DOTALL,
)
_PLAN_LINE_RE = re.compile(r"^\s*(?P<key>[A-Za-z][A-Za-z0-9_-]*)\s*:\s*(?P<value>.*?)\s*$")
_PLAN_FLAG_RE = re.compile(
    r"--(?P<key>time|timeout|percent|cpu-percent|mem-percent)\s+(?P<value>\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_PLAN_PARAM_KEYS = frozenset({"time", "timeout", "percent", "cpu-percent", "mem-percent"})


def _structured_plan_fields(content: str) -> dict[str, Any]:
    """Extract only explicit canonical fields from an Agent plan body.

    The upstream ``save_fault_plan`` tool accepts markdown, while the Stage-2
    confirmation contract needs typed parameters.  A fenced key/value block is
    the primary representation.  The flag fallback accepts only literal
    ChaosBlade ``--time/--percent/...`` spellings, never nearby prose values.
    """
    blocks = [match.group("body") for match in _PLAN_BLOCK_RE.finditer(content)]
    candidates: list[dict[str, str]] = []
    for body in blocks:
        fields: dict[str, str] = {}
        for line in body.splitlines():
            match = _PLAN_LINE_RE.match(line)
            if match:
                fields[match.group("key").lower()] = match.group("value").strip().strip("`")
        if {"scope", "target", "action", "namespace", "names"}.issubset(fields):
            candidates.append(fields)

    # More than one canonical block is ambiguous.  Refuse to choose between
    # potentially different Agent plans and let the Controller reject it.
    if len(candidates) > 1 and any(candidates[0].get(k) != item.get(k) for item in candidates[1:] for k in set(candidates[0]) | set(item)):
        return {}
    fields = dict(candidates[-1] if candidates else {})

    params: dict[str, str] = {
        key: value
        for key, value in fields.items()
        if key in _PLAN_PARAM_KEYS and re.fullmatch(r"\d+(?:\.\d+)?", value)
    }
    if not params:
        for match in _PLAN_FLAG_RE.finditer(content):
            params[match.group("key").lower()] = match.group("value")
    if not params:
        return {}

    result: dict[str, Any] = {"params": params}
    if fields.get("scope") and fields.get("target") and fields.get("action"):
        result["fault_intent"] = {
            "scope": fields["scope"],
            "target": fields["target"],
            "action": fields["action"],
        }
    namespace = fields.get("namespace", "").strip()
    names = fields.get("names", "").strip()
    if namespace and names:
        # The parser does not select a target from prose; it only carries a
        # single name explicitly present in the canonical block.
        if names.startswith("[") and names.endswith("]"):
            try:
                parsed_names = ast.literal_eval(names)
            except (ValueError, SyntaxError):
                parsed_names = []
            clean_names = [str(item).strip() for item in parsed_names] if isinstance(parsed_names, (list, tuple)) else []
        else:
            clean_names = [item.strip() for item in names.split(",") if item.strip()]
        if len(clean_names) == 1:
            result["target"] = {"namespace": namespace, "names": clean_names}
    timeout = params.get("timeout")
    if timeout is not None and timeout.isdigit() and int(timeout) > 0:
        result["duration_seconds"] = int(timeout)
    return result


class BladeTaskError(ValueError):
    """A Worker request would violate the BladeAI task-mode boundary."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "BLADE_TASK_ERROR",
        diagnostic: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.diagnostic = dict(diagnostic or {})


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
        # ``save_fault_plan`` is a real Agent tool, but BladeAI's upstream
        # graph only persists its markdown body and may omit the same typed
        # parameters from the interrupt state.  Keep the latest explicitly
        # machine-readable fields separately so ``record_state`` cannot erase
        # them before the confirmation bridge consumes the proposal.
        self._tool_fields: dict[str, Any] = {}

    def record_tool_event(self, tool: str, payload: Mapping[str, Any]) -> None:
        """Capture typed fields from the Agent's successful planning tool.

        Stage-2 asks the Agent to include one fenced canonical block in
        ``save_fault_plan.plan_content``.  Only values in that block (or
        explicit ChaosBlade ``--flag value`` spellings) are accepted; prose
        numbers are never treated as fault parameters.
        """
        name = str(tool or "").strip().lower().rsplit(".", 1)[-1]
        if name != "save_fault_plan":
            return
        arguments = payload.get("input") or payload.get("params") or payload.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                try:
                    arguments = ast.literal_eval(arguments)
                except (ValueError, SyntaxError):
                    arguments = {}
        if not isinstance(arguments, Mapping):
            return
        content = arguments.get("plan_content")
        if not isinstance(content, str) or not content.strip():
            return
        # The graph emits the same call once through the full callback and
        # again through a legacy, truncated event.  Keep the full parse when
        # the duplicate cannot be parsed; a successful later parse replaces
        # it for a genuinely new planning call.
        parsed = _structured_plan_fields(content)
        if parsed:
            self._tool_fields = parsed

    def record(self, proposal: Mapping[str, Any]) -> None:
        current = dict(self._state_fields)
        current.update(dict(proposal))
        self._proposal = current

    def record_state(self, state: Mapping[str, Any]) -> None:
        """Retain the typed Agent plan exposed at the confirmation gate.

        BladeAI 0.6.2 sometimes puts target/fault/parameter fields only in the
        graph's ``fault_spec`` state and emits a shortened interrupt payload.
        Keeping those same-state fields lets the Stage-2 Worker validate the
        complete proposal without inventing values or trusting free text.
        """
        # LangGraph re-enters this node while resuming an approved interrupt.
        # That resumed capture is not consumed by require_approval again, so
        # every node entry must discard it before inspecting the next plan.
        self._state_fields = {}
        self._proposal = None
        fault_spec = state.get("fault_spec")
        if not isinstance(fault_spec, Mapping):
            to_dict = getattr(fault_spec, "to_dict", None)
            if callable(to_dict):
                try:
                    fault_spec = to_dict()
                except Exception:  # pragma: no cover - defensive SDK boundary.
                    fault_spec = None
        if not isinstance(fault_spec, Mapping):
            # Some LangGraph versions project FaultSpec fields back to the
            # legacy top-level state instead of retaining ``fault_spec``.
            # Capture only the known typed fields; the downstream canonical
            # parser still rejects incomplete or out-of-scope values.
            fault_spec = {
                key: state.get(key)
                for key in (
                    "namespace", "names", "labels", "scope", "blade_target",
                    "blade_action", "params", "params_flags", "duration_seconds",
                    "duration",
                )
                if state.get(key) not in (None, "", {}, [])
            }
        if not isinstance(fault_spec, Mapping):
            return
        target: dict[str, Any] = {}
        namespace = fault_spec.get("namespace")
        if isinstance(namespace, str) and namespace.strip():
            target["namespace"] = namespace.strip()
        names = fault_spec.get("names")
        if isinstance(names, (list, tuple)):
            clean_names = [str(item).strip() for item in names if str(item).strip()]
            if clean_names:
                target["names"] = clean_names
        labels = fault_spec.get("labels")
        if isinstance(labels, Mapping) and labels:
            target["labels"] = {str(key): str(value) for key, value in labels.items()}
        if target:
            self._state_fields["target"] = target
        fault_intent: dict[str, str] = {}
        for source, destination in (
            ("scope", "scope"),
            ("blade_target", "target"),
            ("blade_action", "action"),
        ):
            value = fault_spec.get(source)
            if isinstance(value, str) and value.strip():
                fault_intent[destination] = value.strip()
        if fault_intent:
            self._state_fields["fault_intent"] = fault_intent
        params = fault_spec.get("params") or state.get("params")
        if isinstance(params, Mapping) and params:
            self._state_fields["params"] = dict(params)
        duration = fault_spec.get("duration_seconds") or fault_spec.get("duration")
        duration = duration or state.get("duration_seconds") or state.get("duration")
        if isinstance(duration, int) and not isinstance(duration, bool) and duration > 0:
            self._state_fields["duration_seconds"] = duration

        # Keep fields extracted from the planning tool until ``take``.  They
        # are lower priority than a typed interrupt/state field, and therefore
        # can only fill an omission in the SDK payload.
        for key, value in self._tool_fields.items():
            current = self._state_fields.get(key)
            if current is None or current == {} or current == []:
                self._state_fields[key] = value

    def take(self) -> dict[str, Any]:
        if self._proposal is None:
            if not self._state_fields:
                raise BladeTaskError("BladeAI did not expose a confirmation proposal to the Runtime")
            proposal = {}
        else:
            proposal = dict(self._proposal)
        # The interrupt payload is authoritative when it contains a field;
        # same-gate FaultSpec values fill only fields the payload omitted.
        for key, value in self._state_fields.items():
            current = proposal.get(key)
            if current is None or current == {} or current == []:
                proposal[key] = value
        self._proposal = None
        self._state_fields = {}
        self._tool_fields = {}
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
    qualification_fault: dict[str, Any] | None = None

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
            qualification_fault=_mapping_or_none(
                value.get("qualification_fault"), "qualification_fault"
            ),
        )
        request._validate()
        return request

    def _validate(self) -> None:
        if self.mode == TASK_MODE:
            if self.target is not None:
                raise BladeTaskError("task mode must not preselect target")
            if self.managed_fault is not None:
                raise BladeTaskError("task mode must not inject managed_fault")
            if self.qualification_fault is not None and self.qualification_fault.get(
                "qualification_type"
            ) != WP8_QUALIFICATION_TYPE:
                raise BladeTaskError(
                    "task mode qualification_fault is reserved for BladeAI WP8 qualification"
                )
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
        elif self.qualification_fault is not None:
            payload.update(_qualification_fault_payload(self.qualification_fault))
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

    try:
        async with sse_client(url, headers={"Authorization": f"Bearer {token}"}) as streams:
            read_stream, write_stream = streams
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(tool, dict(arguments))
    except Exception as exc:
        raise BladeTaskError(
            f"{tool} MCP transport failed: {_safe_exception_detail(exc)}",
            code="MCP_TRANSPORT_ERROR",
            diagnostic={"tool": tool, "error_type": type(exc).__name__},
        ) from exc
    text = _mcp_text_content(result)
    if result.isError:
        raise BladeTaskError(
            f"{tool} MCP call failed: {_safe_detail(text) or 'empty error response'}",
            code="MCP_TOOL_ERROR",
            diagnostic={"tool": tool, "mcp_error": _safe_detail(text)},
        )
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BladeTaskError(
            f"{tool} returned non-JSON MCP content: {_safe_detail(text)}",
            code="MCP_NON_JSON_RESPONSE",
            diagnostic={"tool": tool},
        ) from exc
    if not isinstance(value, Mapping):
        raise BladeTaskError(
            f"{tool} returned an invalid response",
            code="MCP_INVALID_RESPONSE",
            diagnostic={"tool": tool, "response_type": type(value).__name__},
        )
    if value.get("ok") is False:
        error = value.get("error")
        error = error if isinstance(error, Mapping) else {}
        response_code = str(error.get("code") or f"{tool.upper()}_ERROR")
        message = _safe_detail(str(error.get("message") or "MCP response reported failure"))
        diagnostic = {
            "tool": tool,
            "response_error_code": response_code,
            "response_error_message": message,
        }
        nested = error.get("diagnostic")
        if isinstance(nested, Mapping):
            diagnostic["response_diagnostic"] = {
                str(key): _safe_detail(str(val)) for key, val in nested.items()
            }
        raise BladeTaskError(
            f"{tool} returned error: {response_code}: {message}",
            code=response_code,
            diagnostic=diagnostic,
        )
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
        exc = error[0]
        if isinstance(exc, BladeTaskError):
            raise exc
        raise BladeTaskError(
            f"async MCP bridge failed: {_safe_exception_detail(exc)}",
            code="ASYNC_MCP_BRIDGE_FAILED",
            diagnostic={"error_type": type(exc).__name__},
        ) from exc
    if not result:
        raise BladeTaskError(
            "async MCP bridge returned no result",
            code="ASYNC_MCP_BRIDGE_EMPTY_RESULT",
        )
    return result[0]


def _mcp_text_content(result: Any) -> str:
    return "".join(
        str(item.text)
        for item in getattr(result, "content", ())
        if getattr(item, "type", None) == "text" and hasattr(item, "text")
    )


def _safe_exception_detail(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {_safe_detail(str(exc))}"


def _safe_detail(value: str, *, limit: int = 300) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    for marker in ("Bearer ", "api_key=", "token=", "password=", "secret="):
        index = text.lower().find(marker.lower())
        if index >= 0:
            text = text[:index] + marker + "<redacted>"
            break
    return text[:limit]


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


def _qualification_fault_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    """Project the controller-fixed WP8 contract into L4 FaultSpec fields.

    This supplies only the fault type/intensity/duration.  The Agent still
    discovers and binds the target Pod by name and current UID.
    """
    fault_type = _required_text(value.get("fault_type"), "qualification_fault.fault_type")
    if fault_type != "network-delay":
        raise BladeTaskError("WP8 qualification currently supports network-delay only")
    intensity = value.get("intensity")
    if not isinstance(intensity, Mapping):
        raise BladeTaskError("qualification_fault.intensity is required")
    delay_ms = intensity.get("delay_ms")
    if isinstance(delay_ms, bool) or delay_ms is None:
        raise BladeTaskError("qualification_fault.intensity.delay_ms is required")
    duration = value.get("duration_seconds")
    if isinstance(duration, bool) or not isinstance(duration, int) or duration < 1:
        raise BladeTaskError("qualification_fault.duration_seconds must be positive")
    return {
        "fault_scope": "pod",
        "fault_target": "network",
        "fault_action": "delay",
        "params": {"time": str(delay_ms)},
        "duration": duration,
        "qualification_type": WP8_QUALIFICATION_TYPE,
        # WP8 must exercise the real SDK confirmation callback.  The target
        # remains Agent-selected; only the fixed qualification contract is
        # supplied by the Controller.
        "needs_confirmation": True,
    }


def _namespace_from_target(value: Mapping[str, Any]) -> object:
    target = value.get("target")
    return target.get("namespace") if isinstance(target, Mapping) else None
