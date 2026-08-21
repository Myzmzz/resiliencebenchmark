#!/usr/bin/env python3
"""Mirror pinned Sock Shop images into Harbor.

The command is deliberately dry-run by default. A real copy requires
``--execute`` and runtime Harbor credentials from the environment.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

import yaml


DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
IMAGE_BY_DIGEST_RE = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
TAG_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


class MirrorError(Exception):
    """Raised when image mirroring cannot be planned or completed safely."""


@dataclass(frozen=True)
class ImagePin:
    source_name: str
    source_ref: str
    digest: str


@dataclass(frozen=True)
class HarborConfig:
    registry: str
    project: str
    username: str | None
    robot_credential: str | None
    insecure: bool = False


class CommandRunner(Protocol):
    def run(
        self,
        argv: list[str],
        *,
        env: dict[str, str],
        stdin: str | None = None,
    ) -> str:
        """Run a command and return stdout."""


class SubprocessRunner:
    def run(
        self,
        argv: list[str],
        *,
        env: dict[str, str],
        stdin: str | None = None,
    ) -> str:
        completed = subprocess.run(
            argv,
            input=stdin,
            check=False,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if completed.returncode != 0:
            raise MirrorError(
                f"command failed: {redact_command(argv)}: {completed.stderr.strip() or completed.stdout.strip()}"
            )
        return completed.stdout.strip()


def redact_command(argv: list[str]) -> str:
    return " ".join("<redacted>" if part.startswith("sha256:") and len(part) > 32 else part for part in argv)


def load_image_pins(config_path: Path) -> list[ImagePin]:
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, dict):
        raise MirrorError("render config top-level document must be a mapping")
    spec = raw.get("spec")
    if not isinstance(spec, dict):
        raise MirrorError("render config missing spec")
    image_pins = spec.get("imagePins")
    if not isinstance(image_pins, list) or len(image_pins) != 14:
        raise MirrorError("spec.imagePins must contain the 14 audited Sock Shop image pins")

    pins: list[ImagePin] = []
    seen: set[str] = set()
    for item in image_pins:
        if not isinstance(item, dict) or not isinstance(item.get("source"), str) or not isinstance(item.get("target"), str):
            raise MirrorError("each imagePins item must contain source and target")
        source_name = item["source"]
        source_ref = item["target"]
        if source_name in seen:
            raise MirrorError(f"duplicate image pin for {source_name}")
        if not IMAGE_BY_DIGEST_RE.match(source_ref):
            raise MirrorError(f"target image for {source_name} must be pinned by sha256 digest")
        digest = source_ref.rsplit("@", 1)[1]
        if not DIGEST_RE.match(digest):
            raise MirrorError(f"target image for {source_name} has an invalid digest")
        pins.append(ImagePin(source_name=source_name, source_ref=source_ref, digest=digest))
        seen.add(source_name)
    return pins


def load_harbor_config(*, require_credentials: bool) -> HarborConfig:
    registry = normalize_registry(os.environ.get("HARBOR_REGISTRY", ""))
    project = normalize_project(os.environ.get("HARBOR_PROJECT_SOCK_SHOP", ""))
    missing = []
    if not registry:
        missing.append("HARBOR_REGISTRY")
    if not project:
        missing.append("HARBOR_PROJECT_SOCK_SHOP")

    username = os.environ.get("HARBOR_ROBOT_USERNAME")
    robot_credential = os.environ.get("HARBOR_ROBOT_TOKEN")
    insecure_raw = os.environ.get("HARBOR_INSECURE", "false").strip().lower()
    if insecure_raw not in {"true", "false"}:
        raise MirrorError("HARBOR_INSECURE must be exactly true or false")
    if require_credentials:
        if not username:
            missing.append("HARBOR_ROBOT_USERNAME")
        if not robot_credential:
            missing.append("HARBOR_ROBOT_TOKEN")
    if missing:
        raise MirrorError(f"missing required environment variables: {', '.join(missing)}")
    return HarborConfig(
        registry=registry,
        project=project,
        username=username,
        robot_credential=robot_credential,
        insecure=insecure_raw == "true",
    )


def normalize_registry(value: str) -> str:
    raw = value.strip()
    if not raw:
        return ""
    parsed = urlparse(raw if "://" in raw else f"//{raw}")
    if parsed.scheme and parsed.scheme not in {"http", "https"}:
        raise MirrorError("HARBOR_REGISTRY must use http or https when a scheme is supplied")
    if not parsed.hostname or parsed.username or parsed.password:
        raise MirrorError("HARBOR_REGISTRY must be a hostname with optional port and no userinfo")
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise MirrorError("HARBOR_REGISTRY must not contain a path, parameters, query, or fragment")
    if not re.fullmatch(r"[A-Za-z0-9.-]+", parsed.hostname):
        raise MirrorError("HARBOR_REGISTRY hostname contains unsupported characters")
    try:
        port = parsed.port
    except ValueError as exc:
        raise MirrorError("HARBOR_REGISTRY port is invalid") from exc
    return f"{parsed.hostname}:{port}" if port is not None else parsed.hostname


def normalize_project(value: str) -> str:
    project = value.strip().strip("/")
    if project and not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", project):
        raise MirrorError("HARBOR_PROJECT_SOCK_SHOP must be one lowercase Harbor project name")
    return project


def destination_tag(source_name: str, digest: str) -> str:
    short_digest = digest.split(":", 1)[1][:12]
    tag = source_name.rsplit(":", 1)[1] if ":" in source_name.rsplit("/", 1)[-1] else "pinned"
    return TAG_SAFE_RE.sub("-", f"{tag}-{short_digest}").strip(".-") or short_digest


def destination_repo(config: HarborConfig, source_name: str) -> str:
    clean_source = source_name.strip("/")
    if not clean_source or clean_source.startswith(".") or ".." in clean_source.split("/"):
        raise MirrorError(f"unsafe source image name: {source_name}")
    return f"{config.registry}/{config.project}/{clean_source}"


def plan_image_map(pins: list[ImagePin], config: HarborConfig) -> dict[str, str]:
    image_map: dict[str, str] = {}
    for pin in pins:
        image_map[pin.source_name] = f"{tag_ref_for(pin, config)}@{pin.digest}"
    return image_map


def tag_ref_for(pin: ImagePin, config: HarborConfig) -> str:
    return f"{destination_repo(config, pin.source_name)}:{destination_tag(pin.source_name, pin.digest)}"


def mirror_images(
    pins: list[ImagePin],
    config: HarborConfig,
    *,
    execute: bool,
    runner: CommandRunner | None = None,
) -> dict[str, str]:
    planned = plan_image_map(pins, config)
    if not execute:
        return planned

    if not config.username or not config.robot_credential:
        raise MirrorError("real mirroring requires Harbor robot credentials")
    if shutil.which("crane") is None and runner is None:
        raise MirrorError("crane is required for real mirroring but was not found in PATH")

    runner = runner or SubprocessRunner()
    with tempfile.TemporaryDirectory(prefix="resbench-crane-") as docker_config:
        env = os.environ.copy()
        env.pop("HARBOR_ROBOT_USERNAME", None)
        env.pop("HARBOR_ROBOT_TOKEN", None)
        env["DOCKER_CONFIG"] = docker_config
        runner.run(
            [
                "crane",
                "auth",
                "login",
                config.registry,
                "-u",
                config.username,
                "--password-stdin",
                *(["--insecure"] if config.insecure else []),
            ],
            env=env,
            stdin=config.robot_credential,
        )
        verified: dict[str, str] = {}
        for pin in pins:
            tagged_destination = tag_ref_for(pin, config)
            runner.run(
                [
                    "crane",
                    "copy",
                    "--platform",
                    "linux/amd64",
                    pin.source_ref,
                    tagged_destination,
                    *(["--insecure"] if config.insecure else []),
                ],
                env=env,
            )
            target_digest = runner.run(
                ["crane", "digest", tagged_destination, *(["--insecure"] if config.insecure else [])],
                env=env,
            ).strip()
            if target_digest != pin.digest:
                raise MirrorError(
                    f"digest verification failed for {pin.source_name}: expected {pin.digest}, got {target_digest}"
                )
            verified[pin.source_name] = f"{tagged_destination}@{target_digest}"
        return verified


def write_json(image_map: dict[str, str]) -> str:
    return json.dumps(image_map, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Mirror pinned Sock Shop images into Harbor and emit an image map JSON.")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("environment/kubernetes/sock-shop/render-config.yaml"),
        help="Sock Shop render config. Defaults to the repository config.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Plan the mirror and print the expected image map. This is the default.",
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        help="Perform crane auth login/copy/digest against Harbor. Requires Harbor environment variables.",
    )
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    execute = bool(args.execute)
    try:
        pins = load_image_pins(args.config)
        config = load_harbor_config(require_credentials=execute)
        image_map = mirror_images(pins, config, execute=execute)
        sys.stdout.write(write_json(image_map))
    except MirrorError as exc:
        print(f"mirror_images: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
