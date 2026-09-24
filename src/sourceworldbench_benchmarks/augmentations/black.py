import logging
import subprocess
from collections.abc import Callable
from pathlib import Path

import typer

from sourceworldbench_benchmarks.augmentations.docker_worktree import temporary_worktree_from_image
from sourceworldbench_benchmarks.augmentations.runner import AugmentationError, fail, run_augmentation
from sourceworldbench_benchmarks.dataset_io import DEFAULT_DATASET_REPO_ID

logger = logging.getLogger(__name__)


def _black_command(version: str, scope: list[str]) -> list[str]:
    cmd = ["uvx", f"black@{version}"]
    cmd += scope or ["."]
    return cmd


def black_cmd(
    image: str | None = typer.Option(
        None,
        "--image",
        help="Local or remote Docker image with the repo checked out at /app.",
    ),
    repo: Path | None = typer.Option(
        None,
        "--repo",
        help="Path to a clean local git working tree.",
    ),
    base_commit: str | None = typer.Option(
        None,
        "--base-commit",
        help="Git revision to check out before augmenting. Defaults to the current checkout.",
    ),
    scope: list[str] = typer.Option(
        [],
        "--scope",
        help="Path(s) within the repo that black is allowed to touch. Repeatable.",
    ),
    out: Path | None = typer.Option(
        None, "--out", help="Write the diff here. Defaults to stdout."
    ),
    black_version: str = typer.Option("25.1.0", "--black-version"),
    restore: bool = typer.Option(
        True,
        "--restore/--no-restore",
        help="Reset the working tree after capturing the diff.",
    ),
) -> None:
    """Run black and emit the resulting patch."""
    mutate = _build_mutate(black_version, scope)
    try:
        if repo is not None and image is not None:
            raise AugmentationError("provide only one of --image or --repo")
        if repo is not None:
            run_augmentation(
                repo=repo,
                base_commit=base_commit,
                scope=scope,
                mutate=mutate,
                out=out,
                restore=restore,
            )
        elif image is not None:
            if base_commit is not None:
                raise AugmentationError("--base-commit is not supported with --image")
            restore = False
            with temporary_worktree_from_image(image) as repo_path:
                run_augmentation(
                    repo=repo_path,
                    base_commit=base_commit,
                    scope=scope,
                    mutate=mutate,
                    out=out,
                    restore=restore,
                )
        else:
            raise AugmentationError("provide --image or --repo")
    except AugmentationError as exc:
        fail(str(exc))


def _build_mutate(version: str, scope: list[str]) -> Callable[[Path], None]:
    def mutate(repo: Path) -> None:
        cmd = _black_command(version, scope)
        logger.info("running: %s (cwd=%s)", " ".join(cmd), repo)
        result = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
        if result.returncode != 0:
            raise AugmentationError(
                f"black failed (exit {result.returncode}):\n{result.stderr}"
            )

    return mutate


def black_hf_cmd(
    base_instance_id: str = typer.Option(
        ..., "--base-instance-id", help="instance_id of the bare base row in the HF dataset."
    ),
    suffix: str | None = typer.Option(
        None, "--suffix", help="Suffix replacing the base state's `base` segment. Defaults to a UTC timestamp."
    ),
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace an existing row with the same instance_id."),
    in_path: str = typer.Option(
        DEFAULT_DATASET_REPO_ID, "--in", help="Dataset to augment: a local JSONL file or an HF dataset repo id."
    ),
    push: bool = typer.Option(True, "--push/--no-push", help="Push the dataset to the hub after augmenting."),
    out_path: str | None = typer.Option(
        None, "--out", help="Write the resulting dataset to this JSONL path. Skipped when omitted."
    ),
    scope: list[str] = typer.Option([], "--scope", help="Path(s) black is allowed to touch. Repeatable."),
    black_version: str = typer.Option("25.1.0", "--black-version"),
) -> None:
    """Run black against the row's container and push a new augmented row."""
    from sourceworldbench_benchmarks.commands.augment_hf import run_hf_augmentation

    run_hf_augmentation(
        base_instance_id=base_instance_id,
        suffix=suffix,
        overwrite=overwrite,
        scope=scope,
        mutate=_build_mutate(black_version, scope),
        in_path=in_path,
        push=push,
        out_path=out_path,
    )
