"""HF-aware augment command group.

Core helpers (load_base_row, register_augmented_row, run_hf_augmentation,
patch_touches_scope) live here and are imported by each augmenter's *_hf_cmd
via a deferred (inside-function) import to avoid a module-level circular dep:

"""

import re
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer

from sourceworldbench_benchmarks.augmentations import HF_COMMANDS
from sourceworldbench_benchmarks.dataset_io import (
    DEFAULT_DATASET_REPO_ID,
    commit_description,
    load_existing_rows,
    push_repo_for,
    push_rows,
    resolve_source,
    upsert_rows,
    write_jsonl,
)
from sourceworldbench_benchmarks.schema import StateDatapoint

_DIFF_PATH = re.compile(r"^diff --git a/(.+) b/(.+)$", re.MULTILINE)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Run an augmentation against an HF dataset row and push the result as a new row.",
)
for _name, _fn in HF_COMMANDS.items():
    app.command(_name)(_fn)


def patch_touches_scope(patch: str, scope: list[str]) -> bool:
    """True iff the unified diff modifies any path inside one of the scope entries.

    Matching is exact for file entries (`tests/foo.py`) and prefix-based for
    directory entries (`tests/` or `tests` both match every path under `tests/`).
    Both `a/<path>` and `b/<path>` headers are checked so renames-into-scope
    are caught.
    """
    normalized = [entry.rstrip("/") for entry in scope]
    for match in _DIFF_PATH.finditer(patch):
        for path in (match.group(1), match.group(2)):
            for entry in normalized:
                if path == entry or path.startswith(entry + "/"):
                    return True
    return False


def load_base_row(base_instance_id: str, *, in_path: str = DEFAULT_DATASET_REPO_ID) -> dict[str, Any]:
    """Fetch the base row from the source dataset; require it to be bare (patch=None)."""
    source_rows, _ = resolve_source(in_path)
    by_id = {row["instance_id"]: row for row in source_rows}
    if base_instance_id not in by_id:
        typer.echo(
            f"error: instance_id {base_instance_id!r} not found in {in_path}",
            err=True,
        )
        raise typer.Exit(code=2)
    row = by_id[base_instance_id]
    if row.get("patch"):
        typer.echo(
            f"error: instance_id {base_instance_id!r} already has a patch; "
            "augmentations must start from a bare base row",
            err=True,
        )
        raise typer.Exit(code=2)
    return row


def register_augmented_row(
    *,
    base_row: dict[str, Any],
    suffix: str | None,
    diff: str,
    overwrite: bool,
    in_path: str = DEFAULT_DATASET_REPO_ID,
    push: bool = True,
    out_path: str | None = None,
) -> str:
    """Build the new augmented row, write it locally and/or push it; return its instance_id."""
    final_suffix = suffix or datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    stem = base_row["instance_id"].rpartition("__")[0]
    new_instance_id = f"{stem}__{final_suffix}"
    test_scope = list(base_row["test_scope"])
    metadata = {
        "parent": base_row["instance_id"],
        "modifies_test_scope": patch_touches_scope(diff, test_scope),
    }
    try:
        row = StateDatapoint(
            instance_id=new_instance_id,
            repo=base_row["repo"],
            base_commit=base_row["base_commit"],
            container=base_row["container"],
            command=base_row["command"],
            test_scope=test_scope,
            patch=diff,
            metadata=metadata,
        )
    except Exception as exc:
        typer.echo(f"error: invalid StateDatapoint: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    push_repo = push_repo_for(in_path)
    changed = {new_instance_id: row.model_dump()}

    if out_path is not None:
        source_rows, _ = resolve_source(in_path)
        write_jsonl(Path(out_path), upsert_rows(source_rows, changed))
        typer.echo(f"Wrote dataset with {new_instance_id} to {out_path}.")

    if not push:
        typer.echo("Skipping push (--no-push).")
        return new_instance_id

    existing = load_existing_rows(push_repo)
    existed = new_instance_id in {r["instance_id"] for r in existing}
    if existed and not overwrite:
        typer.echo(
            f"error: instance_id {new_instance_id!r} already exists in {push_repo}; pass --overwrite to replace it",
            err=True,
        )
        raise typer.Exit(code=2)

    action = "Replace" if existed else "Add"
    merged = upsert_rows(existing, changed)
    push_rows(
        push_repo,
        merged,
        commit_message=f"{action} augmented row {new_instance_id}",
        commit_description=commit_description([new_instance_id]),
    )
    typer.echo(
        f"{action.lower()}d {new_instance_id} in {push_repo} "
        f"(modifies_test_scope={metadata['modifies_test_scope']}, total rows: {len(merged)})."
    )
    return new_instance_id


def run_hf_augmentation(
    *,
    base_instance_id: str,
    suffix: str | None,
    overwrite: bool,
    scope: list[str],
    mutate: Callable[[Path], None],
    in_path: str = DEFAULT_DATASET_REPO_ID,
    push: bool = True,
    out_path: str | None = None,
) -> None:
    """Standard HF augmentation workflow for single-patch augmenters."""
    from sourceworldbench_benchmarks.augmentations.docker_worktree import temporary_worktree_from_image
    from sourceworldbench_benchmarks.augmentations.runner import AugmentationError, run_augmentation

    base_row = load_base_row(base_instance_id, in_path=in_path)
    with temporary_worktree_from_image(base_row["container"]) as worktree:
        with tempfile.TemporaryDirectory() as tmp:
            patch_path = Path(tmp) / "patch.diff"
            try:
                diff = run_augmentation(
                    repo=worktree,
                    base_commit=None,
                    scope=scope,
                    mutate=mutate,
                    out=patch_path,
                    restore=False,
                )
            except AugmentationError as exc:
                typer.echo(f"error: {exc}", err=True)
                raise typer.Exit(code=1) from exc
    register_augmented_row(
        base_row=base_row,
        suffix=suffix,
        diff=diff,
        overwrite=overwrite,
        in_path=in_path,
        push=push,
        out_path=out_path,
    )
