"""Parser for harnesses.yaml configuration."""

from pathlib import Path
from typing import Optional

import yaml

from backend.models.harness import (
    HarnessConfig,
    HarnessEntrypoint,
    HarnessMCP,
    HarnessModels,
    HarnessSafety,
    HarnessVersionPin,
    HarnessesRegistry,
)


def parse_harnesses(repo_path: Path) -> Optional[HarnessesRegistry]:
    """
    Parse harnesses.yaml configuration.

    Args:
        repo_path: Path to the resiliencebenchmark repository

    Returns:
        HarnessesRegistry object or None if file doesn't exist
    """
    harnesses_file = repo_path / "harness" / "harnesses.yaml"

    if not harnesses_file.exists():
        return None

    try:
        with open(harnesses_file, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not data:
            return None

        # Parse harnesses
        harnesses = []
        for harness_id, harness_data in data.get("harnesses", {}).items():
            # Add harness ID to the data
            harness_data_with_id = {"id": harness_id, **harness_data}

            # Parse nested structures
            if "entrypoint" in harness_data:
                entrypoint = HarnessEntrypoint(**harness_data["entrypoint"])
                harness_data_with_id["entrypoint"] = entrypoint

            if "mcp" in harness_data:
                mcp = HarnessMCP(**harness_data["mcp"])
                harness_data_with_id["mcp"] = mcp

            if "models" in harness_data:
                models = HarnessModels(**harness_data["models"])
                harness_data_with_id["models"] = models

            if "safety" in harness_data:
                safety = HarnessSafety(**harness_data["safety"])
                harness_data_with_id["safety"] = safety

            if "version_pin" in harness_data:
                version_pin = HarnessVersionPin(**harness_data["version_pin"])
                harness_data_with_id["version_pin"] = version_pin

            harnesses.append(HarnessConfig(**harness_data_with_id))

        return HarnessesRegistry(
            version=data.get("version", "unknown"),
            description=data.get("description", ""),
            shared=data.get("shared", {}),
            harnesses=harnesses,
        )

    except Exception as e:
        print(f"Error parsing harnesses.yaml: {e}")
        return None
