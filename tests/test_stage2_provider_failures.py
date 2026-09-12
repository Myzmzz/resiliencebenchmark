"""O03: provider failures are classified, broken open, and attributed.

On 2026-09-11 the Bailian account fell into arrears. The provider answered
``HTTP 400`` with ``Arrearage``, the agent exited part-way through, and every
Trial after it was recorded as "execution failed / output is not a structured
result" — an agent verdict for a billing problem.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from starlette.testclient import TestClient

from stage2_service.llm_relay import TrialRelayConfig, create_trial_relay_app
from stage2_service.provider_failures import (
    CircuitPolicy,
    CircuitState,
    ProviderCircuitBreaker,
    ProviderFailure,
    ProviderFailureClass,
    classify_provider_failure,
    failure_attribution,
    failure_detail,
    route_key,
)


ARREARAGE_BODY = b'{"error":{"code":"Arrearage","message":"Access denied, please make sure your account is in good standing. Arrearage."}}'
ROUTE = route_key("dashscope", "qwen3.8-max")


class _Stream(httpx.AsyncByteStream):
    """A mock upstream body that can actually be streamed, as httpx requires."""

    def __init__(self, generator):
        self.generator = generator

    async def __aiter__(self):
        async for item in self.generator:
            yield item

    async def aclose(self):
        await asyncio.sleep(0)


async def _one_chunk(value: bytes):
    yield value


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --- taxonomy -------------------------------------------------------------


@pytest.mark.parametrize(
    ("status_code", "body", "error_type", "expected"),
    [
        (400, ARREARAGE_BODY, None, ProviderFailureClass.ARREARAGE),
        (403, b'{"error":"insufficient_quota"}', None, ProviderFailureClass.ARREARAGE),
        (400, '{"message":"账户余额不足"}', None, ProviderFailureClass.ARREARAGE),
        (401, b'{"error":{"message":"invalid api key"}}', None, ProviderFailureClass.AUTHENTICATION),
        (403, b'{"error":{"message":"forbidden"}}', None, ProviderFailureClass.AUTHENTICATION),
        (429, b'{"error":{"message":"Too Many Requests"}}', None, ProviderFailureClass.RATE_LIMIT),
        (200, b'{"ok":true}', None, ProviderFailureClass.RATE_LIMIT.NONE),
        (500, b"server error", None, ProviderFailureClass.UPSTREAM_ERROR),
        (503, b"unavailable", None, ProviderFailureClass.UPSTREAM_ERROR),
        (400, b'{"error":{"message":"model parameter is invalid"}}', None, ProviderFailureClass.BAD_REQUEST),
        (404, b"missing", None, ProviderFailureClass.BAD_REQUEST),
        (None, None, "ReadTimeout", ProviderFailureClass.TIMEOUT),
        (None, None, "ConnectTimeout", ProviderFailureClass.TIMEOUT),
        (None, None, "ConnectError", ProviderFailureClass.NETWORK),
    ],
)
def test_each_provider_outcome_gets_its_own_class(status_code, body, error_type, expected):
    assert classify_provider_failure(
        status_code=status_code, body=body, error_type=error_type
    ) is expected


def test_arrearage_is_not_read_as_an_ordinary_bad_request():
    """The 2026-09-11 incident in one assertion."""
    assert classify_provider_failure(status_code=400, body=ARREARAGE_BODY) is (
        ProviderFailureClass.ARREARAGE
    )
    assert ProviderFailureClass.ARREARAGE.is_provider_fault is True
    assert ProviderFailureClass.BAD_REQUEST.is_provider_fault is False


def test_failure_detail_is_bounded_and_scrubbed():
    detail = failure_detail(
        b'{"error":{"message":"Arrearage for api_key=sk-livekey-000111222333"}}'
    )

    assert "Arrearage" in detail
    assert "sk-livekey" not in detail
    assert len(detail) <= 240


# --- breaker --------------------------------------------------------------


def test_arrearage_opens_the_route_on_the_first_observation():
    breaker = ProviderCircuitBreaker(CircuitPolicy(failure_threshold=3, open_seconds=300))

    state = breaker.record(
        ProviderFailure(route_key=ROUTE, failure_class=ProviderFailureClass.ARREARAGE)
    )

    assert state is CircuitState.OPEN
    assert breaker.allows(ROUTE) is False
    reason = breaker.rejection_reason(ROUTE)
    assert "ARREARAGE" in reason
    assert ROUTE in reason


def test_transient_failures_need_the_threshold_before_opening():
    breaker = ProviderCircuitBreaker(CircuitPolicy(failure_threshold=3, open_seconds=300))
    failure = ProviderFailure(route_key=ROUTE, failure_class=ProviderFailureClass.UPSTREAM_ERROR)

    assert breaker.record(failure) is CircuitState.CLOSED
    assert breaker.record(failure) is CircuitState.CLOSED
    assert breaker.record(failure) is CircuitState.OPEN


def test_a_malformed_request_never_opens_the_route():
    breaker = ProviderCircuitBreaker(CircuitPolicy(failure_threshold=1))

    for _ in range(5):
        breaker.record(
            ProviderFailure(route_key=ROUTE, failure_class=ProviderFailureClass.BAD_REQUEST)
        )

    assert breaker.allows(ROUTE) is True


def test_one_route_outage_does_not_block_another_provider():
    breaker = ProviderCircuitBreaker()
    other = route_key("deepseek", "deepseek-v4-pro-0813")

    breaker.record(
        ProviderFailure(route_key=ROUTE, failure_class=ProviderFailureClass.ARREARAGE)
    )

    assert breaker.allows(ROUTE) is False
    assert breaker.allows(other) is True
    assert breaker.rejection_reason(other) is None


def test_the_route_is_admitted_again_after_the_cooldown_and_closes_on_success():
    clock = _Clock()
    breaker = ProviderCircuitBreaker(
        CircuitPolicy(failure_threshold=1, open_seconds=300), clock=clock
    )
    breaker.record(
        ProviderFailure(route_key=ROUTE, failure_class=ProviderFailureClass.ARREARAGE)
    )
    assert breaker.allows(ROUTE) is False

    clock.advance(299)
    assert breaker.allows(ROUTE) is False

    clock.advance(2)
    assert breaker.state(ROUTE) is CircuitState.HALF_OPEN
    assert breaker.allows(ROUTE) is True
    assert breaker.rejection_reason(ROUTE) is None

    breaker.record_success(ROUTE)
    assert breaker.state(ROUTE) is CircuitState.CLOSED


def test_a_success_reported_as_a_failure_of_class_none_closes_the_route():
    breaker = ProviderCircuitBreaker(CircuitPolicy(failure_threshold=1))
    breaker.record(ProviderFailure(route_key=ROUTE, failure_class=ProviderFailureClass.ARREARAGE))

    breaker.record(ProviderFailure(route_key=ROUTE, failure_class=ProviderFailureClass.NONE))

    assert breaker.state(ROUTE) is CircuitState.CLOSED


# --- attribution ----------------------------------------------------------


def test_attribution_names_the_provider_not_the_agent():
    attribution = failure_attribution(
        [
            ProviderFailure(
                route_key=ROUTE,
                failure_class=ProviderFailureClass.ARREARAGE,
                status_code=400,
                detail="Arrearage",
                model_alias="qwen3.8-max",
                provider="dashscope",
            )
        ]
    )

    assert attribution["provider_fault"] is True
    assert attribution["primary_failure_class"] == "ARREARAGE"
    assert attribution["failures"][0]["provider"] == "dashscope"


def test_attribution_is_empty_when_nothing_went_wrong_upstream():
    assert failure_attribution([])["provider_fault"] is False


# --- relay observation ----------------------------------------------------


def _relay_client(handler, observed: list[ProviderFailure]):
    config = TrialRelayConfig.issue(
        trial_id="campaign-abcdef0123456789-codex-t1",
        model_alias="qwen3.8-max",
        upstream_base_url="https://upstream.example/v1",
        upstream_api_key="upstream-key-0123456789",
        harness_name="codex",
        provider="dashscope",
        failure_observer=observed.append,
    )
    app = create_trial_relay_app(
        config,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    client = TestClient(app)
    client.headers.update({"authorization": f"Bearer {config.relay_token}"})
    return client, config


def test_the_relay_reports_an_arrearage_response_and_still_forwards_it():
    observed: list[ProviderFailure] = []
    client, _ = _relay_client(
        lambda request: httpx.Response(400, content=ARREARAGE_BODY), observed
    )

    response = client.post("/v1/chat/completions", json={"model": "qwen3.8-max"})

    assert response.status_code == 400
    assert response.content == ARREARAGE_BODY
    assert [item.failure_class for item in observed] == [ProviderFailureClass.ARREARAGE]
    assert observed[0].route_key == ROUTE
    assert "Arrearage" in observed[0].detail


def test_the_relay_reports_a_transport_timeout():
    observed: list[ProviderFailure] = []

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    client, _ = _relay_client(handler, observed)
    response = client.post("/v1/chat/completions", json={"model": "qwen3.8-max"})

    assert response.status_code == 504
    assert [item.failure_class for item in observed] == [ProviderFailureClass.TIMEOUT]


def test_the_relay_reports_a_successful_call_so_the_route_can_close():
    observed: list[ProviderFailure] = []
    client, _ = _relay_client(
        lambda request: httpx.Response(200, stream=_Stream(_one_chunk(b"{}"))), observed
    )

    response = client.post("/v1/chat/completions", json={"model": "qwen3.8-max"})

    assert response.status_code == 200
    assert [item.failure_class for item in observed] == [ProviderFailureClass.NONE]


def test_an_observer_that_raises_never_breaks_inference():
    def exploding(_failure: ProviderFailure) -> None:
        raise RuntimeError("observer is broken")

    config = TrialRelayConfig.issue(
        trial_id="campaign-abcdef0123456789-codex-t1",
        model_alias="qwen3.8-max",
        upstream_base_url="https://upstream.example/v1",
        upstream_api_key="upstream-key-0123456789",
        provider="dashscope",
        failure_observer=exploding,
    )
    app = create_trial_relay_app(
        config,
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=_Stream(_one_chunk(b"{}")))
            )
        ),
    )
    client = TestClient(app)
    client.headers.update({"authorization": f"Bearer {config.relay_token}"})

    assert client.post("/v1/chat/completions", json={"model": "qwen3.8-max"}).status_code == 200


# --- submission gate ------------------------------------------------------


def test_an_open_circuit_refuses_new_submissions_with_an_actionable_reason(tmp_path):
    """O03 acceptance: after the breaker trips, the next submission is refused."""
    from tests.test_stage2_task_service import request, task_service
    from stage2_service.task_service import TaskTemporarilyUnavailable

    class Runner:
        def run(self, request, event_observer=None, stop_requested=None):
            raise AssertionError("a refused submission must never start a campaign")

    breaker = ProviderCircuitBreaker(CircuitPolicy(failure_threshold=1, open_seconds=300))
    service, _supervisor, _controls = task_service(tmp_path, Runner())
    service.provider_breaker = breaker
    openai_route = route_key("openai", "gpt-5.5")

    # Closed: the request is accepted on its own merits.
    assert service.provider_breaker.allows(openai_route) is True

    breaker.record(
        ProviderFailure(
            route_key=openai_route,
            failure_class=ProviderFailureClass.ARREARAGE,
            status_code=400,
            detail="Arrearage",
        )
    )

    with pytest.raises(TaskTemporarilyUnavailable) as error:
        service.create(request())

    message = str(error.value)
    assert "ARREARAGE" in message
    assert openai_route in message
    assert "HTTP 400" in message


def test_a_recovered_route_is_admitted_again_by_the_submission_gate(tmp_path):
    from tests.test_stage2_task_service import request, task_service

    started: list[object] = []

    class Runner:
        def run(self, request, event_observer=None, stop_requested=None):
            started.append(request)
            return {"campaign_id": "campaign-0000000000000000", "trials": []}

    clock = _Clock()
    breaker = ProviderCircuitBreaker(
        CircuitPolicy(failure_threshold=1, open_seconds=300), clock=clock
    )
    service, _supervisor, _controls = task_service(tmp_path, Runner())
    service.provider_breaker = breaker
    openai_route = route_key("openai", "gpt-5.5")
    breaker.record(
        ProviderFailure(route_key=openai_route, failure_class=ProviderFailureClass.ARREARAGE)
    )

    breaker.record_success(openai_route)

    assert service._provider_circuit_reason(
        {"gateway_config": {"routes": {"gpt-5.5": {"provider": "openai"}}}}, "gpt-5.5"
    ) is None


def test_options_report_which_routes_are_broken_open(tmp_path):
    from tests.test_stage2_task_service import task_service

    class Runner:
        def run(self, request, event_observer=None, stop_requested=None):
            raise AssertionError("options must not start a campaign")

    breaker = ProviderCircuitBreaker(CircuitPolicy(failure_threshold=1))
    service, _supervisor, _controls = task_service(tmp_path, Runner())
    service.provider_breaker = breaker
    openai_route = route_key("openai", "gpt-5.5")
    breaker.record(
        ProviderFailure(route_key=openai_route, failure_class=ProviderFailureClass.ARREARAGE)
    )

    circuits = service.options()["provider_circuits"]

    assert circuits[openai_route]["state"] == "open"
    assert circuits[openai_route]["failure_class"] == "ARREARAGE"
