"""Slow smoke test for the image builder, gated by the `docker` marker."""

import shutil
import subprocess
from pathlib import Path

import pytest

from sourceworldbench_benchmarks.image_builder import build_image, local_tag

pytestmark = pytest.mark.docker


def _docker_is_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        proc = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            timeout=5,
        )
        return proc.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def test_build_image_local_produces_local_tag(tmp_path: Path) -> None:
    if not _docker_is_available():
        pytest.skip("docker daemon not available")

    env_dir = tmp_path / "environments" / "sourceworldbench-test-fixture"
    env_dir.mkdir(parents=True)
    (env_dir / "Dockerfile").write_text("FROM alpine:3.20\nRUN true\n")

    ref = build_image(
        repo_root=tmp_path,
        instance_id="sourceworldbench-test-fixture",
        push=False,
        tag="latest",
        build_args={},
        ssh=None,
    )
    assert ref == local_tag("sourceworldbench-test-fixture")

    inspect = subprocess.run(
        ["docker", "image", "inspect", ref],
        capture_output=True,
        check=False,
    )
    assert inspect.returncode == 0, inspect.stderr.decode(errors="replace")
