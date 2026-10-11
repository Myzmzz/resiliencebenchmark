"""HTTP client for one Controller instance's existing Lx API.

Fleet adds no endpoint to the Controller: a batch item is a POST to
``/api/v1/stage2/lx/runs`` and the rest is polling the endpoints that already
exist. The canonical prompt for a replica comes from that replica's own
``/lx/prompt-variants``, so no prompt text is ever stored here.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Mapping


class ControllerError(RuntimeError):
    """A Controller call failed; ``status`` and ``payload`` carry its answer."""

    def __init__(self, message: str, *, status: int = 0, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.payload = payload

    @property
    def retryable(self) -> bool:
        """Transport problems and 503 are the platform's fault, not the agent's."""
        return self.status in {0, 408, 425, 429, 500, 502, 503, 504}


class ControllerClient:
    def __init__(self, base_url: str, *, timeout: float = 30.0, opener=None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._opener = opener or urllib.request.urlopen

    def _request(self, method: str, path: str, body: Mapping[str, Any] | None = None,
                 headers: Mapping[str, str] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        data = json.dumps(dict(body), ensure_ascii=False).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with self._opener(request, timeout=self.timeout) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                payload = json.loads(raw) if raw else {}
            except ValueError:
                payload = {"detail": raw.decode("utf-8", errors="replace")[:2000]}
            raise ControllerError(
                f"{method} {path} returned {exc.code}", status=exc.code, payload=payload
            ) from exc
        except Exception as exc:  # noqa: BLE001 - transport failures are platform failures
            raise ControllerError(f"{method} {path} failed: {type(exc).__name__}", status=0) from exc

    def healthz(self) -> Any:
        return self._request("GET", "/healthz")

    def options(self) -> Any:
        return self._request("GET", "/api/v1/stage2/options")

    def preflight(self) -> Any:
        return self._request("GET", "/api/v1/preflight")

    def autonomy_cases(self) -> Any:
        return self._request("GET", "/api/v1/stage2/autonomy/cases")

    def prompt_variants(self, application: str, slots: Mapping[str, Any]) -> Any:
        return self._request(
            "POST", "/api/v1/stage2/lx/prompt-variants",
            {"application": application, "slots": dict(slots)},
        )

    def create_run(self, body: Mapping[str, Any], *, idempotency_key: str | None = None) -> Any:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        return self._request("POST", "/api/v1/stage2/lx/runs", body, headers)

    def run(self, run_id: str) -> Any:
        return self._request("GET", f"/api/v1/stage2/lx/runs/{run_id}")

    def score(self, run_id: str) -> Any:
        return self._request("GET", f"/api/v1/stage2/lx/runs/{run_id}/score")

    def stop_run(self, run_id: str) -> Any:
        return self._request("POST", f"/api/v1/stage2/lx/runs/{run_id}/stop", {})

    def reset_environment(self, task_id: str) -> Any:
        return self._request("POST", f"/api/v1/stage2/tasks/{task_id}/environment/reset", {})

    def tasks(self) -> Any:
        return self._request("GET", "/api/v1/stage2/tasks")
