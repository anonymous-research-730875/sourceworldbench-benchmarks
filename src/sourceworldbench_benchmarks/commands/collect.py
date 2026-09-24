import subprocess
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn, TimeElapsedColumn

from sourceworldbench_benchmarks.collector import CollectProgressEvent, collect_states
from sourceworldbench_benchmarks.dataset_io import (
    DEFAULT_DATASET_REPO_ID,
    commit_description,
    load_existing_rows,
    push_rows,
    resolve_source,
    upsert_rows,
    write_jsonl,
)
from sourceworldbench_benchmarks.schema import STATE_OUTCOME_FIELDS, StateDatapoint

app = typer.Typer(add_completion=False)


class DockerUnavailableError(RuntimeError):
    """The local Docker daemon could not be reached."""


def _check_docker_available() -> None:
    """Fail fast if `docker info` cannot reach the daemon."""
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
    except FileNotFoundError as exc:
        raise DockerUnavailableError("`docker` binary not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise DockerUnavailableError("`docker info` timed out after 10s; is the daemon running?") from exc
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise DockerUnavailableError(f"docker daemon is not reachable: {stderr}")


def _needs_collect(row: dict) -> bool:
    metadata = row.get("metadata") or {}
    if metadata.get("collection_error"):
        return False
    return all(row.get(field) is None for field in STATE_OUTCOME_FIELDS)


def _event_description(event: CollectProgressEvent) -> str:
    state = event.state.replace("_", " ")
    head = escape(f"{event.instance_id} - {state}")
    detail = f" - {event.message}" if event.message else ""
    return f"{head}{detail}"


_TERMINAL_MARKERS: dict[str, str] = {
    "completed": "[green]✓[/green]",
    "skipped": "[yellow]–[/yellow]",
    "failed": "[red]✗[/red]",
}


@app.command()
def collect(
    in_path: str = typer.Option(
        DEFAULT_DATASET_REPO_ID,
        "--in",
        help="Either input JSONL file or HF dataset with partial StateDatapoint rows.",
    ),
    instance_ids: list[str] = typer.Option(
        [],
        "--instance-id",
        help="Collect specific row(s) by instance_id. Repeatable. Takes precedence over --all/--no-all.",
    ),
    all_rows: bool = typer.Option(
        False,
        "--all/--no-all",
        help="Recalculate all rows including filled ones; default collects only unfilled. Ignored with --instance-id.",
    ),
    timeout: int = typer.Option(600, "--timeout", help="Seconds per docker run."),
    workers: int = typer.Option(1, "--workers", help="Number of rows to collect concurrently."),
    failures_dir: Path = typer.Option(
        Path("failures"),
        "--failures-dir",
        help="Directory for failure artifacts. Defaults to ./failures.",
    ),
    no_progress: bool = typer.Option(False, "--no-progress", help="Disable interactive progress output."),
    push: bool = typer.Option(True, "--push/--no-push", help="Push the dataset to the hub after collecting."),
    out_path: str | None = typer.Option(
        None,
        "--out",
        help="Output path for the collected dataset. If None, the dataset won't be saved locally.",
    ),
) -> None:
    """Fill the test-outcome fields on partial StateDatapoints (single state)."""
    console = Console(stderr=True)
    try:
        _check_docker_available()
    except DockerUnavailableError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    source_rows, push_repo = resolve_source(in_path)
    if not source_rows:
        typer.echo(f"Source {in_path} is empty; nothing to collect.")
        return

    rows_by_id = {row["instance_id"]: row for row in source_rows}

    if instance_ids:
        missing = [iid for iid in instance_ids if iid not in rows_by_id]
        if missing:
            typer.echo(f"error: instance_id(s) not found in {in_path}: {', '.join(missing)}", err=True)
            raise typer.Exit(code=2)
        to_run = [rows_by_id[iid] for iid in instance_ids]
    elif all_rows:
        to_run = source_rows
    else:
        to_run = [row for row in source_rows if _needs_collect(row)]
        if not to_run:
            typer.echo("All rows are already filled; use --all to recalculate.")
            return

    typer.echo(f"Collecting {len(to_run)} row(s) from {in_path}...")

    try:
        states = [StateDatapoint.model_validate(row) for row in to_run]
    except Exception as exc:
        typer.echo(f"error: invalid row in dataset: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    filled_by_id: dict[str, StateDatapoint] = {}

    def on_filled(row: StateDatapoint) -> None:
        filled_by_id[row.instance_id] = row

    try:
        if no_progress:
            summary = collect_states(
                states,
                timeout=timeout,
                workers=workers,
                failures_dir=failures_dir,
                on_filled=on_filled,
            )
            typer.echo(f"done: ok={summary.ok} skip={summary.skip} fail={summary.fail}")
        else:
            with Progress(
                SpinnerColumn(),
                TextColumn("{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                TimeElapsedColumn(),
                console=console,
            ) as progress:
                task = progress.add_task("starting collect", total=len(states))

                def report_progress(event: CollectProgressEvent) -> None:
                    terminal_states = {"completed", "failed", "skipped"}
                    if event.state in terminal_states:
                        marker = _TERMINAL_MARKERS.get(event.state, "")
                        progress.console.print(f"{marker} {_event_description(event)}".strip())
                        progress.update(task, description="", advance=1)
                    else:
                        progress.update(task, description=_event_description(event))

                summary = collect_states(
                    states,
                    timeout=timeout,
                    workers=workers,
                    failures_dir=failures_dir,
                    progress=report_progress,
                    on_filled=on_filled,
                )
                progress.update(task, description=f"done: ok={summary.ok} skip={summary.skip} fail={summary.fail}")
    except NotImplementedError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    filled_rows = {instance_id: filled.model_dump() for instance_id, filled in filled_by_id.items()}

    if out_path is not None:
        out_rows = upsert_rows(source_rows, filled_rows)
        write_jsonl(Path(out_path), out_rows)
        typer.echo(f"Wrote {len(out_rows)} row(s) to {out_path}.")

    if not push:
        typer.echo("Skipping push (--no-push).")
    elif not filled_rows:
        typer.echo("No rows were successfully filled; dataset unchanged.")
    else:
        merged = upsert_rows(load_existing_rows(push_repo), filled_rows)
        push_rows(
            push_repo,
            merged,
            commit_message=f"Fill test outcomes ({len(filled_rows)} row(s))",
            commit_description=commit_description(list(filled_rows)),
        )
        typer.echo(f"Pushed {len(filled_rows)} updated row(s) to {push_repo}.")

    raise typer.Exit(code=0 if summary.fail == 0 else 1)
