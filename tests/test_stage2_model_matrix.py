from pathlib import Path

import pytest
from pydantic import ValidationError

from stage2_service.contracts import (
    CampaignRequest,
    FixedEpisodeRef,
    HarnessKind,
    HarnessModelSelection,
    ModelProtocol,
)
from stage2_service.model_matrix import (
    ModelMatrixCompiler,
    ModelMatrixError,
    bind_provider_identity,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY = REPO_ROOT / "harness/models.yaml"


def request_for(harness: HarnessKind, *models: str) -> CampaignRequest:
    return CampaignRequest(
        request_id="model-matrix-test",
        episode=FixedEpisodeRef(
            internal_path="episode-internal.yaml",
            public_path="episode-public.yaml",
            episode_id="EPI-MODEL-MATRIX-001",
            internal_sha256="a" * 64,
            public_sha256="b" * 64,
        ),
        model_matrix=(HarnessModelSelection(harness=harness, models=models),),
    )


@pytest.mark.parametrize(
    ("harness", "model", "protocol"),
    [
        (HarnessKind.CODEX, "gpt-5.6-sol", ModelProtocol.OPENAI_RESPONSES),
        (
            HarnessKind.CLAUDE_CODE,
            "claude-opus-5",
            ModelProtocol.ANTHROPIC_MESSAGES,
        ),
        (
            HarnessKind.CLAUDE_CODE,
            "deepseek-v4-pro",
            ModelProtocol.ANTHROPIC_MESSAGES,
        ),
        (
            HarnessKind.DEEPSEEK,
            "claude-opus-5",
            ModelProtocol.OPENAI_CHAT_COMPLETIONS,
        ),
        (
            HarnessKind.BLADEAI,
            "gpt-5.6",
            ModelProtocol.OPENAI_CHAT_COMPLETIONS,
        ),
    ],
)
def test_compiles_only_declared_harness_protocols(harness, model, protocol):
    variant = ModelMatrixCompiler(REGISTRY).compile(request_for(harness, model))[0]

    assert variant.harness is harness
    assert variant.identity.requested_alias == model
    assert variant.identity.provider_protocol is protocol
    assert len(variant.identity.registry_sha256) == 64
    assert model.lower().replace(".", "-")[:3] in variant.variant_id


@pytest.mark.parametrize(
    ("harness", "model"),
    [
        (HarnessKind.CODEX, "claude-opus-5"),
        (HarnessKind.CLAUDE_CODE, "gpt-5.6"),
        (HarnessKind.DEEPSEEK, "gpt-5.5"),
        (HarnessKind.BLADEAI, "gpt-5.5"),
    ],
)
def test_rejects_protocol_incompatible_matrix_cell(harness, model):
    with pytest.raises(ModelMatrixError, match="incompatible"):
        ModelMatrixCompiler(REGISTRY).compile(request_for(harness, model))


def test_campaign_contract_rejects_duplicate_harness_or_model_entries():
    with pytest.raises(ValidationError, match="models must be unique"):
        request_for(HarnessKind.CODEX, "gpt-5.6", "gpt-5.6")

    with pytest.raises(ValidationError, match="Harness selections must be unique"):
        CampaignRequest(
            request_id="duplicate-harness-test",
            episode=FixedEpisodeRef(
                internal_path="episode-internal.yaml",
                public_path="episode-public.yaml",
                episode_id="EPI-MODEL-MATRIX-001",
                internal_sha256="a" * 64,
                public_sha256="b" * 64,
            ),
            model_matrix=(
                HarnessModelSelection(
                    harness=HarnessKind.CODEX, models=("gpt-5.6",)
                ),
                HarnessModelSelection(
                    harness=HarnessKind.CODEX, models=("gpt-5.5",)
                ),
            ),
        )


def test_provider_identity_mismatch_is_explicit():
    identity = ModelMatrixCompiler(REGISTRY).compile(
        request_for(HarnessKind.CODEX, "gpt-5.6")
    )[0].identity

    verified = bind_provider_identity(identity, "gpt-5.6")
    mismatch = bind_provider_identity(identity, "different-upstream-model")

    assert verified.verification == "provider_verified"
    assert mismatch.verification == "provider_mismatch"
    assert mismatch.provider_reported_model == "different-upstream-model"


def test_gateway_catalog_freezes_provider_identity_before_trials():
    compiler = ModelMatrixCompiler(
        REGISTRY,
        provider_models_loader=lambda: {"gpt-5.6-sol", "claude-opus-5"},
    )

    identity = compiler.compile(
        request_for(HarnessKind.CODEX, "gpt-5.6-sol")
    )[0].identity

    assert identity.verification == "provider_verified"
    assert identity.provider_reported_model == "gpt-5.6-sol"


def test_gateway_catalog_missing_model_blocks_before_trials():
    compiler = ModelMatrixCompiler(
        REGISTRY,
        provider_models_loader=lambda: {"some-other-model"},
    )

    with pytest.raises(ModelMatrixError, match="not reported by the gateway"):
        compiler.compile(request_for(HarnessKind.CODEX, "gpt-5.6"))


def test_gateway_catalog_failure_is_a_platform_error_without_secret_text():
    def fail():
        raise RuntimeError("secret-bearing provider failure")

    compiler = ModelMatrixCompiler(REGISTRY, provider_models_loader=fail)

    with pytest.raises(
        ModelMatrixError, match="gateway model catalog is unavailable: RuntimeError"
    ) as captured:
        compiler.compile(request_for(HarnessKind.CODEX, "gpt-5.6"))
    assert "secret-bearing" not in str(captured.value)
