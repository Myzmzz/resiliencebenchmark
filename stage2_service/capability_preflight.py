"""Read qualified Harness capability records without probing a live environment.

The adapter declaration tells us only which event shape the code understands.
It is intentionally insufficient to claim a Harness is runnable.  A deployer
must supply a fresh qualification record produced by the qualification flow.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .contracts import HarnessKind
from .harness_adapters import create_adapter
from .harness_adapters.base import HarnessCapability


CAPABILITY_QUALIFICATION_SCHEMA = "stage2-harness-capabilities.v1"


def harness_capabilities_from_qualification(
    qualification_file: Path | None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Return one descriptor per Harness and a transparent source summary.

    Missing, malformed, or partial evidence never raises the readiness of an
    adapter: every affected descriptor remains ``qualification_passed=false``.
    This function performs no process, network, or cluster probe.
    """
    entries, source = _read_entries(qualification_file)
    descriptors: dict[str, dict[str, Any]] = {}
    for harness in HarnessKind:
        declared = create_adapter(harness).capability()
        entry = entries.get(harness.value)
        descriptor, outcome = _qualified_descriptor(harness, declared, entry)
        descriptors[harness.value] = descriptor.model_dump(mode="json")
        source["harnesses"][harness.value] = outcome
    return descriptors, source


def _read_entries(
    qualification_file: Path | None,
) -> tuple[Mapping[str, Any], dict[str, Any]]:
    source: dict[str, Any] = {
        "schema_version": "stage2-harness-capability-preflight.v1",
        "qualification_file": str(qualification_file) if qualification_file else None,
        "status": "capability_probe_missing",
        "harnesses": {},
    }
    if qualification_file is None:
        return {}, source
    if not qualification_file.is_file():
        source["status"] = "capability_probe_file_missing"
        return {}, source
    try:
        payload = json.loads(qualification_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        source["status"] = "capability_probe_file_invalid"
        return {}, source
    if not isinstance(payload, Mapping):
        source["status"] = "capability_probe_file_invalid"
        return {}, source
    if payload.get("schema_version") != CAPABILITY_QUALIFICATION_SCHEMA:
        source["status"] = "capability_probe_schema_invalid"
        return {}, source
    entries = payload.get("harnesses")
    if not isinstance(entries, Mapping):
        source["status"] = "capability_probe_file_invalid"
        return {}, source
    source["status"] = "qualification_records_loaded"
    return entries, source


def _qualified_descriptor(
    harness: HarnessKind,
    declared: HarnessCapability,
    entry: Any,
) -> tuple[HarnessCapability, dict[str, Any]]:
    if not isinstance(entry, Mapping):
        return _unqualified(declared, "capability_probe_missing")
    qualification = entry.get("qualification")
    capability = entry.get("capability")
    if not isinstance(qualification, Mapping) or not isinstance(capability, Mapping):
        return _unqualified(declared, "capability_probe_record_invalid")
    status = qualification.get("status")
    evidence_ref = qualification.get("evidence_ref")
    if status != "passed" or not isinstance(evidence_ref, str) or not evidence_ref.strip():
        return _unqualified(declared, "qualification_not_passed")
    data = dict(capability)
    if data.get("kind") not in {None, harness.value}:
        return _unqualified(declared, "capability_probe_kind_mismatch")
    data["kind"] = harness.value
    data["qualification_passed"] = True
    probe = data.get("probe")
    data["probe"] = {
        **(dict(probe) if isinstance(probe, Mapping) else {}),
        "qualification_status": "passed",
        "qualification_evidence_ref": evidence_ref.strip(),
    }
    try:
        return HarnessCapability.model_validate(data), {
            "status": "qualified",
            "evidence_ref": evidence_ref.strip(),
        }
    except Exception:  # The caller must see an unavailable capability, not a crash.
        return _unqualified(declared, "capability_probe_record_invalid")


def _unqualified(
    declared: HarnessCapability, reason: str
) -> tuple[HarnessCapability, dict[str, Any]]:
    return declared.model_copy(
        update={
            "qualification_passed": False,
            "probe": {**declared.probe, "qualification_status": reason},
        }
    ), {"status": reason}
