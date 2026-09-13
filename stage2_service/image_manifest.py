"""Derive the runtime image's file manifest from the Dockerfile that builds it.

The controller image's script list used to be written out by hand in two places:
the ``COPY`` lines of ``deploy/stage2/Dockerfile.runtime-overlay`` and the file
list inside ``scripts/build_stage2_image.py``. Adding a script to one and not
the other produced an image whose source digest did not change, so the release
kept the base image's older copy — which is how the 2026-09-11 round shipped a
``deploy_application.py`` that rejected ``--server-dry-run`` (O18).

The Dockerfile is the only place that decides what is in the image, so it is the
only place this list is read from. Three checks use the same parse:

* build    - every ``COPY`` source must exist, and the digest covers all of them;
* deploy   - the built metadata records each file's sha256 to compare against;
* start-up - every ``COPY`` destination must be present in the running image,
             and the recorded revision must match the image's own label.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


MANIFEST_SCHEMA = "stage2-runtime-manifest.v1"
DEFAULT_DOCKERFILE = Path("deploy/stage2/Dockerfile.runtime-overlay")
# Produced by the frontend build just before ``docker build``, so it is absent
# from a clean checkout and its contents differ per build. It is still declared
# here, so an undeclared missing source stays an error.
GENERATED_SOURCES: tuple[str, ...] = ("frontend/dist",)
IMAGE_ROOT = Path("/app")
REVISION_ENV = "RESBENCH_SOURCE_HEAD"


class ImageManifestError(RuntimeError):
    """The image does not contain what the Dockerfile says it contains."""


@dataclass(frozen=True)
class CopyEntry:
    sources: tuple[str, ...]
    destination: str

    def destinations(self) -> tuple[str, ...]:
        """Expand a multi-source COPY into one destination path per source."""
        if len(self.sources) == 1 and not self.destination.endswith("/"):
            return (self.destination,)
        base = self.destination.rstrip("/")
        return tuple(f"{base}/{Path(source).name}" for source in self.sources)

    def pairs(self) -> tuple[tuple[str, str], ...]:
        return tuple(zip(self.sources, self.destinations()))


def parse_copy_entries(dockerfile_text: str) -> tuple[CopyEntry, ...]:
    """Read every local ``COPY`` instruction, ignoring flags and comments."""
    entries: list[CopyEntry] = []
    for statement in _logical_lines(dockerfile_text):
        if not statement.upper().startswith("COPY "):
            continue
        try:
            tokens = shlex.split(statement)[1:]
        except ValueError:
            continue
        from_another_stage = any(token.startswith("--from=") for token in tokens)
        operands = [token for token in tokens if not token.startswith("--")]
        if from_another_stage or len(operands) < 2:
            # Stage-to-stage copies carry no repository source to verify.
            continue
        entries.append(
            CopyEntry(sources=tuple(operands[:-1]), destination=operands[-1])
        )
    return tuple(entries)


def _logical_lines(text: str) -> Iterable[str]:
    """Join backslash continuations so a wrapped COPY parses as one statement."""
    buffer = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            buffer += line[:-1].strip() + " "
            continue
        yield (buffer + line).strip()
        buffer = ""
    if buffer.strip():
        yield buffer.strip()


def copied_sources(dockerfile_text: str) -> tuple[str, ...]:
    """Every repository path the Dockerfile copies, in sorted order."""
    sources = {source for entry in parse_copy_entries(dockerfile_text) for source in entry.sources}
    return tuple(sorted(sources))


def build_manifest(
    repo_root: Path,
    *,
    dockerfile: Path = DEFAULT_DOCKERFILE,
    revision: str = "unknown",
    image_root: Path = IMAGE_ROOT,
    generated: Sequence[str] = GENERATED_SOURCES,
) -> dict[str, Any]:
    """Hash every file the Dockerfile copies; a missing source fails the build."""
    repo_root = Path(repo_root).resolve()
    dockerfile_path = repo_root / dockerfile
    try:
        text = dockerfile_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ImageManifestError(f"Dockerfile is not readable: {dockerfile}") from exc

    entries: list[dict[str, Any]] = []
    missing: list[str] = []
    generated_set = set(generated)
    for copy_entry in parse_copy_entries(text):
        for source, destination in copy_entry.pairs():
            path = repo_root / source
            if source in generated_set:
                # Recorded so start-up still checks it is present, but not
                # hashed: a build artifact differs from build to build.
                entries.append(
                    {
                        "source": source,
                        "destination": _image_path(destination, image_root),
                        "sha256": "",
                        "kind": "generated",
                    }
                )
                continue
            if not path.exists():
                missing.append(source)
                continue
            entries.append(
                {
                    "source": source,
                    "destination": _image_path(destination, image_root),
                    "sha256": _digest_path(path),
                    "kind": "directory" if path.is_dir() else "file",
                }
            )
    if missing:
        raise ImageManifestError(
            "Dockerfile copies sources that do not exist: " + ", ".join(sorted(missing))
        )
    return {
        "schema_version": MANIFEST_SCHEMA,
        "dockerfile": dockerfile.as_posix(),
        "revision": revision,
        "image_root": image_root.as_posix(),
        "entries": sorted(entries, key=lambda item: item["source"]),
    }


def _image_path(destination: str, image_root: Path) -> str:
    path = Path(destination)
    if path.is_absolute():
        return path.as_posix()
    return (image_root / path).as_posix()


def _digest_path(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_dir():
        for child in sorted(
            item
            for item in path.rglob("*")
            if item.is_file()
            and "__pycache__" not in item.parts
            and "node_modules" not in item.parts
            and item.suffix not in {".pyc", ".pyo"}
        ):
            digest.update(child.relative_to(path).as_posix().encode())
            digest.update(b"\0")
            digest.update(child.read_bytes())
            digest.update(b"\0")
    else:
        digest.update(path.read_bytes())
    return digest.hexdigest()


def verify_image_contents(
    manifest: Mapping[str, Any], *, root: Path | None = None
) -> tuple[str, ...]:
    """Report every manifest entry the running image does not actually have."""
    problems: list[str] = []
    entries = manifest.get("entries")
    if not isinstance(entries, list) or not entries:
        return ("runtime manifest lists no files",)
    base = Path(root) if root is not None else None
    for entry in entries:
        destination = str(entry.get("destination") or "")
        if not destination:
            problems.append("manifest entry has no destination")
            continue
        path = (
            base / Path(destination).relative_to("/")
            if base is not None
            else Path(destination)
        )
        if not path.exists():
            problems.append(f"missing from image: {destination}")
            continue
        expected = str(entry.get("sha256") or "")
        if expected and _digest_path(path) != expected:
            problems.append(
                f"stale copy in image: {destination} does not match the built revision"
            )
    return tuple(problems)


def verify_dockerfile_destinations(
    dockerfile_text: str, *, root: Path = Path("/"), image_root: Path = IMAGE_ROOT
) -> tuple[str, ...]:
    """Check the image actually holds every path its own Dockerfile copies.

    Used inside the image, where the sources are not available to re-hash: it
    answers "is anything the recipe promised simply not here?".
    """
    problems: list[str] = []
    for entry in parse_copy_entries(dockerfile_text):
        for destination in entry.destinations():
            absolute = Path(_image_path(destination, image_root))
            path = root / absolute.relative_to("/")
            if not path.exists():
                problems.append(f"missing from image: {absolute.as_posix()}")
    if not problems and not parse_copy_entries(dockerfile_text):
        return ("Dockerfile copies nothing; the manifest cannot be checked",)
    return tuple(problems)


def diff_manifests(
    built: Mapping[str, Any], current: Mapping[str, Any]
) -> tuple[str, ...]:
    """Report drift between the manifest a build recorded and one built now."""
    problems: list[str] = []
    built_entries = {
        str(entry.get("source")): entry for entry in built.get("entries") or []
    }
    current_entries = {
        str(entry.get("source")): entry for entry in current.get("entries") or []
    }
    for source in sorted(set(built_entries) - set(current_entries)):
        problems.append(f"no longer copied by the Dockerfile: {source}")
    for source in sorted(set(current_entries) - set(built_entries)):
        problems.append(f"copied by the Dockerfile but not in the built image: {source}")
    for source in sorted(set(built_entries) & set(current_entries)):
        expected = str(built_entries[source].get("sha256") or "")
        observed = str(current_entries[source].get("sha256") or "")
        if expected and observed and expected != observed:
            problems.append(f"changed since the image was built: {source}")
    return tuple(problems)


def verify_revision(manifest: Mapping[str, Any], observed: str | None) -> tuple[str, ...]:
    """Report a manifest built from a revision other than the image's own."""
    recorded = str(manifest.get("revision") or "")
    observed = (observed or "").strip()
    if not recorded or recorded == "unknown":
        return ("runtime manifest does not record the revision it was built from",)
    if not observed:
        return (f"image does not report a revision to compare with {recorded}",)
    if not (recorded.startswith(observed) or observed.startswith(recorded)):
        return (
            f"runtime manifest revision {recorded} does not match image revision {observed}",
        )
    return ()


def verify(
    manifest: Mapping[str, Any],
    *,
    root: Path | None = None,
    observed_revision: str | None = None,
) -> tuple[str, ...]:
    return verify_revision(manifest, observed_revision) + verify_image_contents(
        manifest, root=root
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--dockerfile", type=Path, default=DEFAULT_DOCKERFILE)
    parser.add_argument("--revision", default=os.environ.get(REVISION_ENV, "unknown"))
    parser.add_argument("--emit", type=Path, help="write the manifest to this path")
    parser.add_argument(
        "--verify",
        action="store_true",
        help="check the running image against the manifest built from the Dockerfile",
    )
    parser.add_argument("--image-root", type=Path, default=None)
    parser.add_argument("--manifest", type=Path, help="verify against this manifest file")
    parser.add_argument(
        "--verify-in-image",
        action="store_true",
        help="check that every Dockerfile COPY destination is present in this image",
    )
    args = parser.parse_args(argv)

    if args.verify_in_image:
        dockerfile = args.repo_root / args.dockerfile
        try:
            text = dockerfile.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"runtime manifest error: {exc}", file=sys.stderr)
            return 2
        problems = verify_dockerfile_destinations(
            text, root=args.image_root or Path("/")
        )
        for problem in problems:
            print(f"runtime manifest mismatch: {problem}", file=sys.stderr)
        if problems:
            return 3
        print("runtime manifest verified: every copied path is present")
        return 0

    if args.manifest is not None:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    else:
        try:
            manifest = build_manifest(
                args.repo_root, dockerfile=args.dockerfile, revision=args.revision
            )
        except ImageManifestError as exc:
            print(f"runtime manifest error: {exc}", file=sys.stderr)
            return 2

    if args.emit is not None:
        args.emit.parent.mkdir(parents=True, exist_ok=True)
        args.emit.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    if not args.verify:
        if args.emit is None:
            print(json.dumps(manifest, indent=2, sort_keys=True))
        return 0

    problems = verify(
        manifest,
        root=args.image_root,
        observed_revision=args.revision,
    )
    if problems:
        for problem in problems:
            print(f"runtime manifest mismatch: {problem}", file=sys.stderr)
        return 3
    print(f"runtime manifest verified: {len(manifest['entries'])} entries")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
