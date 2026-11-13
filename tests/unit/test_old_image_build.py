"""The legacy branch must build its own pinned images without replacing latest."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from src.config import REPO_ROOT


def test_build_script_defaults_to_versioned_tags(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "docker-arguments"
    docker = fake_bin / "docker"
    docker.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >> "$DOCKER_CALLS"\n')
    docker.chmod(0o755)
    env = os.environ.copy()
    env.pop("NO_LATEST", None)
    env.update(PATH=f"{fake_bin}:{env['PATH']}", DOCKER_CALLS=str(calls))

    subprocess.run([str(REPO_ROOT / "scripts/build-images.sh"), "all"],
                   env=env, check=True, capture_output=True, text=True)

    args = calls.read_text().splitlines()
    version = (REPO_ROOT / "images/ubuntu/app/VERSION").read_text().strip()
    fs_version = (REPO_ROOT / "images/fs/VERSION").read_text().strip()
    for image in ("sandbox-code", "sandbox-ubuntu", "sandbox-gateway"):
        assert f"{image}:{version}" in args
        assert f"{image}:latest" not in args
    assert f"sandbox-fs:{fs_version}" in args
    assert "sandbox-fs:latest" not in args


@pytest.mark.parametrize("name", ("skills", "todo", "memory"))
def test_legacy_cli_is_available_and_starts(name: str) -> None:
    cli = REPO_ROOT / "images/code/bin" / f"{name}.py"
    result = subprocess.run([sys.executable, str(cli), "--help"],
                            capture_output=True, text=True, check=True)
    assert "usage:" in result.stdout.lower()
