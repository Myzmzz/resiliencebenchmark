"""Load the built-in static permission profiles shipped with Stage-2."""

from __future__ import annotations

from pathlib import Path

import yaml

from .contracts import PermissionProfile


DEFAULT_PROFILE_ROOT = Path(__file__).resolve().parents[1] / "harness/permission-profiles"


def load_permission_profiles(
    root: Path = DEFAULT_PROFILE_ROOT,
) -> tuple[PermissionProfile, ...]:
    profiles: list[PermissionProfile] = []
    seen: set[str] = set()
    for path in sorted(root.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        profile = PermissionProfile.model_validate(raw)
        if profile.profile_id in seen:
            raise ValueError(f"duplicate permission profile id: {profile.profile_id}")
        seen.add(profile.profile_id)
        profiles.append(profile)
    if not profiles:
        raise ValueError(f"no permission profiles found under {root}")
    return tuple(profiles)


def get_permission_profile(
    profile_id: str,
    root: Path = DEFAULT_PROFILE_ROOT,
) -> PermissionProfile:
    for profile in load_permission_profiles(root):
        if profile.profile_id == profile_id:
            return profile
    raise KeyError(profile_id)
