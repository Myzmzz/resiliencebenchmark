"""Parser for models.yaml configuration."""

from pathlib import Path
from typing import Optional

import yaml

from backend.models.model_config import (
    CapabilityProbe,
    CredentialRef,
    ModelConfig,
    ModelsRegistry,
)


def parse_models(repo_path: Path) -> Optional[ModelsRegistry]:
    """
    Parse models.yaml configuration.

    Args:
        repo_path: Path to the resiliencebenchmark repository

    Returns:
        ModelsRegistry object or None if file doesn't exist
    """
    models_file = repo_path / "harness" / "models.yaml"

    if not models_file.exists():
        return None

    try:
        with open(models_file, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not data:
            return None

        # Parse credential refs
        credential_refs = {}
        for ref_name, ref_data in data.get("credential_refs", {}).items():
            credential_refs[ref_name] = CredentialRef(**ref_data)

        # Parse models
        models = []
        for model_id, model_data in data.get("models", {}).items():
            # Add model ID to the data
            model_data_with_id = {"id": model_id, **model_data}

            # Parse capability probe if present
            if "capability_probe" in model_data and isinstance(
                model_data["capability_probe"], dict
            ):
                capability_probe = CapabilityProbe(**model_data["capability_probe"])
                model_data_with_id["capability_probe"] = capability_probe

            # Add default credential_ref if not specified
            if "credential_ref" not in model_data_with_id:
                default_credential = data.get("defaults", {}).get("credential_ref")
                if default_credential:
                    model_data_with_id["credential_ref"] = default_credential

            models.append(ModelConfig(**model_data_with_id))

        return ModelsRegistry(
            version=data.get("version", "unknown"),
            description=data.get("description", ""),
            credential_refs=credential_refs,
            defaults=data.get("defaults", {}),
            models=models,
        )

    except Exception as e:
        print(f"Error parsing models.yaml: {e}")
        return None
