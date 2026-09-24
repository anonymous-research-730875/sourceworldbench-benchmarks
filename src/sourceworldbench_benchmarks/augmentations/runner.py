"""Shared runner for augmentation commands.

An augmentation mutates a git working tree in-place. This module provides the
plumbing around that step: ensuring the tree is clean before running, capturing
the resulting diff, and restoring the tree afterwards.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import NoReturn

import typer

from sourceworldbench_benchmarks.augmentations import git

logger = logging.getLogger(__name__)


class AugmentationError(RuntimeError):
    """Raised when an augmentation cannot be produced as requested."""


def _ensure_git_repo(repo: Path) -> None:
    if not repo.exists():
        raise AugmentationError(f"repo does not exist: {repo}")
    if not git.is_git_worktree(repo):
        raise AugmentationError(f"not a git working tree: {repo}")


def _ensure_clean(repo: Path) -> None:
    status = git.status_porcelain(repo)
    if status.strip():
        raise AugmentationError(f"repo has uncommitted changes; commit or stash before augmenting:\n{status}")


def _resolve_base_commit(repo: Path, base_commit: str | None) -> str | None:
    if base_commit is None:
        return None
    try:
        return git.rev_parse(repo, base_commit)
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() if exc.stderr else str(exc)
        raise AugmentationError(f"invalid --base-commit {base_commit!r}: {detail}") from exc


def run_augmentation(
    *,
    repo: Path,
    base_commit: str | None = None,
    scope: Sequence[str],
    mutate: Callable[[Path], None],
    out: Path | None,
    restore: bool = True,
) -> str:
    """Run `mutate` against `repo` and emit the resulting diff.

    Parameters
    ----------
    repo: Path to a clean git working tree.
    base_commit: Git revision to check out before augmenting. If None, use the
        current checkout.
    scope: Paths passed to the augmentation and `git diff`.
    mutate: Callable that performs the augmentation in-place against `repo`.
    out: If provided, write the diff to this path. Otherwise, write to stdout.
    restore: If True, reset the working tree (and remove new files) after
        capturing the diff.

    Returns the unified diff as a string.
    """
    repo = repo.resolve()
    _ensure_git_repo(repo)
    git.restore_worktree(repo)
    _ensure_clean(repo)
    original_checkout = git.current_checkout(repo)
    resolved_base = _resolve_base_commit(repo, base_commit)

    try:
        if resolved_base is not None:
            git.checkout(repo, resolved_base)
        mutate(repo)
        changed = git.changed_paths(repo)
        if not changed:
            raise AugmentationError("augmentation produced no changes")
        diff = git.capture_diff(repo, scope)
        if not diff.strip():
            raise AugmentationError("no in-scope diff captured (files changed outside scope only?)")
    finally:
        if restore:
            git.restore_worktree(repo)
            if resolved_base is not None:
                git.checkout(repo, original_checkout)

    if out is None:
        sys.stdout.write(diff)
    else:
        out.write_text(diff, encoding="utf-8")
        logger.info("wrote patch to %s (%d bytes)", out, len(diff))
    return diff


def run_augmentations(
    *,
    repo: Path,
    base_commit: str | None = None,
    scope: Sequence[str],
    mutate_many: Callable[[Path], Iterable[None]],
    out: Path,
    restore: bool = True,
) -> list[str]:
    """Run a generator of independent repo mutations and write one patch per mutation.

    `mutate_many(repo)` must apply one mutation, then yield. After each yield, this
    runner captures the current diff, writes it as a patch, and restores the repo
    before resuming the generator for the next mutation.
    """
    repo = repo.resolve()
    out = out.resolve()
    _ensure_git_repo(repo)
    _ensure_clean(repo)
    if out.is_relative_to(repo):
        raise AugmentationError("--out for multi-augmentation output must be outside the target repo")
    if out.exists() and not out.is_dir():
        raise AugmentationError(f"--out exists and is not a directory: {out}")
    if out.exists() and any(out.iterdir()):
        raise AugmentationError(f"--out directory is not empty: {out}")

    original_checkout = git.current_checkout(repo)
    resolved_base = _resolve_base_commit(repo, base_commit)
    patches: list[str] = []

    try:
        if resolved_base is not None:
            git.checkout(repo, resolved_base)
        out.mkdir(parents=True, exist_ok=True)
        for index, _ in enumerate(mutate_many(repo), start=1):
            try:
                changed = git.changed_paths(repo)
                if not changed:
                    raise AugmentationError(f"augmentation {index} produced no changes")
                diff = git.capture_diff(repo, scope)
                if not diff.strip():
                    raise AugmentationError(
                        f"augmentation {index} produced no in-scope diff (files changed outside scope only?)"
                    )
                patch_name = f"{index:06d}.patch"
                (out / patch_name).write_text(diff, encoding="utf-8")
                patches.append(diff)
            finally:
                git.restore_worktree(repo)
    finally:
        if restore and resolved_base is not None:
            git.checkout(repo, original_checkout)

    if not patches:
        raise AugmentationError("augmentation produced no changes")
    logger.info("wrote %d patches to %s", len(patches), out)
    return patches


def fail(message: str) -> NoReturn:
    """Print an error and exit with code 1."""
    print(f"error: {message}", file=sys.stderr)
    raise typer.Exit(code=1)
