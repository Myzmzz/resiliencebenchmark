"""The simulated user's reply must survive a thinking model's formatting.

2026-09-22: the platform model deepseek-v4-pro-0813 reasons before it answers
and the reasoning shares the completion budget.  With the old 4,000-token cap a
long confirmation could cut the JSON off, which ended cdx-dspro-r1 on the
fleet and 4 of 5 BladeAI qualifications on the new environment with "Harness
conversation response is not JSON".
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from stage2_service.simulated_user import (
    HARNESS_MODEL_MAX_COMPLETION_TOKENS,
    ConversationError,
    HarnessResponder,
    parse_harness_reply,
)


ANSWER = '{"approved": true, "answer_mode": "approve_recommendation", "reason": "ok"}'


@pytest.mark.parametrize(
    "content",
    [
        ANSWER,
        f"```json\n{ANSWER}\n```",
        f"好的，我的决定如下：\n{ANSWER}\n以上。",
        [{"type": "text", "text": ANSWER}],
    ],
    ids=["plain", "fenced", "sentence-around", "content-blocks"],
)
def test_reply_object_is_recovered(content: Any) -> None:
    value = parse_harness_reply(content)
    assert value["approved"] is True
    assert value["answer_mode"] == "approve_recommendation"


def test_truncated_reply_names_the_finish_reason() -> None:
    with pytest.raises(ConversationError, match=r"not JSON \(finish_reason=length, \d+ chars\)"):
        parse_harness_reply('{"approved": true, "reason": "the plan is bou', finish_reason="length")


def test_a_json_array_is_still_rejected_as_not_an_object() -> None:
    with pytest.raises(ConversationError, match="not an object"):
        parse_harness_reply('[{"approved": true}]')


def test_from_environment_uses_the_raised_cap_and_the_shared_parser(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class FakeResponse:
        content = f"Decision:\n{ANSWER}"
        response_metadata = {"finish_reason": "stop"}
        usage_metadata = None

    class FakeChatOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        def invoke(self, messages: Any) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setitem(sys.modules, "langchain_openai", types.SimpleNamespace(ChatOpenAI=FakeChatOpenAI))
    responder = HarnessResponder.from_environment(
        {"RESBENCH_LLM_API_KEY": "test-key-not-secret", "RESBENCH_LLM_BASE_URL": "http://gateway.test/v1"},
        "deepseek-v4-pro-0813", "otel-demo-01", 300, 900,
    )
    result = responder.model_call("instructions", {"question": "confirm"})

    assert captured["max_completion_tokens"] == HARNESS_MODEL_MAX_COMPLETION_TOKENS == 16000
    value = result.value if hasattr(result, "value") else result
    assert value["approved"] is True
