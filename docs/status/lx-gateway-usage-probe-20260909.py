"""最小探针：记录 LiteLLM 前置/后置钩子各自能拿到什么。"""
import json, os
from pathlib import Path
from litellm.integrations.custom_logger import CustomLogger

OUT = Path(os.environ.get("PROBE_OUT", "probe-result.json"))
HDR = "x-resbench-trial-id"


def _dig_headers(container):
    """在给定 dict 里找 proxy_server_request.headers。"""
    if not isinstance(container, dict):
        return None, None
    psr = container.get("proxy_server_request")
    if isinstance(psr, dict):
        return psr.get("headers"), "proxy_server_request"
    lp = container.get("litellm_params")
    if isinstance(lp, dict):
        psr = lp.get("proxy_server_request")
        if isinstance(psr, dict):
            return psr.get("headers"), "litellm_params.proxy_server_request"
    return None, None


def _record(stage, payload):
    rows = json.loads(OUT.read_text()) if OUT.exists() else []
    rows.append({"stage": stage, **payload})
    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=2))


class Probe(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        headers, where = _dig_headers(data)
        _record("pre_call", {
            "found_headers_at": where,
            "trial_id": (headers or {}).get(HDR),
            "all_resbench_headers": {k: v for k, v in (headers or {}).items()
                                    if k.lower().startswith("x-resbench-")},
            "top_level_keys": sorted(data.keys())[:25],
        })
        return None

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        headers, where = _dig_headers(kwargs)
        usage = None
        u = getattr(response_obj, "usage", None) or (
            response_obj.get("usage") if isinstance(response_obj, dict) else None)
        if u is not None:
            usage = u if isinstance(u, dict) else (
                u.model_dump() if hasattr(u, "model_dump") else dict(u))
        _record("log_success", {
            "found_headers_at": where,
            "trial_id": (headers or {}).get(HDR),
            "all_resbench_headers": {k: v for k, v in (headers or {}).items()
                                    if k.lower().startswith("x-resbench-")},
            "usage": usage,
            "litellm_call_id": kwargs.get("litellm_call_id"),
            "model": kwargs.get("model"),
            "start_time": str(start_time),
            "end_time": str(end_time),
            "duration_s": (end_time - start_time).total_seconds()
                          if start_time and end_time else None,
            "kwargs_keys": sorted(k for k in kwargs.keys() if not k.startswith("_"))[:30],
        })


logger_instance = Probe()


async def _fail(self, kwargs, response_obj, start_time, end_time):
    headers, where = _dig_headers(kwargs)
    _record("log_failure", {
        "found_headers_at": where,
        "trial_id": (headers or {}).get(HDR),
        "all_resbench_headers": {k: v for k, v in (headers or {}).items()
                                if k.lower().startswith("x-resbench-")},
        "usage": None,
        "exception": str(kwargs.get("exception"))[:120],
        "litellm_call_id": kwargs.get("litellm_call_id"),
        "duration_s": (end_time - start_time).total_seconds() if start_time and end_time else None,
    })


Probe.async_log_failure_event = _fail
