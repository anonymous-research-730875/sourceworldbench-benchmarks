import logging
from pathlib import Path

import typer

from sourceworldbench_benchmarks.augmentations import git
from sourceworldbench_benchmarks.augmentations.docker_worktree import temporary_worktree_from_image
from sourceworldbench_benchmarks.augmentations.runner import AugmentationError, fail
from sourceworldbench_benchmarks.dataset_io import DEFAULT_DATASET_REPO_ID, resolve_source

logger = logging.getLogger(__name__)

app = typer.Typer(add_completion=False)


def _build_from_repo(repo: Path, out: Path, *, branch: str) -> list[dict]:
    repo = repo.resolve()
    if not git.is_git_worktree(repo):
        fail(f"not a git working tree: {repo}")
    try:
        return git.build_git_history(repo, out, branch=branch)
    except ValueError as exc:
        fail(str(exc))


@app.command("build-git-history")
def build_git_history_cmd(
    out: Path = typer.Option(
        ...,
        "--out",
        help="Write the report here (e.g. reports/repo_git_history/<name>__main.json).",
    ),
    repo: Path | None = typer.Option(
        None,
        "--repo",
        help="Path to a local git working tree. Mutually exclusive with --base-instance-id.",
    ),
    base_instance_id: str | None = typer.Option(
        None,
        "--base-instance-id",
        help="Bare base row in the HF dataset; the repo is extracted from its container image.",
    ),
    in_path: str = typer.Option(
        DEFAULT_DATASET_REPO_ID,
        "--in",
        help="HF dataset repo id (or local JSONL path) to load the row from.",
    ),
    branch: str = typer.Option(
        "main",
        "--branch",
        help="Branch whose commit history to record.",
    ),
) -> None:
    """Write a git history report for use with augment git-history-main."""
    if repo is not None and base_instance_id is not None:
        fail("provide only one of --repo or --base-instance-id")

    if repo is not None:
        commits = _build_from_repo(repo, out, branch=branch)
    elif base_instance_id is not None:
        source_rows, _ = resolve_source(in_path)
        by_id = {row["instance_id"]: row for row in source_rows}
        if base_instance_id not in by_id:
            fail(f"instance_id {base_instance_id!r} not found in {in_path}")
        row = by_id[base_instance_id]
        if row.get("patch"):
            fail(
                f"instance_id {base_instance_id!r} has a patch; "
                "build-git-history needs a bare base row (patch=null)"
            )
        container = row.get("container")
        if not container:
            fail(f"instance_id {base_instance_id!r} has no container field")
        try:
            with temporary_worktree_from_image(container) as worktree:
                commits = _build_from_repo(worktree, out, branch=branch)
        except AugmentationError as exc:
            fail(str(exc))
    else:
        fail("provide --repo or --base-instance-id")

    typer.echo(f"wrote {len(commits)} commits to {out}")
    logger.info("wrote %d commits to %s", len(commits), out)
