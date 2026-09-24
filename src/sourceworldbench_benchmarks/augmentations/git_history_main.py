"""Extract augmentation patches straight from a repo's git history.

The patch for "repo X at commit Y with step s" is computed as follows:

1. Read the git history report for X (``reports/repo_git_history/<name>__main.json``).
2. In its ``commits`` list (sorted oldest -> newest) find the entry whose ``sha`` is Y.
3. Take the entry ``s`` list-indices away from Y -- the target commit ``Y + s``.
4. The patch is ``git diff Y (Y + s)``: the change that turns state Y into state Y + s.

Note: when ``s`` is negative the target is an *older* commit, so the patch turns a
future state into a past one, which can be counter-intuitive (it reverses history).

Example::

    sourceworldbench-benchmarks build-git-history \\
        --repo repos/rich --out reports/repo_git_history/rich__main.json
        
    sourceworldbench-benchmarks augment git-history-main \\
        --repo repos/rich --report reports/repo_git_history/rich__main.json \\
        --base-commit efe8f619138327dd3e8fe490289d1b0e6da4a0e8 --step 46 \\
        --exclude tests --exclude docs --exclude CHANGELOG.md

    sourceworldbench-benchmarks augment git-history-main \\
        --image sourceworldbench/rich__efe8f619:dev --report reports/repo_git_history/rich__main.json \\
        --base-commit efe8f619138327dd3e8fe490289d1b0e6da4a0e8 --step 46 \\
        --exclude tests --exclude docs --exclude CHANGELOG.md
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import typer

from sourceworldbench_benchmarks.augmentations import git
from sourceworldbench_benchmarks.augmentations.docker_worktree import temporary_worktree_from_image
from sourceworldbench_benchmarks.augmentations.runner import AugmentationError, fail
from sourceworldbench_benchmarks.dataset_io import DEFAULT_DATASET_REPO_ID

logger = logging.getLogger(__name__)


def _load_commits(report: Path) -> list[dict]:
    """Read the git history report and return its ``commits`` list."""
    if not report.exists():
        raise AugmentationError(f"report does not exist: {report}")
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AugmentationError(f"could not read report {report}: {exc}") from exc

    commits = data.get("commits") if isinstance(data, dict) else None
    if not isinstance(commits, list) or not commits:
        raise AugmentationError(f"report {report} has no 'commits' list")
    return commits


def _resolve_base_sha(repo: Path, base_commit: str) -> str:
    """Resolve the base commit Y to a full sha."""
    try:
        return git.rev_parse(repo, base_commit)
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() if exc.stderr else str(exc)
        raise AugmentationError(f"invalid base commit {base_commit!r}: {detail}") from exc


def _find_index(commits: list[dict], base_sha: str) -> int:
    """Return the index of the commit whose sha is `base_sha`."""
    for index, commit in enumerate(commits):
        sha = commit.get("sha", "")
        if sha == base_sha or (len(base_sha) >= 7 and sha.startswith(base_sha)):
            return index
    raise AugmentationError(f"base commit {base_sha} not found in report's commit history")


def _target_sha(commits: list[dict], base_index: int, step: int) -> str:
    """Resolve the sha of the commit `step` indices away from `base_index`."""
    target_index = base_index + step
    if not 0 <= target_index < len(commits):
        raise AugmentationError(
            f"step {step:+d} from index {base_index} lands at {target_index}, "
            f"out of range [0, {len(commits) - 1}]"
        )
    return commits[target_index]["sha"]


def _diff(
    repo: Path,
    base_sha: str,
    target_sha: str,
    scope: Sequence[str],
    exclude: Sequence[str],
) -> str:
    """Return ``git diff base_sha target_sha``, kept to `scope` and minus `exclude`."""
    # ``--binary`` (which implies ``--full-index``) embeds the payload for binary
    # files so ``git apply`` can reconstruct them; without it git only emits a
    # "Binary files ... differ" placeholder that fails to apply.
    args = ["diff", "--no-color", "--binary", base_sha, target_sha]
    pathspecs = [*scope, *(f":(exclude){path}" for path in exclude)]
    if pathspecs:
        args += ["--", *pathspecs]
    try:
        return git.git(repo, *args).stdout
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() if exc.stderr else str(exc)
        raise AugmentationError(f"git diff {base_sha}..{target_sha} failed: {detail}") from exc


def git_history_patch(
    *,
    repo: Path,
    report: Path,
    step: int,
    base_commit: str,
    scope: Sequence[str] = (),
    exclude: Sequence[str] = (),
    out: Path | None = None,
) -> str:
    """Compute the patch between commit Y and the commit `step` away in git history.

    Parameters
    ----------
    repo: Path to the target git working tree (must contain both commits).
    report: Path to the git history report (``<name>__main.json``).
    step: Signed offset in the report's commit list from the base commit Y.
    base_commit: The base commit Y to augment from.
    scope: Paths to restrict the diff to.
    exclude: Paths to drop from the diff (applied after `scope`).
    out: If provided, write the diff to this path. Otherwise, write to stdout.

    Returns the unified diff as a string.
    """
    repo = repo.resolve()
    if not git.is_git_worktree(repo):
        raise AugmentationError(f"not a git working tree: {repo}")

    commits = _load_commits(report)
    base_sha = _resolve_base_sha(repo, base_commit)
    base_index = _find_index(commits, base_sha)
    target_sha = _target_sha(commits, base_index, step)

    logger.info(
        "augmenting %s at %s with step %+d -> %s",
        repo.name,
        base_sha[:10],
        step,
        target_sha[:10],
    )

    diff = _diff(repo, base_sha, target_sha, scope, exclude)
    if not diff.strip():
        raise AugmentationError(f"git diff {base_sha[:10]}..{target_sha[:10]} produced no changes")

    if out is None:
        sys.stdout.write(diff)
    else:
        out.write_text(diff, encoding="utf-8")
        logger.info("wrote patch to %s (%d bytes)", out, len(diff))
    return diff


def git_history_main_hf_cmd(
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
    report: Path = typer.Option(
        ..., "--report", help="Path to the git history report (reports/repo_git_history/<name>__main.json)."
    ),
    step: int = typer.Option(..., "--step", help="Signed offset in the commit history from the base commit."),
    base_commit: str | None = typer.Option(
        None, "--base-commit", help="Override the base commit; defaults to the row's base_commit field."
    ),
    scope: list[str] = typer.Option([], "--scope", help="Path(s) to restrict the diff to. Repeatable."),
    exclude: list[str] = typer.Option(
        [], "--exclude", help="Path(s) to drop from the diff (applied after --scope). Repeatable."
    ),
) -> None:
    """Emit the git-history patch for the row's container and push a new augmented row."""
    from sourceworldbench_benchmarks.augmentations.docker_worktree import temporary_worktree_from_image
    from sourceworldbench_benchmarks.commands.augment_hf import load_base_row, register_augmented_row

    base_row = load_base_row(base_instance_id, in_path=in_path)
    resolved_base_commit = base_commit or base_row["base_commit"]
    try:
        with temporary_worktree_from_image(base_row["container"]) as worktree:
            diff = git_history_patch(
                repo=worktree,
                report=report,
                step=step,
                base_commit=resolved_base_commit,
                scope=scope,
                exclude=exclude,
            )
    except AugmentationError as exc:
        fail(str(exc))
        return
    register_augmented_row(
        base_row=base_row,
        suffix=suffix,
        diff=diff,
        overwrite=overwrite,
        in_path=in_path,
        push=push,
        out_path=out_path,
    )


def git_history_main_cmd(
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
    report: Path = typer.Option(
        ...,
        "--report",
        help="Path to the git history report (reports/repo_git_history/<name>__main.json).",
    ),
    step: int = typer.Option(
        ...,
        "--step",
        help="Signed offset in the commit history from the base commit (e.g. 1, -3).",
    ),
    base_commit: str = typer.Option(
        ...,
        "--base-commit",
        help="The base commit Y to augment from.",
    ),
    scope: list[str] = typer.Option(
        [],
        "--scope",
        help="Path(s) within the repo to restrict the diff to. Repeatable.",
    ),
    exclude: list[str] = typer.Option(
        [],
        "--exclude",
        help="Path(s) to drop from the diff (applied after --scope). Repeatable.",
    ),
    out: Path | None = typer.Option(
        None, "--out", help="Write the patch here. Defaults to stdout."
    ),
) -> None:
    """Emit the patch between a base commit and the commit `step` away in git history."""
    try:
        if repo is not None and image is not None:
            raise AugmentationError("provide only one of --image or --repo")
        if repo is not None:
            git_history_patch(
                repo=repo,
                report=report,
                step=step,
                base_commit=base_commit,
                scope=scope,
                exclude=exclude,
                out=out,
            )
        elif image is not None:
            with temporary_worktree_from_image(image) as repo_path:
                git_history_patch(
                    repo=repo_path,
                    report=report,
                    step=step,
                    base_commit=base_commit,
                    scope=scope,
                    exclude=exclude,
                    out=out,
                )
        else:
            raise AugmentationError("provide --image or --repo")
    except AugmentationError as exc:
        fail(str(exc))
