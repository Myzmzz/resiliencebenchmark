"""O18: the image's file list comes from the Dockerfile, and is checked.

The controller image used to be described twice — the Dockerfile's COPY lines
and a hand-written list in scripts/build_stage2_image.py. On 2026-09-11 the two
drifted: deploy_application.py was added to the Dockerfile but not to the digest
list, so the release kept the base image's older copy and the reset preflight
failed for the wrong reason.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stage2_service.image_manifest import (
    DEFAULT_DOCKERFILE,
    ImageManifestError,
    build_manifest,
    copied_sources,
    main,
    parse_copy_entries,
    verify,
    verify_image_contents,
    verify_revision,
)


REPO_ROOT = Path(__file__).resolve().parents[1]

DOCKERFILE = """\
ARG STAGE2_RUNTIME_BASE
FROM ${STAGE2_RUNTIME_BASE}
USER root
COPY pyproject.toml uv.lock /app/
# a comment that mentions COPY but copies nothing
COPY --chown=10001:10001 stage2_service /app/stage2_service
COPY --chown=10001:10001 scripts/deploy_application.py /app/scripts/deploy_application.py
COPY --chown=10001:10001 --chmod=0755 deploy/stage2/codex-eval /app/runtime/bin/codex-eval
COPY --from=builder /out/thing /app/thing
"""


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for relative, content in (
        ("pyproject.toml", "[project]\n"),
        ("uv.lock", "lock\n"),
        ("stage2_service/__init__.py", "x = 1\n"),
        ("scripts/deploy_application.py", "print('deploy')\n"),
        ("deploy/stage2/codex-eval", "#!/bin/sh\n"),
        (DEFAULT_DOCKERFILE.as_posix(), DOCKERFILE),
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


# --- parsing --------------------------------------------------------------


def test_every_local_copy_is_read_and_stage_copies_are_not():
    entries = parse_copy_entries(DOCKERFILE)
    sources = copied_sources(DOCKERFILE)

    assert sources == (
        "deploy/stage2/codex-eval",
        "pyproject.toml",
        "scripts/deploy_application.py",
        "stage2_service",
        "uv.lock",
    )
    assert "/out/thing" not in [source for entry in entries for source in entry.sources]


def test_a_multi_source_copy_expands_to_one_destination_each():
    entry = next(entry for entry in parse_copy_entries(DOCKERFILE) if len(entry.sources) == 2)

    assert entry.pairs() == (
        ("pyproject.toml", "/app/pyproject.toml"),
        ("uv.lock", "/app/uv.lock"),
    )


def test_the_real_dockerfile_parses_and_lists_the_scripts_it_ships():
    sources = copied_sources(
        (REPO_ROOT / DEFAULT_DOCKERFILE).read_text(encoding="utf-8")
    )

    assert "scripts/deploy_application.py" in sources
    assert "scripts/run_harness_trial.py" in sources
    assert "stage2_service" in sources


def test_the_build_digest_covers_every_copied_script():
    """A COPY line added to the Dockerfile is in the build inputs by construction."""
    from scripts.build_stage2_image import source_digest

    before = source_digest()
    target = REPO_ROOT / "scripts/deploy_application.py"
    original = target.read_bytes()
    try:
        target.write_bytes(original + b"\n# touched by a test\n")
        assert source_digest() != before
    finally:
        target.write_bytes(original)
    assert source_digest() == before


# --- build ----------------------------------------------------------------


def test_building_a_manifest_hashes_each_entry(tmp_path: Path):
    manifest = build_manifest(_tree(tmp_path), revision="abc1234", generated=())

    assert manifest["revision"] == "abc1234"
    by_source = {entry["source"]: entry for entry in manifest["entries"]}
    assert by_source["scripts/deploy_application.py"]["destination"] == (
        "/app/scripts/deploy_application.py"
    )
    assert len(by_source["scripts/deploy_application.py"]["sha256"]) == 64
    assert by_source["stage2_service"]["kind"] == "directory"


def test_a_copy_of_a_file_that_does_not_exist_fails_the_build(tmp_path: Path):
    """缺脚本: the Dockerfile names a script the tree does not have."""
    root = _tree(tmp_path)
    (root / "scripts/deploy_application.py").unlink()

    with pytest.raises(ImageManifestError, match="scripts/deploy_application.py"):
        build_manifest(root, revision="abc1234", generated=())


# --- verification ---------------------------------------------------------


def _image(tmp_path: Path, root: Path) -> Path:
    """Lay out an /app-shaped tree from the repository sources."""
    image = tmp_path / "image"
    manifest = build_manifest(root, revision="abc1234", generated=())
    for entry in manifest["entries"]:
        target = image / Path(entry["destination"]).relative_to("/")
        source = root / entry["source"]
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            for child in source.rglob("*"):
                if child.is_file():
                    copy = target / child.relative_to(source)
                    copy.parent.mkdir(parents=True, exist_ok=True)
                    copy.write_bytes(child.read_bytes())
        else:
            target.write_bytes(source.read_bytes())
    return image


def test_a_complete_image_verifies(tmp_path: Path):
    root = _tree(tmp_path)
    manifest = build_manifest(root, revision="abc1234", generated=())

    assert verify(manifest, root=_image(tmp_path, root), observed_revision="abc1234") == ()


def test_a_missing_file_is_reported_by_name(tmp_path: Path):
    """缺脚本 at run time."""
    root = _tree(tmp_path)
    manifest = build_manifest(root, revision="abc1234", generated=())
    image = _image(tmp_path, root)
    (image / "app/scripts/deploy_application.py").unlink()

    problems = verify_image_contents(manifest, root=image)

    assert any("missing from image" in item and "deploy_application.py" in item for item in problems)


def test_a_stale_copy_is_reported_even_though_the_file_exists(tmp_path: Path):
    """旧脚本: the base image's older copy survived the overlay."""
    root = _tree(tmp_path)
    manifest = build_manifest(root, revision="abc1234", generated=())
    image = _image(tmp_path, root)
    (image / "app/scripts/deploy_application.py").write_text(
        "print('an older copy without --server-dry-run')\n", encoding="utf-8"
    )

    problems = verify_image_contents(manifest, root=image)

    assert any("stale copy in image" in item for item in problems)


@pytest.mark.parametrize(
    ("recorded", "observed"),
    [("abc1234", "def5678"), ("abc1234", ""), ("unknown", "abc1234")],
)
def test_a_revision_that_does_not_match_is_reported(recorded: str, observed: str):
    """版本不一致."""
    problems = verify_revision({"revision": recorded, "entries": [1]}, observed)

    assert isinstance(problems, tuple) and len(problems) == 1
    # verify() concatenates the two checks, so a bare string here would raise.
    assert verify({"revision": recorded, "entries": []}, observed_revision=observed)


def test_a_matching_short_revision_is_accepted():
    assert verify_revision({"revision": "abc1234def", "entries": [1]}, "abc1234") == ()


def test_an_empty_manifest_is_not_treated_as_a_pass():
    assert verify_image_contents({"entries": []}) == ("runtime manifest lists no files",)


# --- CLI ------------------------------------------------------------------


def test_the_cli_emits_a_manifest_and_verifies_an_image(tmp_path: Path, capsys):
    root = _tree(tmp_path)
    out = tmp_path / "manifest.json"

    assert main(["--repo-root", str(root), "--revision", "abc1234", "--emit", str(out)]) == 0
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["revision"] == "abc1234"

    image = _image(tmp_path, root)
    assert (
        main(
            [
                "--manifest",
                str(out),
                "--verify",
                "--revision",
                "abc1234",
                "--image-root",
                str(image),
            ]
        )
        == 0
    )

    (image / "app/scripts/deploy_application.py").unlink()
    assert (
        main(
            [
                "--manifest",
                str(out),
                "--verify",
                "--revision",
                "abc1234",
                "--image-root",
                str(image),
            ]
        )
        == 3
    )
    assert "runtime manifest mismatch" in capsys.readouterr().err


# --- the image checks itself ----------------------------------------------


def test_the_overlay_ships_its_own_dockerfile_and_runs_the_self_check():
    """Without both lines the image cannot tell whether it is complete."""
    text = (REPO_ROOT / DEFAULT_DOCKERFILE).read_text(encoding="utf-8")

    assert (
        "COPY --chown=10001:10001 deploy/stage2/Dockerfile.runtime-overlay "
        "/app/deploy/stage2/Dockerfile.runtime-overlay" in text
    )
    assert "stage2_service.image_manifest --verify-in-image" in text
    assert "ENV RESBENCH_SOURCE_HEAD=${SOURCE_HEAD}" in text
    assert DEFAULT_DOCKERFILE.as_posix() in copied_sources(text)


def test_the_in_image_check_names_what_is_missing(tmp_path: Path):
    from stage2_service.image_manifest import verify_dockerfile_destinations

    root = _tree(tmp_path)
    image = _image(tmp_path, root)

    assert verify_dockerfile_destinations(DOCKERFILE, root=image) == ()

    (image / "app/scripts/deploy_application.py").unlink()
    problems = verify_dockerfile_destinations(DOCKERFILE, root=image)

    assert problems == ("missing from image: /app/scripts/deploy_application.py",)


def test_drift_between_the_built_manifest_and_the_tree_is_reported(tmp_path: Path):
    from stage2_service.image_manifest import diff_manifests

    root = _tree(tmp_path)
    built = build_manifest(root, revision="abc1234", generated=())

    assert diff_manifests(built, build_manifest(root, revision="abc1234", generated=())) == ()

    (root / "scripts/deploy_application.py").write_text("print('newer')\n", encoding="utf-8")
    problems = diff_manifests(built, build_manifest(root, revision="def5678", generated=()))

    assert problems == ("changed since the image was built: scripts/deploy_application.py",)
