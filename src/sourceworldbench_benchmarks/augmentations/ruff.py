import logging
import subprocess
from collections.abc import Callable
from enum import Enum
from pathlib import Path

import typer

from sourceworldbench_benchmarks.augmentations.docker_worktree import temporary_worktree_from_image
from sourceworldbench_benchmarks.augmentations.runner import AugmentationError, fail, run_augmentation
from sourceworldbench_benchmarks.dataset_io import DEFAULT_DATASET_REPO_ID

logger = logging.getLogger(__name__)


class RuffMode(str, Enum):
    """Ruff invocation mode."""

    FORMAT = "format"
    CHECK = "check"


class IndentStyle(str, Enum):
    """Ruff formatter indent style."""

    SPACE = "space"
    TAB = "tab"


def _ensure_format_only(mode: RuffMode, option: str, value: object | None) -> None:
    if value is not None and mode is not RuffMode.FORMAT:
        raise AugmentationError(f"{option} is only valid with --mode format")


def _ruff_command(
    version: str,
    mode: RuffMode,
    scope: list[str],
    select: str | None,
    line_length: int | None,
    indent_style: IndentStyle | None,
    indent_width: int | None,
) -> list[str]:
    _ensure_format_only(mode, "--line-length", line_length)
    _ensure_format_only(mode, "--indent-style", indent_style)
    _ensure_format_only(mode, "--indent-width", indent_width)
    if select and mode is not RuffMode.CHECK:
        raise AugmentationError("--select is only valid with --mode check")

    cmd = ["uvx", f"ruff@{version}", mode.value]
    if indent_style is not None:
        cmd += ["--config", f'format.indent-style = "{indent_style.value}"']
    if indent_width is not None:
        cmd += ["--config", f"indent-width = {indent_width}"]
    if line_length is not None:
        cmd += ["--line-length", str(line_length)]
    if mode is RuffMode.CHECK:
        cmd += ["--fix"]
        if select:
            cmd += ["--select", select]
    cmd += scope or ["."]
    return cmd


def ruff_cmd(
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
        help="Path(s) within the repo that ruff is allowed to touch. Repeatable.",
    ),
    out: Path | None = typer.Option(
        None, "--out", help="Write the diff here. Defaults to stdout."
    ),
    ruff_version: str = typer.Option("0.15.13", "--ruff-version"),
    mode: RuffMode = typer.Option(RuffMode.FORMAT, "--mode"),
    select: str | None = typer.Option(
        None,
        "--select",
        help="Comma-separated rule codes (only with --mode check), e.g. 'I,UP'.",
    ),
    line_length: int | None = typer.Option(
        None,
        "--line-length",
        help="Formatter line length (only with --mode format), e.g. 79.",
    ),
    indent_style: IndentStyle | None = typer.Option(
        None,
        "--indent-style",
        case_sensitive=False,
        help='Formatter indent style: "space" or "tab" (only with --mode format).',
    ),
    indent_width: int | None = typer.Option(
        None,
        "--indent-width",
        min=1,
        help="Spaces per indent level / tab width (only with --mode format), e.g. 2 or 4.",
    ),
    restore: bool = typer.Option(
        True,
        "--restore/--no-restore",
        help="Reset the working tree after capturing the diff.",
    ),
) -> None:
    """Run ruff and emit the resulting patch."""
    mutate = _build_mutate(ruff_version, mode, scope, select, line_length, indent_style, indent_width)
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


def _build_mutate(
    version: str,
    mode: RuffMode,
    scope: list[str],
    select: str | None,
    line_length: int | None,
    indent_style: IndentStyle | None,
    indent_width: int | None,
) -> Callable[[Path], None]:
    def mutate(repo: Path) -> None:
        cmd = _ruff_command(version, mode, scope, select, line_length, indent_style, indent_width)
        logger.info("running: %s (cwd=%s)", " ".join(cmd), repo)
        result = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
        if result.returncode != 0 and mode is RuffMode.FORMAT:
            raise AugmentationError(
                f"ruff format failed (exit {result.returncode}):\n{result.stderr}"
            )

    return mutate


def ruff_hf_cmd(
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
    scope: list[str] = typer.Option([], "--scope", help="Path(s) ruff is allowed to touch. Repeatable."),
    ruff_version: str = typer.Option("0.15.13", "--ruff-version"),
    mode: RuffMode = typer.Option(RuffMode.FORMAT, "--mode"),
    select: str | None = typer.Option(None, "--select", help="Comma-separated rule codes (only with --mode check)."),
    line_length: int | None = typer.Option(None, "--line-length"),
    indent_style: IndentStyle | None = typer.Option(None, "--indent-style", case_sensitive=False),
    indent_width: int | None = typer.Option(None, "--indent-width", min=1),
) -> None:
    """Run ruff against the row's container and push a new augmented row."""
    from sourceworldbench_benchmarks.commands.augment_hf import run_hf_augmentation

    run_hf_augmentation(
        base_instance_id=base_instance_id,
        suffix=suffix,
        overwrite=overwrite,
        scope=scope,
        mutate=_build_mutate(ruff_version, mode, scope, select, line_length, indent_style, indent_width),
        in_path=in_path,
        push=push,
        out_path=out_path,
    )
