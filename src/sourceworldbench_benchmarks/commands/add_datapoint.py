import json
from pathlib import Path

import typer

from sourceworldbench_benchmarks.dataset_io import (
    DEFAULT_DATASET_REPO_ID,
    commit_description,
    load_existing_rows,
    push_repo_for,
    push_rows,
    upsert_rows,
)
from sourceworldbench_benchmarks.schema import StateDatapoint


def add_datapoint(
    file: Path = typer.Argument(
        ...,
        exists=True,
        dir_okay=False,
        readable=True,
        help="JSON file containing a single StateDatapoint.",
    ),
    dataset: str = typer.Option(
        DEFAULT_DATASET_REPO_ID,
        "--dataset",
        help="Dataset to add into: a local JSONL file or an HF dataset repo id.",
    ),
    overwrite: bool = typer.Option(
        False,
        "--overwrite",
        help="Replace an existing row with the same instance_id instead of failing.",
    ),
) -> None:
    """Add a manually crafted datapoint from a JSON file to the dataset."""
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        typer.echo(f"error: {file}: invalid JSON: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    try:
        row = StateDatapoint.model_validate(raw)
    except Exception as exc:
        typer.echo(f"error: invalid StateDatapoint: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    push_repo = push_repo_for(dataset)
    existing = load_existing_rows(push_repo)
    existed = row.instance_id in {r["instance_id"] for r in existing}
    if existed and not overwrite:
        typer.echo(
            f"error: instance_id {row.instance_id!r} already exists in {push_repo}; pass --overwrite to replace it",
            err=True,
        )
        raise typer.Exit(code=2)

    action = "Replace" if existed else "Add"
    merged = upsert_rows(existing, {row.instance_id: row.model_dump()})
    push_rows(
        push_repo,
        merged,
        commit_message=f"{action} datapoint {row.instance_id}",
        commit_description=commit_description([row.instance_id]),
    )
    typer.echo(f"{action.lower()}d {row.instance_id} in {push_repo} (total rows: {len(merged)}).")
