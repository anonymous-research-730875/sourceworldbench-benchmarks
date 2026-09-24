"""copies a git worktree from a Docker image to tmp for augmentation."""

from __future__ import annotations

import logging
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sourceworldbench_benchmarks.augmentations.runner import AugmentationError

logger = logging.getLogger(__name__)


def _run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(args, capture_output=True, text=True, check=check)
    except FileNotFoundError as exc:
        raise AugmentationError(f"docker executable not found: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        cmd = " ".join(args[:4])
        raise AugmentationError(f"{cmd} failed: {detail}") from exc


def ensure_image(image: str) -> None:
    """Pull ``image`` when it is not present locally."""
    inspect = _run(["docker", "image", "inspect", image], check=False)
    if inspect.returncode == 0:
        return
    logger.info("pulling image %s", image)
    _run(["docker", "pull", image])


def copy_worktree_from_image(image: str, dest: Path, *, workdir: str = "/app") -> None:
    """Copy ``workdir`` from ``image`` into ``dest``."""
    ensure_image(image)
    create = _run(["docker", "create", image])
    container_id = create.stdout.strip()
    if not container_id:
        raise AugmentationError(f"docker create returned no container id for {image!r}")
    try:
        dest.mkdir(parents=True, exist_ok=True)
        _run(["docker", "cp", f"{container_id}:{workdir}/.", str(dest)])
    finally:
        _run(["docker", "rm", container_id])


@contextmanager
def temporary_worktree_from_image(
    image: str,
    *,
    workdir: str = "/app",
) -> Iterator[Path]:
    """Extract ``workdir`` from ``image`` into a temp directory, then delete it."""
    with tempfile.TemporaryDirectory(prefix="sourceworldbench-augment-") as tmp:
        repo = Path(tmp) / "repo"
        copy_worktree_from_image(image, repo, workdir=workdir)
        yield repo
