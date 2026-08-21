#!/usr/bin/env python3
"""Train-Ticket workload generator used inside the benchmark workload image.

The generator reads the controller-rendered workload.json plus runtime
environment variables. It writes a JMeter-compatible JTL file so downstream
analysis can consume one stable artifact format without embedding a mutable
JMeter plan in the public benchmark repository.
"""

from __future__ import annotations

import csv
import json
import math
import os
import socket
import ssl
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlparse
import http.client


MAX_RESPONSE_BYTES = 2 * 1024 * 1024
JTL_FIELDS = [
    "timeStamp",
    "elapsed",
    "label",
    "responseCode",
    "responseMessage",
    "threadName",
    "dataType",
    "success",
    "failureMessage",
    "bytes",
    "sentBytes",
    "grpThreads",
    "allThreads",
    "URL",
    "Latency",
    "IdleTime",
    "Connect",
]


class WorkloadRuntimeError(Exception):
    """Raised when the workload cannot run safely."""


@dataclass
class Sample:
    timestamp_ms: int
    elapsed_ms: int
    label: str
    response_code: str
    response_message: str
    thread_name: str
    success: bool
    failure_message: str
    response_bytes: int
    sent_bytes: int
    url: str
    latency_ms: int
    connect_ms: int

    def to_jtl_row(self, concurrency: int) -> list[Any]:
        return [
            self.timestamp_ms,
            self.elapsed_ms,
            self.label,
            self.response_code,
            self.response_message,
            self.thread_name,
            "text",
            str(self.success).lower(),
            self.failure_message,
            self.response_bytes,
            self.sent_bytes,
            concurrency,
            concurrency,
            self.url,
            self.latency_ms,
            0,
            self.connect_ms,
        ]


@dataclass
class RunState:
    samples: list[Sample] = field(default_factory=list)
    consecutive_failures: int = 0
    abort_reason: str | None = None


class HttpClient:
    def __init__(self, base_url: str, connect_timeout_ms: int, response_timeout_ms: int, allowed_hosts: set[str]):
        self.base_url = validate_base_url(base_url, allowed_hosts)
        self.parsed = urlparse(self.base_url)
        self.connect_timeout = connect_timeout_ms / 1000
        self.response_timeout = response_timeout_ms / 1000

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        token: str | None = None,
        label: str,
        thread_name: str,
    ) -> tuple[Sample, dict[str, Any] | list[Any] | None]:
        if not path.startswith("/"):
            raise WorkloadRuntimeError(f"unsafe path for {label}: {path}")
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8") if body is not None else b""
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"

        started = time.time()
        timestamp_ms = int(started * 1000)
        response_code = "000"
        response_message = ""
        response_body = b""
        connect_ms = 0
        parsed_json: dict[str, Any] | list[Any] | None = None
        success = False
        failure = ""
        conn: http.client.HTTPConnection | http.client.HTTPSConnection | None = None
        try:
            conn_cls = http.client.HTTPSConnection if self.parsed.scheme == "https" else http.client.HTTPConnection
            context = ssl.create_default_context() if self.parsed.scheme == "https" else None
            connect_start = time.time()
            if context is None:
                conn = conn_cls(self.parsed.hostname, self.parsed.port, timeout=self.connect_timeout)
            else:
                conn = conn_cls(self.parsed.hostname, self.parsed.port, timeout=self.connect_timeout, context=context)
            request_target = path
            conn.request(method.upper(), request_target, body=payload if payload else None, headers=headers)
            connect_ms = int((time.time() - connect_start) * 1000)
            if conn.sock is not None:
                conn.sock.settimeout(self.response_timeout)
            response = conn.getresponse()
            response_body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(response_body) > MAX_RESPONSE_BYTES:
                response_body = response_body[:MAX_RESPONSE_BYTES]
                failure = f"response body exceeded {MAX_RESPONSE_BYTES} bytes"
                response_message = failure
            response_code = str(response.status)
            response_message = response.reason or ""
            if response_body:
                try:
                    parsed_json = json.loads(response_body.decode("utf-8"))
                except json.JSONDecodeError:
                    parsed_json = None
            success = 200 <= response.status < 300 and not failure
        except (OSError, TimeoutError, socket.timeout) as exc:
            failure = exc.__class__.__name__
            response_message = str(exc)
        finally:
            if conn is not None:
                conn.close()

        elapsed_ms = int((time.time() - started) * 1000)
        sample = Sample(
            timestamp_ms=timestamp_ms,
            elapsed_ms=elapsed_ms,
            label=label,
            response_code=response_code,
            response_message=response_message,
            thread_name=thread_name,
            success=success,
            failure_message=failure,
            response_bytes=len(response_body),
            sent_bytes=len(payload),
            url=url,
            latency_ms=elapsed_ms,
            connect_ms=connect_ms,
        )
        return sample, parsed_json


def parse_allowed_hosts(value: str) -> set[str]:
    hosts = {item.strip().lower().rstrip(".") for item in value.split(",") if item.strip()}
    if not hosts:
        raise WorkloadRuntimeError("TRAIN_TICKET_ALLOWED_HOSTS must contain at least one host")
    for host in hosts:
        if "/" in host or "@" in host or ":" in host:
            raise WorkloadRuntimeError("TRAIN_TICKET_ALLOWED_HOSTS entries must be hostnames without scheme, port, or path")
    return hosts


def validate_base_url(value: str, allowed_hosts: set[str]) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise WorkloadRuntimeError("TRAIN_TICKET_BASE_URL must be an http(s) URL with a hostname")
    if parsed.username or parsed.password or "@" in parsed.netloc:
        raise WorkloadRuntimeError("TRAIN_TICKET_BASE_URL must not contain userinfo")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise WorkloadRuntimeError("TRAIN_TICKET_BASE_URL must not include path, query, or fragment")
    if parsed.hostname.lower().rstrip(".") not in allowed_hosts:
        raise WorkloadRuntimeError("TRAIN_TICKET_BASE_URL hostname is not in TRAIN_TICKET_ALLOWED_HOSTS")
    return value.rstrip("/")


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise WorkloadRuntimeError("workload.json must be a JSON object")
    return data


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise WorkloadRuntimeError(f"missing required environment variable: {name}")
    return value


def credential(name: str) -> str:
    direct = os.environ.get(name, "")
    if direct:
        return direct
    ref_name = os.environ.get(f"{name}_ENV", "")
    if ref_name:
        return require_env(ref_name)
    raise WorkloadRuntimeError(f"missing required environment variable: {name}")


def positive_number(config: dict[str, Any], key: str, default: float | None = None) -> float:
    value = config.get(key, default)
    if not isinstance(value, (int, float)) or value <= 0:
        raise WorkloadRuntimeError(f"{key} must be a positive number")
    return float(value)


def positive_int(config: dict[str, Any], key: str) -> int:
    value = config.get(key)
    if not isinstance(value, int) or value <= 0:
        raise WorkloadRuntimeError(f"{key} must be a positive integer")
    return value


def parse_int_env(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise WorkloadRuntimeError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise WorkloadRuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def require_bool_config(config: dict[str, Any], key: str, default: bool) -> bool:
    value = config.get(key, default)
    if not isinstance(value, bool):
        raise WorkloadRuntimeError(f"{key} must be a boolean")
    return value


def scenario_value(config: dict[str, Any], key: str) -> str:
    scenario = config.get("scenario")
    if not isinstance(scenario, dict):
        raise WorkloadRuntimeError("workload.json missing scenario object")
    value = scenario.get(key)
    if not isinstance(value, str) or not value.strip():
        raise WorkloadRuntimeError(f"scenario.{key} must be a non-empty string")
    return value.strip()


def extract_data(payload: dict[str, Any] | list[Any] | None) -> Any:
    if isinstance(payload, dict):
        return payload.get("data")
    return None


def app_status_ok(payload: dict[str, Any] | list[Any] | None) -> bool:
    if not isinstance(payload, dict) or "status" not in payload:
        return True
    status = payload["status"]
    return status is True or status == 1 or status == "1" or str(status).upper() in {"SUCCESS", "OK"}


def require_app_success(sample: Sample, parsed: dict[str, Any] | list[Any] | None, label: str) -> bool:
    if not sample.success:
        return False
    if not app_status_ok(parsed):
        sample.success = False
        sample.failure_message = f"{label} application status indicates failure"
        return False
    return True


def flow_login(client: HttpClient, username: str, password: str, thread_name: str) -> tuple[list[Sample], str, str]:
    sample, parsed = client.request(
        "POST",
        "/api/v1/users/login",
        body={"username": username, "password": password},
        label="login",
        thread_name=thread_name,
    )
    data = extract_data(parsed)
    token = data.get("token", "") if isinstance(data, dict) else ""
    account_id = data.get("userId", "") if isinstance(data, dict) else ""
    if not require_app_success(sample, parsed, "login") or not token or not account_id:
        sample.success = False
        sample.failure_message = sample.failure_message or "missing login token or account id"
    return [sample], str(token), str(account_id)


def flow_search(client: HttpClient, config: dict[str, Any], token: str | None, thread_name: str) -> tuple[list[Sample], dict[str, str]]:
    from_station = scenario_value(config, "from_station")
    to_station = scenario_value(config, "to_station")
    travel_date = scenario_value(config, "travel_date")
    sample, parsed = client.request(
        "POST",
        "/api/v1/travelservice/trips/left",
        body={"startPlace": from_station, "endPlace": to_station, "departureTime": travel_date},
        token=token,
        label="search-trips",
        thread_name=thread_name,
    )
    trip: dict[str, str] = {}
    data = extract_data(parsed)
    first = data[0] if isinstance(data, list) and data else None
    if isinstance(first, dict):
        trip_id = first.get("tripId")
        if isinstance(trip_id, dict):
            trip["trip_id"] = f"{trip_id.get('type', '')}{trip_id.get('number', '')}".strip()
        trip["from"] = str(first.get("startStation") or from_station)
        trip["to"] = str(first.get("terminalStation") or to_station)
    if not require_app_success(sample, parsed, "search") or not trip.get("trip_id"):
        sample.success = False
        sample.failure_message = sample.failure_message or "missing trip id"
    return [sample], trip


def flow_order(client: HttpClient, config: dict[str, Any], username: str, password: str, thread_name: str) -> list[Sample]:
    samples, token, account_id = flow_login(client, username, password, thread_name)
    if not token or not account_id:
        return samples

    search_samples, trip = flow_search(client, config, token, thread_name)
    samples.extend(search_samples)
    if not trip.get("trip_id"):
        return samples

    contacts_sample, contacts_payload = client.request(
        "GET",
        f"/api/v1/contactservice/contacts/account/{quote(account_id, safe='')}",
        token=token,
        label="contacts",
        thread_name=thread_name,
    )
    samples.append(contacts_sample)
    contacts_data = extract_data(contacts_payload)
    first_contact = contacts_data[0] if isinstance(contacts_data, list) and contacts_data else None
    contacts_id = first_contact.get("id", "") if isinstance(first_contact, dict) else ""
    if not require_app_success(contacts_sample, contacts_payload, "contacts") or not contacts_id:
        contacts_sample.success = False
        contacts_sample.failure_message = contacts_sample.failure_message or "missing contact id"
        return samples

    travel_date = scenario_value(config, "travel_date")
    preserve_body = {
        "accountId": account_id,
        "contactsId": contacts_id,
        "tripId": trip["trip_id"],
        "seatType": "2",
        "date": travel_date,
        "from": trip.get("from") or scenario_value(config, "from_station"),
        "to": trip.get("to") or scenario_value(config, "to_station"),
        "assurance": "0",
        "foodType": 0,
        "foodName": "",
        "foodPrice": 0,
        "stationName": "",
        "storeName": "",
        "handleDate": travel_date,
        "consigneeName": "",
        "consigneePhone": "",
        "consigneeWeight": 0,
        "isWithin": False,
    }
    preserve_sample, preserve_payload = client.request(
        "POST",
        "/api/v1/preserveservice/preserve",
        body=preserve_body,
        token=token,
        label="preserve-order",
        thread_name=thread_name,
    )
    samples.append(preserve_sample)
    if not require_app_success(preserve_sample, preserve_payload, "preserve"):
        return samples

    order_sample, order_payload = client.request(
        "POST",
        "/api/v1/orderservice/order/refresh",
        body={
            "loginId": account_id,
            "enableStateQuery": False,
            "enableTravelDateQuery": False,
            "enableBoughtDateQuery": False,
            "travelDateStart": None,
            "travelDateEnd": None,
            "boughtDateStart": None,
            "boughtDateEnd": None,
        },
        token=token,
        label="query-order",
        thread_name=thread_name,
    )
    samples.append(order_sample)
    order_id = newest_order_id(order_payload)
    if not require_app_success(order_sample, order_payload, "query-order") or not order_id:
        order_sample.success = False
        order_sample.failure_message = order_sample.failure_message or "missing order id"
        return samples

    if require_bool_config(config, "cleanupCreatedOrders", True):
        cancel_sample, cancel_payload = client.request(
            "GET",
            f"/api/v1/cancelservice/cancel/{quote(order_id, safe='')}/{quote(account_id, safe='')}",
            token=token,
            label="cleanup-order",
            thread_name=thread_name,
        )
        samples.append(cancel_sample)
        require_app_success(cancel_sample, cancel_payload, "cleanup-order")
    return samples


def newest_order_id(payload: dict[str, Any] | list[Any] | None) -> str:
    data = extract_data(payload)
    if not isinstance(data, list) or not data:
        return ""
    last = data[-1]
    if not isinstance(last, dict):
        return ""
    return str(last.get("id") or "").strip()


def run_flow(
    profile_id: str,
    client: HttpClient,
    config: dict[str, Any],
    username: str,
    password: str,
    thread_name: str,
) -> list[Sample]:
    if profile_id == "login":
        samples, _, _ = flow_login(client, username, password, thread_name)
        return samples
    if profile_id == "search":
        samples, _ = flow_search(client, config, None, thread_name)
        return samples
    if profile_id == "order":
        return flow_order(client, config, username, password, thread_name)
    raise WorkloadRuntimeError(f"unsupported profileId: {profile_id}")


def record_samples(state: RunState, samples: list[Sample], thresholds: dict[str, Any], min_samples: int) -> None:
    if state.abort_reason:
        return
    state.samples.extend(samples)
    for sample in samples:
        state.consecutive_failures = 0 if sample.success else state.consecutive_failures + 1
        max_consecutive = int(thresholds.get("maxConsecutiveFailures", 0) or 0)
        if max_consecutive and state.consecutive_failures >= max_consecutive:
            state.abort_reason = f"maxConsecutiveFailures reached: {state.consecutive_failures}"
            return
    if len(state.samples) < min_samples:
        return
    failures = sum(1 for sample in state.samples if not sample.success)
    max_error_rate = float(thresholds.get("maxErrorRate", 1.0))
    if failures / len(state.samples) > max_error_rate:
        state.abort_reason = f"maxErrorRate exceeded: {failures}/{len(state.samples)}"
        return
    max_p95 = int(thresholds.get("maxP95LatencyMs", 0) or 0)
    if max_p95:
        elapsed = sorted(sample.elapsed_ms for sample in state.samples)
        # Nearest-rank percentile uses a one-based rank. Subtracting one after
        # ceil is important at exact boundaries: for 20 samples p95 is the
        # 19th value, not the maximum (20th) value.
        index = max(0, math.ceil(len(elapsed) * 0.95) - 1)
        if elapsed[index] > max_p95:
            state.abort_reason = f"maxP95LatencyMs exceeded: {elapsed[index]}"


def default_abort_min_samples(concurrency: int, thresholds: dict[str, Any]) -> int:
    """Avoid sequentially rejecting a window before its error-rate resolution is meaningful."""
    minimum = max(20, concurrency * 2)
    try:
        max_error_rate = float(thresholds.get("maxErrorRate", 1.0))
    except (TypeError, ValueError) as exc:
        raise WorkloadRuntimeError("maxErrorRate must be numeric") from exc
    if 0 < max_error_rate < 1:
        minimum = max(minimum, math.ceil(1 / max_error_rate))
    return minimum


def write_jtl_header(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("w", encoding="utf-8", newline="")
    writer = csv.writer(handle)
    writer.writerow(JTL_FIELDS)
    handle.flush()
    return handle, writer


def append_jtl(writer: Any, handle: Any, samples: list[Sample], concurrency: int) -> None:
    for sample in samples:
        writer.writerow(sample.to_jtl_row(concurrency))
    handle.flush()


def run_workload(
    config: dict[str, Any],
    base_url: str,
    username: str,
    password: str,
    result_path: Path,
    allowed_hosts_raw: str,
) -> int:
    profile_id = str(config.get("profileId") or "")
    target_flow_qps = positive_number(config, "targetFlowQps")
    concurrency = positive_int(config, "concurrency")
    duration_seconds = positive_number(config, "durationSeconds")
    require_bool_config(config, "cleanupCreatedOrders", True)
    thresholds = config.get("abortThresholds")
    if not isinstance(thresholds, dict):
        raise WorkloadRuntimeError("workload.json missing abortThresholds object")
    connect_timeout_ms = parse_int_env("CONNECT_TIMEOUT_MS", 3000, minimum=100, maximum=60000)
    response_timeout_ms = parse_int_env("RESPONSE_TIMEOUT_MS", 10000, minimum=100, maximum=120000)
    min_samples = parse_int_env(
        "ABORT_MIN_SAMPLES",
        default_abort_min_samples(concurrency, thresholds),
        minimum=1,
        maximum=100000,
    )

    allowed_hosts = parse_allowed_hosts(allowed_hosts_raw)
    client = HttpClient(base_url, connect_timeout_ms, response_timeout_ms, allowed_hosts)
    state = RunState()
    lock = threading.Lock()
    schedule = {"next": 0}
    start = time.monotonic()
    deadline = start + duration_seconds

    handle, writer = write_jtl_header(result_path)
    try:
        def worker(worker_id: int) -> None:
            thread_name = f"train-ticket-{worker_id}"
            while True:
                with lock:
                    if state.abort_reason:
                        return
                    slot = schedule["next"]
                    schedule["next"] += 1
                scheduled_at = start + slot / target_flow_qps
                now = time.monotonic()
                if scheduled_at >= deadline:
                    return
                if scheduled_at > now:
                    time.sleep(scheduled_at - now)
                if time.monotonic() >= deadline:
                    return
                samples = run_flow(profile_id, client, config, username, password, thread_name)
                with lock:
                    append_jtl(writer, handle, samples, concurrency)
                    record_samples(state, samples, thresholds, min_samples)

        threads = [threading.Thread(target=worker, args=(index + 1,), daemon=True) for index in range(concurrency)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        handle.close()
    if state.abort_reason:
        print(f"ABORT: {state.abort_reason}", file=sys.stderr)
        return 3
    return 0


def main() -> int:
    try:
        config = read_json(Path(os.environ.get("WORKLOAD_CONFIG_PATH", "/etc/train-ticket-workload/workload.json")))
        base_url = require_env("TRAIN_TICKET_BASE_URL")
        allowed_hosts_raw = require_env("TRAIN_TICKET_ALLOWED_HOSTS")
        username = credential("TRAIN_TICKET_USERNAME")
        password = credential("TRAIN_TICKET_PASSWORD")
        result_path = Path(os.environ.get("RESULT_ARTIFACT", "/results/train-ticket.jtl"))
        return run_workload(config, base_url, username, password, result_path, allowed_hosts_raw)
    except WorkloadRuntimeError as exc:
        print(f"train_ticket_workload_generator: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
