from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts import mirror_images


CONFIG = Path("environment/kubernetes/sock-shop/render-config.yaml")


class FakeRunner:
    def __init__(self, digests: dict[str, str]):
        self.digests = digests
        self.commands: list[list[str]] = []
        self.inputs: list[str | None] = []
        self.docker_configs: list[str] = []
        self.envs: list[dict[str, str]] = []

    def run(self, argv: list[str], *, env: dict[str, str], stdin: str | None = None) -> str:
        self.commands.append(argv)
        self.inputs.append(stdin)
        self.docker_configs.append(env["DOCKER_CONFIG"])
        self.envs.append(dict(env))
        if argv[:3] == ["crane", "auth", "login"]:
            return "logged in"
        if argv[:2] == ["crane", "copy"]:
            return "copied"
        if argv[:2] == ["crane", "digest"]:
            destination = argv[2]
            return self.digests[destination]
        raise AssertionError(f"unexpected command: {argv}")


def harbor_config() -> mirror_images.HarborConfig:
    return mirror_images.HarborConfig(
        registry="harbor.example:85",
        project="sock-shop",
        username="robot$benchmark",
        robot_credential="not-a-real-token",
    )


def test_load_image_pins_reads_all_sock_shop_pins():
    pins = mirror_images.load_image_pins(CONFIG)

    assert len(pins) == 14
    assert {pin.source_name for pin in pins} >= {"weaveworksdemos/front-end", "mongo", "rabbitmq"}
    assert all(pin.source_ref.endswith(pin.digest) for pin in pins)


def test_dry_run_returns_pinned_harbor_image_map_without_runner():
    pins = mirror_images.load_image_pins(CONFIG)
    image_map = mirror_images.mirror_images(pins, harbor_config(), execute=False)

    assert len(image_map) == 14
    assert image_map["weaveworksdemos/front-end"].startswith(
        "harbor.example:85/sock-shop/weaveworksdemos/front-end@sha256:"
    )
    assert all("@sha256:" in target for target in image_map.values())


def test_execute_uses_crane_copy_and_verifies_digest_without_printing_secret(monkeypatch, capsys):
    monkeypatch.setenv("HARBOR_ROBOT_TOKEN", "not-a-real-token")
    monkeypatch.setenv("HARBOR_ROBOT_USERNAME", "robot$benchmark")
    pins = mirror_images.load_image_pins(CONFIG)
    config = harbor_config()
    expected_digests = {
        mirror_images.tag_ref_for(pin, config): pin.digest
        for pin in pins
    }
    runner = FakeRunner(expected_digests)

    image_map = mirror_images.mirror_images(pins, config, execute=True, runner=runner)
    output = mirror_images.write_json(image_map)

    assert len([command for command in runner.commands if command[:2] == ["crane", "copy"]]) == 14
    assert len([command for command in runner.commands if command[:2] == ["crane", "digest"]]) == 14
    assert runner.commands[0][:4] == ["crane", "auth", "login", "harbor.example:85"]
    assert "--password-stdin" in runner.commands[0]
    assert "not-a-real-token" not in output
    assert "not-a-real-token" not in " ".join(" ".join(command) for command in runner.commands)
    assert all("HARBOR_ROBOT_TOKEN" not in env for env in runner.envs)
    assert all("HARBOR_ROBOT_USERNAME" not in env for env in runner.envs)
    assert len(set(runner.docker_configs)) == 1
    assert json.loads(output) == image_map
    assert capsys.readouterr().out == ""


def test_execute_rejects_digest_mismatch():
    pins = mirror_images.load_image_pins(CONFIG)
    config = harbor_config()
    expected_digests = {
        mirror_images.tag_ref_for(pin, config): pin.digest
        for pin in pins
    }
    first_destination = mirror_images.tag_ref_for(pins[0], config)
    expected_digests[first_destination] = "sha256:" + "0" * 64
    runner = FakeRunner(expected_digests)

    with pytest.raises(mirror_images.MirrorError, match="digest verification failed"):
        mirror_images.mirror_images(pins, config, execute=True, runner=runner)


def test_load_harbor_config_allows_dry_run_without_credentials(monkeypatch):
    monkeypatch.setenv("HARBOR_REGISTRY", "https://harbor.example:85/")
    monkeypatch.setenv("HARBOR_PROJECT_SOCK_SHOP", "/sock-shop/")
    monkeypatch.delenv("HARBOR_ROBOT_USERNAME", raising=False)
    monkeypatch.delenv("HARBOR_ROBOT_TOKEN", raising=False)

    config = mirror_images.load_harbor_config(require_credentials=False)

    assert config.registry == "harbor.example:85"
    assert config.project == "sock-shop"
    assert config.username is None
    assert config.robot_credential is None


@pytest.mark.parametrize(
    "registry",
    [
        "https://user:password@harbor.example:85",
        "https://harbor.example:85/project",
        "https://harbor.example:85?token=secret",
        "https://harbor.example:85#secret",
    ],
)
def test_harbor_registry_rejects_userinfo_paths_queries_and_fragments(monkeypatch, registry):
    monkeypatch.setenv("HARBOR_REGISTRY", registry)
    monkeypatch.setenv("HARBOR_PROJECT_SOCK_SHOP", "sock-shop")

    with pytest.raises(mirror_images.MirrorError):
        mirror_images.load_harbor_config(require_credentials=False)


def test_cli_defaults_to_dry_run(monkeypatch):
    monkeypatch.setenv("HARBOR_REGISTRY", "harbor.example:85")
    monkeypatch.setenv("HARBOR_PROJECT_SOCK_SHOP", "sock-shop")

    result = subprocess.run(
        ["python3", "scripts/mirror_images.py", "--config", str(CONFIG)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    image_map = json.loads(result.stdout)
    assert len(image_map) == 14
    assert result.stderr == ""


def test_cli_execute_requires_credentials(monkeypatch):
    monkeypatch.setenv("HARBOR_REGISTRY", "harbor.example:85")
    monkeypatch.setenv("HARBOR_PROJECT_SOCK_SHOP", "sock-shop")
    monkeypatch.delenv("HARBOR_ROBOT_USERNAME", raising=False)
    monkeypatch.delenv("HARBOR_ROBOT_TOKEN", raising=False)

    result = subprocess.run(
        ["python3", "scripts/mirror_images.py", "--execute", "--config", str(CONFIG)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert "HARBOR_ROBOT_USERNAME" in result.stderr
    assert "HARBOR_ROBOT_TOKEN" in result.stderr
