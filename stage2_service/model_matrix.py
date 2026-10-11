"""Compile a frozen Harness-model matrix from the repository model registry."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

from .contracts import (
    CampaignRequest,
    HarnessKind,
    ModelIdentity,
    ModelProtocol,
)


class ModelMatrixError(ValueError):
    pass


HARNESS_PROTOCOLS: Mapping[HarnessKind, tuple[ModelProtocol, ...]] = {
    HarnessKind.CODEX: (ModelProtocol.OPENAI_RESPONSES,),
    HarnessKind.CLAUDE_CODE: (ModelProtocol.ANTHROPIC_MESSAGES,),
    HarnessKind.DEEPSEEK: (ModelProtocol.OPENAI_CHAT_COMPLETIONS,),
    HarnessKind.BLADEAI: (ModelProtocol.OPENAI_CHAT_COMPLETIONS,),
}


@dataclass(frozen=True)
class CompiledModelVariant:
    harness: HarnessKind
    identity: ModelIdentity
    variant_id: str


class ModelMatrixCompiler:
    def __init__(
        self,
        registry_path: Path,
        *,
        provider_models_loader: Callable[[], set[str]] | None = None,
    ):
        path = registry_path.resolve()
        if not path.is_file() or path.is_symlink():
            raise ModelMatrixError("model registry must be an existing regular file")
        raw = path.read_bytes()
        payload = yaml.safe_load(raw)
        if not isinstance(payload, Mapping) or not isinstance(payload.get("models"), Mapping):
            raise ModelMatrixError("model registry does not contain a models mapping")
        self.registry_path = path
        self.registry_sha256 = hashlib.sha256(raw).hexdigest()
        self.models: Mapping[str, Any] = payload["models"]
        self.provider_models_loader = provider_models_loader

    def compile(self, request: CampaignRequest) -> tuple[CompiledModelVariant, ...]:
        requested = [
            (selection.harness, alias)
            for selection in request.model_matrix
            for alias in selection.models
        ]
        if not requested:
            raise ModelMatrixError("model matrix must contain at least one Harness-model variant")
        variants = tuple(
            self._compile_variant(harness, alias, provider_models=None)
            for harness, alias in requested
        )
        if self.provider_models_loader is None:
            return variants
        try:
            provider_models = self.provider_models_loader()
        except ModelMatrixError:
            raise
        except Exception as exc:  # noqa: BLE001 - provider errors are platform evidence.
            raise ModelMatrixError(
                f"gateway model catalog is unavailable: {type(exc).__name__}"
            ) from exc
        if not provider_models:
            raise ModelMatrixError("gateway model catalog returned no model ids")
        return tuple(
            self._compile_variant(
                harness,
                alias,
                provider_models=provider_models,
            )
            for harness, alias in requested
        )

    def _compile_variant(
        self,
        harness: HarnessKind,
        alias: str,
        *,
        provider_models: set[str] | None,
    ) -> CompiledModelVariant:
        spec = self.models.get(alias)
        if not isinstance(spec, Mapping):
            raise ModelMatrixError(f"model alias is not registered: {alias}")
        upstream = str(spec.get("upstream_model") or "")
        if not upstream:
            raise ModelMatrixError(f"model alias has no upstream_model: {alias}")
        raw_candidates = spec.get("protocol_candidates")
        if not isinstance(raw_candidates, list) or not raw_candidates:
            raise ModelMatrixError(f"model alias has no protocol candidates: {alias}")
        candidates: set[ModelProtocol] = set()
        for raw in raw_candidates:
            try:
                candidates.add(ModelProtocol(str(raw)))
            except ValueError as exc:
                raise ModelMatrixError(
                    f"model alias declares an unknown protocol: {alias}={raw}"
                ) from exc
        protocol = next(
            (
                candidate
                for candidate in HARNESS_PROTOCOLS[harness]
                if candidate in candidates
            ),
            None,
        )
        if protocol is None:
            accepted = ", ".join(item.value for item in HARNESS_PROTOCOLS[harness])
            declared = ", ".join(sorted(item.value for item in candidates))
            raise ModelMatrixError(
                f"model {alias} is incompatible with {harness.value}: "
                f"requires [{accepted}], registry declares [{declared}]"
            )
        slug = re.sub(r"[^a-z0-9]+", "-", alias.lower()).strip("-")[:48]
        suffix = hashlib.sha256(alias.encode("utf-8")).hexdigest()[:8]
        provider_model: str | None = None
        verification = "registry_frozen"
        if provider_models is not None:
            provider_model = next(
                (
                    candidate
                    for candidate in (upstream, alias)
                    if candidate in provider_models
                ),
                None,
            )
            if provider_model is None:
                raise ModelMatrixError(
                    f"model {alias} is not reported by the gateway model catalog"
                )
            verification = "provider_verified"
        identity = ModelIdentity(
            requested_alias=alias,
            upstream_model=upstream,
            provider_protocol=protocol,
            registry_sha256=self.registry_sha256,
            provider_reported_model=provider_model,
            verification=verification,
        )
        return CompiledModelVariant(
            harness=harness,
            identity=identity,
            variant_id=f"{harness.value}-{slug}-{suffix}",
        )


def bind_provider_identity(
    identity: ModelIdentity, provider_reported_model: str | None
) -> ModelIdentity:
    if not provider_reported_model:
        return identity
    expected = {identity.requested_alias, identity.upstream_model}
    verification = (
        "provider_verified"
        if provider_reported_model in expected
        else "provider_mismatch"
    )
    return identity.model_copy(
        update={
            "provider_reported_model": provider_reported_model,
            "verification": verification,
        }
    )


def load_gateway_model_ids(
    base_url: str,
    api_key: str,
    *,
    timeout_seconds: int = 15,
) -> set[str]:
    url = f"{base_url.rstrip('/')}/models"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ModelMatrixError(
            f"gateway model catalog request failed: {type(exc).__name__}"
        ) from exc
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(data, list):
        raise ModelMatrixError("gateway model catalog response has no data list")
    return {
        str(item["id"])
        for item in data
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }
