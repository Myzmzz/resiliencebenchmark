"""真实 L0 暴露的两处：网关凭据送不进去，计划进不了校验器。

2026-09-13 在独占环境跑完整 L0 时，两处依次挡住了注入：

1. 试验被判 ``CASE_INVALID`` / ``GATEWAY_EVIDENCE_MISSING``。另外三家 Harness
   以子进程启动，每轮的中继凭据随环境变量注入；BladeAI 是**先于试验启动、
   且活得比试验长**的服务，读不到那份环境。它于是直接打模型网关，中继一个
   request id 都没发，取证为空。

2. 三次回合、零次 ``blade_create``。BladeAI 把计划写成散文加一段围栏 JSON，
   并请用户回一个确认词（F10）。平台的对话解释器只把"选项"抽了出来——实测
   第一轮得到 ``"A"``、第二轮得到 ``"确认"``——真正的计划从未送到校验器，
   于是六个字段全报缺失、只能拒绝。被拒后它再用散文复述同一份计划，循环往复：
   一次 run 25 轮，全程没有尝试过注入。
"""

from __future__ import annotations

import json

import httpx
import pytest

from harness.bladeai_http import BladeAIHttpClient
from harness.bladeai_http.protocol import config_path
from stage2_service.harness_adapters.bladeai_confirm import plan_from_text


# --- 一、网关凭据走公开配置接口 ------------------------------------------

def _client(handler, **kwargs) -> BladeAIHttpClient:
    http = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://bladeai.test"
    )
    return BladeAIHttpClient("http://bladeai.test", http_client=http, **kwargs)


def test_configure_writes_each_key_separately_and_reports_applied():
    seen: list[tuple[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.url.path.rsplit("/", 1)[-1]
        seen.append((key, json.loads(request.content)["value"]))
        return httpx.Response(200, json={
            "status": "success",
            "data": {"key": key, "hot_reload": True, "rebuild_error": None},
        })

    applied = _client(handler).configure({
        "api_base_url": "http://127.0.0.1:18090/v1",
        "llm_api_key": "relay-token",
    })

    assert applied == {"api_base_url": True, "llm_api_key": True}
    assert seen == [
        ("api_base_url", "http://127.0.0.1:18090/v1"),
        ("llm_api_key", "relay-token"),
    ]


def test_configure_refuses_a_setting_that_needs_a_restart():
    """平台起不了它，也停不了它——热生效不了就必须当场失败，不能静默带病往下跑。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "status": "success",
            "data": {"key": "api_base_url", "hot_reload": False, "rebuild_error": None},
        })

    with pytest.raises(Exception) as excinfo:
        _client(handler).configure({"api_base_url": "http://127.0.0.1:18090/v1"})
    assert "restart" in str(excinfo.value)


def test_a_refusal_arrives_as_http_200_and_must_still_raise():
    """0.7.0 实测：写只读键返回 HTTP 200 + status=fail + code 1002，data 为 null。

    只看状态码会把拒绝当成功。``llm_api_key`` 正是这样一个只读键——
    平台因此无法把每轮的中继令牌交给一个被服务化的 Harness。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "status": "fail",
            "code": 1002,
            "message": "key 'llm_api_key' is not writable via the HTTP API",
            "data": None,
        })

    with pytest.raises(Exception) as excinfo:
        _client(handler).configure({"llm_api_key": "relay-token"})
    assert "not writable" in str(excinfo.value)


def test_a_key_that_omits_hot_reload_is_accepted():
    """只有显式 false 才算失败；不报这个字段的键仍然是写成功了。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "status": "success", "data": {"key": "model_name"},
        })

    assert _client(handler).configure({"model_name": "qwen3.8-max"}) == {"model_name": True}


def test_configure_surfaces_a_rebuild_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "status": "success",
            "data": {"key": "llm_api_key", "hot_reload": True,
                     "rebuild_error": "invalid key"},
        })

    with pytest.raises(Exception) as excinfo:
        _client(handler).configure({"llm_api_key": "x"})
    assert "invalid key" in str(excinfo.value)


def test_configure_records_key_names_but_never_credential_values(tmp_path):
    """凭据不能落进产出物。只记键名。"""
    from harness.bladeai_http import EventLog

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "status": "success",
            "data": {"key": request.url.path.rsplit("/", 1)[-1],
                     "hot_reload": True, "rebuild_error": None},
        })

    log_path = tmp_path / "events.jsonl"
    client = _client(handler, event_log=EventLog(log_path))
    client.configure({"llm_api_key": "sk-super-secret-value"})

    written = log_path.read_text(encoding="utf-8")
    assert "sk-super-secret-value" not in written
    assert "llm_api_key" in written


def test_config_path_matches_the_published_endpoint():
    assert config_path("api_base_url") == "/api/v1/config/api_base_url"


# --- 二、从 Agent 自己的文本里取回计划 ------------------------------------

_REAL_L0_MESSAGE = """
上一轮为 `approved=false / reject`，我未提交任何变更。这一轮递交同一份计划：

```json
{
  "target": {
    "namespace": "otel-demo",
    "name": "cart-7c58f6bb56-jzz9b",
    "uid": "404cdc54-7ff6-498f-bd93-b5ab060dab08",
    "kind": "Pod"
  },
  "fault_type": "cpu-load",
  "intensity": { "cpu_percent": 80 },
  "stop_conditions": ["cart Pod 不再是 Running 或发生重启"],
  "safety_ttl_seconds": 600
}
```

请确认这份修正计划（回复「确认」即提交）。
"""


def test_plan_is_recovered_from_the_real_l0_message():
    plan = plan_from_text([_REAL_L0_MESSAGE])
    assert plan is not None
    assert plan["target"]["uid"] == "404cdc54-7ff6-498f-bd93-b5ab060dab08"
    assert plan["target"]["name"] == "cart-7c58f6bb56-jzz9b"
    assert plan["fault_type"] == "cpu-load"
    assert plan["intensity"] == {"cpu_percent": 80}
    # 越界的 TTL 原样带出：由平台校验器判，不在这里悄悄改。
    assert plan["safety_ttl_seconds"] == 600


def test_the_latest_plan_wins_when_the_agent_restates_it():
    older = _REAL_L0_MESSAGE
    newer = _REAL_L0_MESSAGE.replace('"cpu_percent": 80', '"cpu_percent": 60')
    assert plan_from_text([older, newer])["intensity"] == {"cpu_percent": 60}


def test_non_plan_blocks_are_ignored():
    """它也会用围栏块打印 kubectl 输出和工具参数，那些不是计划。"""
    message = """
先看一眼现状：

```json
{"items": [{"metadata": {"name": "cart-7c58f6bb56-jzz9b"}}]}
```

还需要确认强度再提交。
"""
    assert plan_from_text([message]) is None


def test_a_turn_with_no_json_at_all_is_unaffected():
    assert plan_from_text(["请问下一步该做什么？"]) is None
    assert plan_from_text([]) is None


def test_malformed_json_does_not_raise():
    assert plan_from_text(["```json\n{\"fault_type\": \n```"]) is None


def test_nothing_is_invented_beyond_what_the_agent_wrote():
    """只搬运，不补全：Agent 没写的字段不能凭空出现。"""
    message = '```json\n{"fault_type": "cpu-load"}\n```'
    plan = plan_from_text([message])
    assert plan == {"fault_type": "cpu-load"}
