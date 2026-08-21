from __future__ import annotations

import subprocess
import json
from pathlib import Path

import pytest
import yaml

from scripts import render_sock_shop


CONFIG = Path("environment/kubernetes/sock-shop/render-config.yaml")


def make_upstream_manifest(config: render_sock_shop.RenderConfig) -> str:
    containers = [
        {"name": source.replace("/", "-"), "image": source}
        for source in sorted(config.image_pins)
    ]
    docs = [
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": "sock-shop"},
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "front-end"},
            "spec": {"ports": [{"port": 80}], "selector": {"name": "front-end"}},
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "front-end"},
            "spec": {
                "template": {
                    "spec": {
                        "nodeSelector": {"beta.kubernetes.io/os": "linux"},
                        "containers": containers,
                    },
                },
            },
        },
    ]
    return yaml.safe_dump_all(docs, explicit_start=True, sort_keys=False)


def write_manifest(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "complete-demo.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def container_images(docs: list[dict]) -> list[str]:
    images: list[str] = []
    for doc in docs:
        spec = doc.get("spec", {}).get("template", {}).get("spec", {})
        for container in spec.get("containers", []) or []:
            images.append(container["image"])
    return images


def test_render_from_local_input_pins_images_namespace_and_node_selector(tmp_path):
    config = render_sock_shop.load_config(CONFIG)
    input_path = write_manifest(tmp_path, make_upstream_manifest(config))

    rendered = render_sock_shop.render_manifest(CONFIG, input_path=input_path)
    docs = [doc for doc in yaml.safe_load_all(rendered) if doc]

    assert docs[0]["kind"] == "Namespace"
    assert docs[0]["metadata"]["name"] == "sock-shop"
    assert docs[1]["metadata"]["namespace"] == "sock-shop"
    assert docs[2]["metadata"]["namespace"] == "sock-shop"
    assert docs[2]["spec"]["template"]["spec"]["nodeSelector"] == {"kubernetes.io/os": "linux"}
    assert set(container_images(docs)) == set(config.image_pins.values())
    assert all("@sha256:" in image for image in container_images(docs))


def test_rejects_unknown_container_image(tmp_path):
    config = render_sock_shop.load_config(CONFIG)
    manifest = make_upstream_manifest(config).replace("weaveworksdemos/carts", "example/unknown", 1)
    input_path = write_manifest(tmp_path, manifest)

    with pytest.raises(render_sock_shop.RenderError, match="no digest pin configured"):
        render_sock_shop.render_manifest(CONFIG, input_path=input_path)


def test_rejects_unsafe_object_kind(tmp_path):
    config = render_sock_shop.load_config(CONFIG)
    manifest = make_upstream_manifest(config) + "---\napiVersion: v1\nkind: Secret\nmetadata:\n  name: hidden\n"
    input_path = write_manifest(tmp_path, manifest)

    with pytest.raises(render_sock_shop.RenderError, match="unsafe object type"):
        render_sock_shop.render_manifest(CONFIG, input_path=input_path)


def test_verify_input_sha_rejects_fixture_manifest(tmp_path):
    config = render_sock_shop.load_config(CONFIG)
    input_path = write_manifest(tmp_path, make_upstream_manifest(config))

    with pytest.raises(render_sock_shop.RenderError, match="SHA-256 mismatch"):
        render_sock_shop.render_manifest(CONFIG, input_path=input_path, verify_input_sha=True)


def test_cli_writes_rendered_yaml_to_stdout(tmp_path):
    config = render_sock_shop.load_config(CONFIG)
    input_path = write_manifest(tmp_path, make_upstream_manifest(config))

    result = subprocess.run(
        [
            "python3",
            "scripts/render_sock_shop.py",
            "--config",
            str(CONFIG),
            "--input",
            str(input_path),
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    assert [doc["kind"] for doc in docs] == ["Namespace", "Service", "Deployment"]
    assert result.stderr == ""


def test_render_with_image_map_uses_harbor_digest_targets(tmp_path):
    config = render_sock_shop.load_config(CONFIG)
    input_path = write_manifest(tmp_path, make_upstream_manifest(config))
    image_map = {
        source: f"harbor.example:85/sock-shop/{source}@sha256:{index:064x}"
        for index, source in enumerate(sorted(config.image_pins), start=1)
    }
    image_map_path = tmp_path / "image-map.json"
    image_map_path.write_text(json.dumps(image_map), encoding="utf-8")

    rendered = render_sock_shop.render_manifest(CONFIG, input_path=input_path, image_map_path=image_map_path)
    docs = [doc for doc in yaml.safe_load_all(rendered) if doc]

    assert set(container_images(docs)) == set(image_map.values())
    assert all(image.startswith("harbor.example:85/sock-shop/") for image in container_images(docs))


def test_render_rejects_incomplete_image_map(tmp_path):
    config = render_sock_shop.load_config(CONFIG)
    input_path = write_manifest(tmp_path, make_upstream_manifest(config))
    image_map = {
        source: f"harbor.example:85/sock-shop/{source}@sha256:{index:064x}"
        for index, source in enumerate(sorted(config.image_pins), start=1)
    }
    image_map.pop(next(iter(image_map)))
    image_map_path = tmp_path / "image-map.json"
    image_map_path.write_text(json.dumps(image_map), encoding="utf-8")

    with pytest.raises(render_sock_shop.RenderError, match="image map must cover exactly"):
        render_sock_shop.render_manifest(CONFIG, input_path=input_path, image_map_path=image_map_path)
