"""Portable ownership normalization for the controller/Agent shared Trial tree."""

from __future__ import annotations

import os
import stat
from pathlib import Path


DEFAULT_SHARED_TRIAL_GID = 10004
CODEX_ARG0_ALIAS_TARGET = (
    "/usr/local/lib/node_modules/@openai/codex/node_modules/"
    "@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex"
)
CODEX_ARG0_ALIAS_NAMES = frozenset({
    "applypatch",
    "apply_patch",
    "codex-execve-wrapper",
    "codex-linux-sandbox",
})
DSH_NODE_MODULES_REL_PREFIX = ("dsh-home", "profiles", "node_modules")
DSH_NODE_MODULES_TARGET_ROOT = "/opt/resiliencebenchmark/deepseek-harness/node_modules"
# The locked DSH distribution resolves this package from its frontend's
# nested dependency tree. Observed in the real 0.1.0-rc.7 profile cache.
DSH_NESTED_PACKAGE_TARGETS = {
    "@deepseek-ai/dsh-client-web": (
        f"{DSH_NODE_MODULES_TARGET_ROOT}/@deepseek-ai/dsh-web-frontend/"
        "node_modules/@deepseek-ai/dsh-client-web"
    ),
}


def normalize_shared_trial_tree(
    cwd: Path,
    shared_gid: int = DEFAULT_SHARED_TRIAL_GID,
) -> None:
    """Make regular Agent output controller-readable without following links.

    The shared Trial root is distinct from every Controller-private directory.
    Ordinary symlinks and non-regular entries are rejected.  The exact known
    runtime cache links for Codex argv0 aliases and DSH package aliases are
    verified by their link text only; their targets are never followed or
    chmod/chown'ed.  Call this after the Agent exits and before terminal
    evidence is accepted.
    """
    if shared_gid <= 0:
        raise RuntimeError("shared Trial group is invalid")
    entries = [cwd]
    errors: list[str] = []
    while entries:
        current = entries.pop()
        try:
            metadata = current.lstat()
        except OSError:
            errors.append("shared Trial output metadata is not controller-normalized")
            continue
        if stat.S_ISLNK(metadata.st_mode):
            if _is_allowed_agent_symlink(current, cwd):
                continue
            errors.append("Agent Trial output may not contain symlinks")
            continue
        if stat.S_ISDIR(metadata.st_mode):
            try:
                _ensure_metadata(current, metadata, shared_gid, 0o2770)
            except RuntimeError as exc:
                errors.append(str(exc))
            try:
                with os.scandir(current) as children:
                    entries.extend(Path(child.path) for child in children)
            except OSError:
                errors.append("shared Trial output directory is not controller-normalized")
            continue
        if stat.S_ISREG(metadata.st_mode):
            # Keep an executable bit when an Agent generated a helper, while
            # making ordinary 0600 final reports readable to Controller group.
            owner_bits = stat.S_IMODE(metadata.st_mode) & 0o700
            try:
                _ensure_metadata(current, metadata, shared_gid, owner_bits | 0o060)
            except RuntimeError as exc:
                errors.append(str(exc))
            continue
        errors.append("Agent Trial output contains an unsupported file type")
    if errors:
        raise RuntimeError("; ".join(dict.fromkeys(errors)))


def _is_allowed_agent_symlink(path: Path, root: Path) -> bool:
    return _is_codex_arg0_alias(path, root) or _is_dsh_node_modules_package_alias(path, root)


def _is_codex_arg0_alias(path: Path, root: Path) -> bool:
    if path.name not in CODEX_ARG0_ALIAS_NAMES:
        return False
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    parts = relative.parts
    if len(parts) < 5:
        return False
    if parts[-4] != "tmp" or parts[-3] != "arg0" or not parts[-2].startswith("codex-arg0"):
        return False
    if parts[-2] == "codex-arg0":
        return False
    try:
        target = os.readlink(path)
    except OSError:
        return False
    return target == CODEX_ARG0_ALIAS_TARGET


def _is_dsh_node_modules_package_alias(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    parts = relative.parts
    prefix_len = len(DSH_NODE_MODULES_REL_PREFIX)
    if parts[:prefix_len] != DSH_NODE_MODULES_REL_PREFIX:
        return False
    package_parts = parts[prefix_len:]
    if not _is_dsh_package_parts(package_parts):
        return False
    package = "/".join(package_parts)
    expected = DSH_NESTED_PACKAGE_TARGETS.get(package, f"{DSH_NODE_MODULES_TARGET_ROOT}/{package}")
    try:
        target = os.readlink(path)
    except OSError:
        return False
    return target == expected


def _is_dsh_package_parts(parts: tuple[str, ...]) -> bool:
    if len(parts) == 1:
        return _is_npm_name_component(parts[0]) and not parts[0].startswith("@")
    if len(parts) == 2:
        scope, name = parts
        return (
            scope.startswith("@")
            and _is_npm_name_component(scope[1:])
            and _is_npm_name_component(name)
        )
    return False


def _is_npm_name_component(value: str) -> bool:
    return bool(value) and value not in {".", ".."} and all(
        char.isalnum() or char in "._-" for char in value
    )


def _ensure_metadata(path: Path, metadata: os.stat_result, shared_gid: int, mode: int) -> None:
    """Avoid mutating already-normalized Agent-owned outputs from Controller."""
    current_mode = stat.S_IMODE(metadata.st_mode)
    try:
        if metadata.st_gid != shared_gid:
            os.chown(path, -1, shared_gid, follow_symlinks=False)
        if current_mode != mode:
            os.chmod(path, mode, follow_symlinks=False)
    except PermissionError as exc:
        raise RuntimeError("shared Trial output metadata is not controller-normalized") from exc
