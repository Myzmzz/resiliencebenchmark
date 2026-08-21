#!/usr/bin/env python3
"""Render the pinned Sock Shop Kubernetes manifest.

The renderer deliberately does not call kubectl or kustomize. It fetches the
archived upstream manifest, verifies the configured SHA-256, normalizes only
the fields required for the benchmark baseline, and writes YAML to stdout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml


EXPECTED_UPSTREAM_URL = (
    "https://raw.githubusercontent.com/microservices-demo/microservices-demo/"
    "9dff06fae4981921caec6a62393a6ebfce4b3e3f/deploy/kubernetes/complete-demo.yaml"
)
EXPECTED_UPSTREAM_SHA256 = "02d70d2c7b576ea8b18fc436e18b9158d5b662e50180998b82127d8050813771"
EXPECTED_NAMESPACE = "sock-shop"
PINNED_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:/_-]+@sha256:[0-9a-f]{64}$")


class RenderError(Exception):
    """Raised when the upstream manifest cannot be safely rendered."""


@dataclass(frozen=True)
class RenderConfig:
    namespace: str
    source_url: str
    source_sha256: str
    allowed_objects: frozenset[tuple[str, str]]
    image_pins: dict[str, str]


def load_config(path: Path) -> RenderConfig:
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, dict):
        raise RenderError("render config top-level document must be a mapping")
    spec = raw.get("spec")
    if not isinstance(spec, dict):
        raise RenderError("render config missing spec")

    namespace = spec.get("namespace")
    source = spec.get("source")
    allowed_objects = spec.get("allowedObjects")
    image_pins = spec.get("imagePins")
    if namespace != EXPECTED_NAMESPACE:
        raise RenderError(f"namespace must be {EXPECTED_NAMESPACE}")
    if not isinstance(source, dict):
        raise RenderError("spec.source must be a mapping")
    if source.get("url") != EXPECTED_UPSTREAM_URL:
        raise RenderError("source.url must be the exact pinned raw GitHub URL")
    if source.get("sha256") != EXPECTED_UPSTREAM_SHA256:
        raise RenderError("source.sha256 does not match the audited upstream digest")
    if not isinstance(allowed_objects, list) or not allowed_objects:
        raise RenderError("spec.allowedObjects must be a non-empty list")
    if not isinstance(image_pins, list) or len(image_pins) != 14:
        raise RenderError("spec.imagePins must contain the 14 audited Sock Shop image pins")

    allowed: set[tuple[str, str]] = set()
    for item in allowed_objects:
        if not isinstance(item, dict) or not isinstance(item.get("apiVersion"), str) or not isinstance(item.get("kind"), str):
            raise RenderError("each allowedObjects item must contain apiVersion and kind")
        allowed.add((item["apiVersion"], item["kind"]))

    pins: dict[str, str] = {}
    for item in image_pins:
        if not isinstance(item, dict) or not isinstance(item.get("source"), str) or not isinstance(item.get("target"), str):
            raise RenderError("each imagePins item must contain source and target")
        source_image = strip_image_reference(item["source"])
        target_image = item["target"]
        if source_image in pins:
            raise RenderError(f"duplicate image pin for {source_image}")
        if not PINNED_IMAGE_RE.match(target_image):
            raise RenderError(f"target image for {source_image} must be pinned by sha256 digest")
        pins[source_image] = target_image

    return RenderConfig(
        namespace=namespace,
        source_url=source["url"],
        source_sha256=source["sha256"],
        allowed_objects=frozenset(allowed),
        image_pins=pins,
    )


def load_image_map(path: Path, config: RenderConfig) -> dict[str, str]:
    with path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise RenderError("image map must be a JSON object mapping source image names to pinned targets")
    image_map: dict[str, str] = {}
    for source_image, target_image in raw.items():
        if not isinstance(source_image, str) or not isinstance(target_image, str):
            raise RenderError("image map keys and values must be strings")
        if not PINNED_IMAGE_RE.match(target_image):
            raise RenderError(f"image map target for {source_image} must be pinned by sha256 digest")
        image_map[source_image] = target_image

    expected = set(config.image_pins)
    actual = set(image_map)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if extra:
            details.append(f"extra: {', '.join(extra)}")
        raise RenderError(f"image map must cover exactly the configured image pins ({'; '.join(details)})")
    return image_map


def apply_image_map(config: RenderConfig, image_map: dict[str, str]) -> RenderConfig:
    return RenderConfig(
        namespace=config.namespace,
        source_url=config.source_url,
        source_sha256=config.source_sha256,
        allowed_objects=config.allowed_objects,
        image_pins=dict(image_map),
    )


def strip_image_reference(image: str) -> str:
    image = image.split("@", 1)[0]
    slash = image.rfind("/")
    colon = image.rfind(":")
    if colon > slash:
        return image[:colon]
    return image


def read_source(config: RenderConfig, input_path: Path | None = None) -> bytes:
    if input_path is not None:
        return input_path.read_bytes()
    with urllib.request.urlopen(config.source_url, timeout=30) as response:  # noqa: S310 - URL is exact and pinned.
        return response.read()


def verify_sha256(payload: bytes, expected: str, description: str) -> None:
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected:
        raise RenderError(f"{description} SHA-256 mismatch: expected {expected}, got {actual}")


def parse_documents(payload: bytes) -> list[dict[str, Any]]:
    docs = [doc for doc in yaml.safe_load_all(payload.decode("utf-8")) if doc is not None]
    if not docs:
        raise RenderError("source manifest did not contain any YAML documents")
    for index, doc in enumerate(docs, start=1):
        if not isinstance(doc, dict):
            raise RenderError(f"YAML document {index} must be a mapping")
    return docs


def pod_specs(doc: dict[str, Any]) -> Iterable[dict[str, Any]]:
    if doc.get("kind") != "Deployment":
        return []
    template = doc.get("spec", {}).get("template", {})
    spec = template.get("spec", {})
    if not isinstance(spec, dict):
        raise RenderError(f"Deployment {object_name(doc)} has invalid pod spec")
    return [spec]


def object_name(doc: dict[str, Any]) -> str:
    metadata = doc.get("metadata", {})
    if isinstance(metadata, dict) and isinstance(metadata.get("name"), str):
        return metadata["name"]
    return "<unnamed>"


def normalize_documents(docs: list[dict[str, Any]], config: RenderConfig) -> list[dict[str, Any]]:
    used_pins: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for doc in docs:
        api_version = doc.get("apiVersion")
        kind = doc.get("kind")
        if (api_version, kind) not in config.allowed_objects:
            raise RenderError(f"unsafe object type {api_version}/{kind} at {object_name(doc)}")

        metadata = doc.setdefault("metadata", {})
        if not isinstance(metadata, dict):
            raise RenderError(f"{kind} {object_name(doc)} metadata must be a mapping")
        if kind == "Namespace":
            metadata["name"] = config.namespace
            metadata.pop("namespace", None)
        else:
            metadata["namespace"] = config.namespace

        for spec in pod_specs(doc):
            migrate_node_selector(spec)
            used_pins.update(pin_container_images(spec, config))
        normalized.append(doc)

    unused = sorted(set(config.image_pins) - used_pins)
    if unused:
        raise RenderError(f"configured image pins were not used by the manifest: {', '.join(unused)}")
    validate_rendered_documents(normalized, config)
    return normalized


def migrate_node_selector(pod_spec: dict[str, Any]) -> None:
    selector = pod_spec.get("nodeSelector")
    if selector is None:
        return
    if not isinstance(selector, dict):
        raise RenderError("nodeSelector must be a mapping")
    old_value = selector.pop("beta.kubernetes.io/os", None)
    if old_value is not None and "kubernetes.io/os" not in selector:
        selector["kubernetes.io/os"] = old_value
    if not selector:
        pod_spec.pop("nodeSelector", None)


def pin_container_images(pod_spec: dict[str, Any], config: RenderConfig) -> set[str]:
    used: set[str] = set()
    for field in ("initContainers", "containers"):
        containers = pod_spec.get(field, [])
        if containers is None:
            continue
        if not isinstance(containers, list):
            raise RenderError(f"{field} must be a list")
        for container in containers:
            if not isinstance(container, dict) or not isinstance(container.get("image"), str):
                raise RenderError(f"{field} entries must contain an image")
            source_image = strip_image_reference(container["image"])
            target = config.image_pins.get(source_image)
            if target is None:
                raise RenderError(f"no digest pin configured for image {container['image']}")
            container["image"] = target
            used.add(source_image)
    return used


def validate_rendered_documents(docs: list[dict[str, Any]], config: RenderConfig) -> None:
    for doc in docs:
        kind = doc.get("kind")
        metadata = doc.get("metadata", {})
        if not isinstance(metadata, dict):
            raise RenderError(f"{kind} {object_name(doc)} metadata must be a mapping")
        if kind == "Namespace":
            if metadata.get("name") != config.namespace:
                raise RenderError("Namespace object must be named sock-shop")
        elif metadata.get("namespace") != config.namespace:
            raise RenderError(f"{kind} {object_name(doc)} must be in namespace {config.namespace}")

        for spec in pod_specs(doc):
            selector = spec.get("nodeSelector", {})
            if isinstance(selector, dict) and "beta.kubernetes.io/os" in selector:
                raise RenderError(f"Deployment {object_name(doc)} still uses beta.kubernetes.io/os")
            for field in ("initContainers", "containers"):
                for container in spec.get(field, []) or []:
                    image = container.get("image")
                    if not isinstance(image, str) or "@sha256:" not in image:
                        raise RenderError(f"Deployment {object_name(doc)} contains an unpinned image")


def render_manifest(
    config_path: Path,
    input_path: Path | None = None,
    verify_input_sha: bool = False,
    image_map_path: Path | None = None,
) -> str:
    config = load_config(config_path)
    if image_map_path is not None:
        config = apply_image_map(config, load_image_map(image_map_path, config))
    payload = read_source(config, input_path)
    if input_path is None or verify_input_sha:
        verify_sha256(payload, config.source_sha256, str(input_path or config.source_url))
    docs = normalize_documents(parse_documents(payload), config)
    return yaml.safe_dump_all(docs, explicit_start=True, sort_keys=False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render the pinned Sock Shop Kubernetes manifest to stdout.")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("environment/kubernetes/sock-shop/render-config.yaml"),
        help="Sock Shop render config. Defaults to the repository config.",
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="Optional local upstream manifest for offline tests. The network source is used when omitted.",
    )
    parser.add_argument(
        "--verify-input-sha",
        action="store_true",
        help="Also require --input to match the audited upstream SHA-256.",
    )
    parser.add_argument(
        "--image-map",
        type=Path,
        help="Optional JSON mapping from configured source image names to Harbor repo@sha256 targets.",
    )
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        sys.stdout.write(render_manifest(args.config, args.input, args.verify_input_sha, args.image_map))
    except RenderError as exc:
        print(f"render_sock_shop: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
