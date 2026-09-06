"""Immutable LiteLLM gateway route snapshot for Stage-2 runtime evidence."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

import yaml


class GatewayConfigError(ValueError):
    """Raised when the LiteLLM route table is not acceptable for Stage-2."""


_ENV_REF = re.compile(r"^[A-Z_][A-Z0-9_]*$")


@dataclass(frozen=True)
class GatewayRoute:
    model_alias: str
    provider: str
    upstream_model: str
    api_base_host: str
    api_base_scheme: str
    api_base_path: str
    credential_env_ref: str

    def as_public_dict(self) -> dict[str, str]:
        return {
            "model_alias": self.model_alias,
            "provider": self.provider,
            "upstream_model": self.upstream_model,
            "api_base_host": self.api_base_host,
            "api_base_scheme": self.api_base_scheme,
            "api_base_path": self.api_base_path,
            "credential_env_ref": self.credential_env_ref,
        }


@dataclass(frozen=True)
class GatewayConfigSnapshot:
    config_path: Path
    config_sha256: str
    _routes: Mapping[str, GatewayRoute]
    _required_aliases: tuple[str, ...]

    @classmethod
    def from_file(
        cls,
        path: Path,
        *,
        required_aliases: Sequence[str],
    ) -> "GatewayConfigSnapshot":
        source = Path(path)
        try:
            raw = source.read_bytes()
        except OSError as exc:
            raise GatewayConfigError(
                f"LiteLLM gateway config is not readable: {type(exc).__name__}"
            ) from exc
        try:
            document = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise GatewayConfigError("LiteLLM gateway config is not valid YAML") from exc
        if not isinstance(document, dict):
            raise GatewayConfigError("LiteLLM gateway config must be a mapping")
        _reject_active_routing_policy(document)
        model_list = document.get("model_list")
        if not isinstance(model_list, list) or not model_list:
            raise GatewayConfigError("LiteLLM gateway config must define a non-empty model_list")

        routes: dict[str, GatewayRoute] = {}
        for index, item in enumerate(model_list):
            if not isinstance(item, dict):
                raise GatewayConfigError(f"model_list[{index}] must be a mapping")
            alias = item.get("model_name")
            if not isinstance(alias, str) or not alias:
                raise GatewayConfigError(f"model_list[{index}] must define model_name")
            if alias in routes:
                raise GatewayConfigError(f"duplicate LiteLLM model route for alias {alias!r}")
            routes[alias] = _parse_route(alias, item, index)

        required = tuple(required_aliases)
        missing = [alias for alias in required if alias not in routes]
        if missing:
            raise GatewayConfigError(
                "LiteLLM gateway config is missing required alias(es): " + ", ".join(missing)
            )
        return cls(
            config_path=source.resolve(),
            config_sha256=hashlib.sha256(raw).hexdigest(),
            _routes=MappingProxyType(dict(routes)),
            _required_aliases=required,
        )

    @property
    def required_aliases(self) -> tuple[str, ...]:
        return self._required_aliases

    @property
    def model_aliases(self) -> tuple[str, ...]:
        return tuple(self._routes)

    def route(self, alias: str) -> dict[str, str]:
        try:
            return self._routes[alias].as_public_dict()
        except KeyError as exc:
            raise GatewayConfigError(f"unknown LiteLLM model alias {alias!r}") from exc

    def required_routes(self) -> dict[str, dict[str, str]]:
        return {alias: self.route(alias) for alias in self._required_aliases}


def _reject_active_routing_policy(document: Mapping[str, Any]) -> None:
    router_settings = document.get("router_settings")
    if router_settings not in (None, {}, []):
        raise GatewayConfigError("LiteLLM router_settings must not enable fallback or load balancing")
    for key in ("fallbacks", "context_window_fallbacks", "model_group_alias"):
        value = document.get(key)
        if value not in (None, {}, []):
            raise GatewayConfigError(f"LiteLLM {key} must not be configured for Stage-2")


def _parse_route(alias: str, item: Mapping[str, Any], index: int) -> GatewayRoute:
    params = item.get("litellm_params")
    if not isinstance(params, dict):
        raise GatewayConfigError(f"model_list[{index}] must define litellm_params")
    for key in ("fallbacks", "context_window_fallbacks"):
        value = params.get(key)
        if value not in (None, {}, []):
            raise GatewayConfigError(f"model route {alias!r} must not define {key}")

    model = params.get("model")
    if not isinstance(model, str) or "/" not in model:
        raise GatewayConfigError(f"model route {alias!r} must define litellm_params.model as provider/model")
    provider, upstream_model = model.split("/", 1)
    if not provider or not upstream_model:
        raise GatewayConfigError(f"model route {alias!r} has an invalid provider/model value")

    api_base = params.get("api_base")
    if not isinstance(api_base, str) or not api_base:
        raise GatewayConfigError(f"model route {alias!r} must define litellm_params.api_base")
    parsed = urlparse(api_base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise GatewayConfigError(f"model route {alias!r} api_base must be an explicit http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise GatewayConfigError(
            f"model route {alias!r} api_base must not contain userinfo, query, or fragment"
        )

    api_key = params.get("api_key")
    if not isinstance(api_key, str) or not api_key.startswith("os.environ/"):
        raise GatewayConfigError(f"model route {alias!r} api_key must be an os.environ/NAME reference")
    credential_env_ref = api_key.removeprefix("os.environ/")
    if not _ENV_REF.fullmatch(credential_env_ref):
        raise GatewayConfigError(f"model route {alias!r} api_key env reference is invalid")

    return GatewayRoute(
        model_alias=alias,
        provider=provider,
        upstream_model=upstream_model,
        api_base_host=parsed.hostname.lower(),
        api_base_scheme=parsed.scheme,
        api_base_path=parsed.path or "",
        credential_env_ref=credential_env_ref,
    )
