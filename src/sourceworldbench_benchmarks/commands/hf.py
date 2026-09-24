import json
from pathlib import Path
from typing import Any

import typer
from huggingface_hub import HfApi

from sourceworldbench_benchmarks.dataset_io import (
    DEFAULT_DATASET_REPO_ID,
    commit_description,
    load_existing_rows,
    push_rows,
    read_jsonl,
    upsert_rows,
)
from sourceworldbench_benchmarks.gcs import GcsFiles
from sourceworldbench_benchmarks.k8s.config import DEFAULT_REMOTE_ROOT
from sourceworldbench_benchmarks.schema import STATE_OUTCOME_FIELDS

app = typer.Typer(
    add_completion=False, help="Manage the sourceworldbench-benchmarks Hugging Face dataset."
)


def _is_filled(row: dict[str, Any]) -> bool:
    """A row is filled when every test-outcome field is non-None."""
    return all(row.get(field) is not None for field in STATE_OUTCOME_FIELDS)


def _confirm(message: str, *, assume_yes: bool, default: bool = False) -> bool:
    if assume_yes:
        return True
    return typer.confirm(message, default=default)


@app.command()
def create(
    repo_id: str = typer.Argument(
        ...,
        help="Target Hugging Face dataset repo id, e.g. 'anonymous-research-730875/sourceworldbench-benchmarks-poc'.",
    ),
    private: bool = typer.Option(
        False, "--private", help="Create the dataset as a private repo."
    ),
    seed: Path | None = typer.Option(
        None,
        "--seed",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Optional JSONL file to seed the dataset with after creation.",
    ),
) -> None:
    """Create an empty dataset repo on the Hub (optionally seeded from a JSONL)."""
    api = HfApi()
    url = api.create_repo(
        repo_id=repo_id, repo_type="dataset", private=private, exist_ok=True
    )
    typer.echo(f"Dataset repo ready: {url}")

    if seed is not None:
        rows = read_jsonl(seed)
        if not rows:
            typer.echo(f"Seed file {seed} is empty; nothing to push.")
            return
        push_rows(
            repo_id,
            [row.model_dump() for row in rows],
            commit_message="Add dataset points",
            commit_description=commit_description([row.instance_id for row in rows]),
        )
        typer.echo(f"Pushed {len(rows)} rows from {seed} to {repo_id}.")


@app.command()
def append(
    repo_id: str = typer.Argument(..., help="Target Hugging Face dataset repo id."),
    jsonl: Path = typer.Argument(
        ...,
        exists=True,
        dir_okay=False,
        readable=True,
        help="JSONL file with rows to append.",
    ),
    override: bool = typer.Option(
        False,
        "--override/--skip-existing",
        help="On instance_id collision: --override rewrites the dataset replacing collisions; "
        "--skip-existing keeps existing rows and ignores incoming duplicates.",
    ),
    allow_partial: bool = typer.Option(
        False,
        "--allow-partial",
        help="Allow pushing rows with missing test-outcome fields.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Assume yes to all confirmation prompts."
    ),
) -> None:
    """Append rows from a JSONL file to the dataset on the Hub."""
    incoming = read_jsonl(jsonl)
    if not incoming:
        typer.echo(f"No rows in {jsonl}; nothing to do.")
        return

    partial_ids = [
        row.instance_id for row in incoming if not _is_filled(row.model_dump())
    ]
    if partial_ids:
        typer.echo(
            f"{len(partial_ids)} incoming rows are partial (test-outcome fields are None):"
        )
        for instance_id in partial_ids[:10]:
            typer.echo(f"  - {instance_id}")
        if len(partial_ids) > 10:
            typer.echo(f"  ... and {len(partial_ids) - 10} more")
        if not allow_partial and not _confirm(
            "Append partial rows anyway?", assume_yes=yes
        ):
            typer.echo("Aborted.")
            raise typer.Exit(code=1)

    existing = load_existing_rows(repo_id)
    existing_ids = {row["instance_id"] for row in existing}
    incoming_by_id = {row.instance_id: row for row in incoming}
    collisions = sorted(set(incoming_by_id) & existing_ids)

    if collisions:
        typer.echo(
            f"{len(collisions)} incoming instance_ids already exist in {repo_id}:"
        )
        for instance_id in collisions[:10]:
            typer.echo(f"  - {instance_id}")
        if len(collisions) > 10:
            typer.echo(f"  ... and {len(collisions) - 10} more")
        if not _confirm(
            f"Proceed with mode={'override' if override else 'skip-existing'}?",
            assume_yes=yes,
            default=True,
        ):
            typer.echo("Aborted.")
            raise typer.Exit(code=1)

    if override:
        kept = [row for row in existing if row["instance_id"] not in incoming_by_id]
        new_rows = kept + [row.model_dump() for row in incoming]
        affected_ids = [row.instance_id for row in incoming]
        commit_message = "Upsert dataset points"
    else:
        new_incoming = [
            row.model_dump() for row in incoming if row.instance_id not in existing_ids
        ]
        new_rows = existing + new_incoming
        affected_ids = [row["instance_id"] for row in new_incoming]
        commit_message = "Add dataset points"

    if not affected_ids:
        typer.echo("Nothing to push after applying collision policy.")
        return

    push_rows(
        repo_id,
        new_rows,
        commit_message=commit_message,
        commit_description=commit_description(affected_ids),
    )
    added = len(new_rows) - len(existing)
    typer.echo(
        f"Pushed {len(new_rows)} total rows to {repo_id} (net {added:+d} vs previous; override={override})."
    )


_GCS_HTTPS_PREFIX = "https://storage.googleapis.com/"


def load_test_outcomes(source: str | dict[str, Any]) -> dict[str, list[str]]:
    """Fetch a row's ``result.json`` from GCS and return only its test-outcome lists.

    ``source`` is either the URL itself (``https://storage.googleapis.com/...``
    or ``gs://...``), or a full row dict whose ``metadata['gke_path']`` holds
    the URL.

    Reads through ``GcsFiles`` (authenticated ``google.cloud.storage`` client)
    since the bucket is private — raw HTTPS gets a 403.  Requires default GCP
    credentials to be configured locally (``gcloud auth application-default
    login``).

    Returns a dict with exactly the keys in ``STATE_OUTCOME_FIELDS``
    (``passed_tests``, ``failed_tests``, ``skipped_tests``, ``errored_tests``,
    ``discovery_errors``); each value is the list from the result (or ``[]`` if
    the field is absent or null).
    """
    if isinstance(source, dict):
        metadata = source.get("metadata") or {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata) if metadata else {}
        url = metadata.get("gke_path") if isinstance(metadata, dict) else None
        if not url:
            raise ValueError("row has no metadata['gke_path'] to load from")
    else:
        url = source

    if url.startswith(_GCS_HTTPS_PREFIX):
        gs_uri = "gs://" + url[len(_GCS_HTTPS_PREFIX):]
    elif url.startswith("gs://"):
        gs_uri = url
    else:
        raise ValueError(f"unsupported URL scheme (expected gs:// or {_GCS_HTTPS_PREFIX}): {url!r}")

    payload = json.loads(GcsFiles().read_bytes(gs_uri).decode("utf-8"))
    return {field: (payload.get(field) or []) for field in STATE_OUTCOME_FIELDS}


def _resolve_execution_data_uri(source: str | dict[str, Any], tracer: str) -> str:
    """Return a ``gs://`` URI for ``execution_data[tracer]``.

    ``source`` is either the URI itself (``gs://`` or
    ``https://storage.googleapis.com/``) or a row dict whose ``execution_data``
    maps tracer name → URI.
    """
    if isinstance(source, dict):
        execution_data = source.get("execution_data") or {}
        if isinstance(execution_data, str):
            execution_data = json.loads(execution_data) if execution_data else {}
        url = execution_data.get(tracer) if isinstance(execution_data, dict) else None
        if not url:
            raise ValueError(f"row has no execution_data[{tracer!r}] to load from")
    else:
        url = source

    if url.startswith(_GCS_HTTPS_PREFIX):
        return "gs://" + url[len(_GCS_HTTPS_PREFIX):]
    if url.startswith("gs://"):
        return url
    raise ValueError(f"unsupported URL scheme (expected gs:// or {_GCS_HTTPS_PREFIX}): {url!r}")


def load_execution_data(source: str | dict[str, Any], tracer: str) -> dict[str, Any]:
    """Fetch a row's tracer output from GCS and return its parsed JSON.

    ``source`` is either a ``gs://`` / ``https://storage.googleapis.com/`` URI
    pointing at a tracer output file directly, or a full row dict whose
    ``execution_data`` maps tracer name → URI (in which case ``tracer`` selects
    which one to load — e.g. ``"cprofile"``).

    Reads through ``GcsFiles``; requires default GCP credentials
    (``gcloud auth application-default login``).

    Returns the JSON body as a dict.
    """
    gs_uri = _resolve_execution_data_uri(source, tracer)
    raw = GcsFiles().read_bytes(gs_uri)
    assert raw is not None
    return json.loads(raw.decode("utf-8"))


def save_execution_data(source: str | dict[str, Any], tracer: str, dest: Path) -> Path:
    """Download a row's tracer output from GCS to a local file.

    ``source`` and ``tracer`` resolve the same way as `load_execution_data`.
    ``dest`` may be:
      - an existing directory (or a path with no suffix): the URI's basename is
        used as the filename inside it (parent dirs are created).
      - a full file path: the file is written there (its parent dirs are created).

    Returns the final on-disk path.
    """
    gs_uri = _resolve_execution_data_uri(source, tracer)
    basename = gs_uri.rsplit("/", 1)[-1]

    if dest.is_dir() or dest.suffix == "":
        target_dir = dest
        target_name = basename
    else:
        target_dir = dest.parent
        target_name = dest.name
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / target_name

    raw = GcsFiles().read_bytes(gs_uri)
    assert raw is not None
    target_path.write_bytes(raw)
    return target_path


def _default_run_name_from_jsonl(jsonl: Path) -> str:
    """Derive a k8s run name from the JSONL filename (strip extension, ``_`` -> ``-``)."""
    stem = jsonl.name.split(".", 1)[0]
    return stem.replace("_", "-")


def _gke_result_url(*, remote_root: str, run_name: str, instance_id: str) -> str:
    """Build the public https:// URL of a worker's ``result.json`` on GCS.

    ``remote_root`` may be either a ``gs://bucket/prefix`` URI or an
    ``https://storage.googleapis.com/bucket/prefix`` URL — the output is always
    normalised to https so it can be pasted straight into a browser.
    """
    if remote_root.startswith("gs://"):
        remote_root = "https://storage.googleapis.com/" + remote_root[len("gs://"):]
    return f"{remote_root.rstrip('/')}/{run_name}/workers/{instance_id}/result.json"


def _row_with_gke_path(row: dict[str, Any], gke_path: str) -> dict[str, Any]:
    """Return a copy of ``row`` with ``metadata['gke_path']`` set to ``gke_path``.

    Handles ``metadata`` being either a dict (JSONL from k8s download) or a
    JSON-encoded string (rows loaded from the Hub parquet).
    """
    new_row = dict(row)
    metadata = new_row.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata) if metadata else {}
        except json.JSONDecodeError:
            metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    metadata = {**metadata, "gke_path": gke_path}
    new_row["metadata"] = metadata
    return new_row


@app.command("add-gke-path")
def add_gke_path(
    jsonl: Path = typer.Argument(
        ...,
        exists=True,
        dir_okay=False,
        readable=True,
        help="JSONL file with collected rows (typically the output of `k8s download`).",
    ),
    repo_id: str = typer.Option(
        DEFAULT_DATASET_REPO_ID,
        "--repo-id",
        help="Target Hugging Face dataset repo id.",
    ),
    run_name: str | None = typer.Option(
        None,
        "--run-name",
        help=(
            "K8s run name used to build the GCS result URL. If omitted, derived "
            "from the JSONL filename by stripping the extension and replacing "
            "'_' with '-' (e.g. 'farid_k8s_3k_test_0' -> 'farid-k8s-3k-test-0')."
        ),
    ),
    remote_root: str = typer.Option(
        DEFAULT_REMOTE_ROOT,
        "--remote-root",
        help="GCS prefix under which the run directory lives (gs:// or https://).",
    ),
    out: Path | None = typer.Option(
        None,
        "--out",
        help="Write patched rows to a local JSONL instead of pushing to the Hub (for inspection).",
    ),
    override: bool = typer.Option(
        True,
        "--override/--skip-existing",
        help="On instance_id collision: --override replaces existing rows (default), "
        "--skip-existing keeps existing rows and ignores incoming duplicates.",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Assume yes to all confirmation prompts."),
) -> None:
    """Add ``metadata['gke_path']`` to each row in ``jsonl`` and push to the Hub.

    The gke_path points to the row's ``result.json`` on GCS, using the URL
    scheme:

        {remote_root}/{run_name}/workers/{instance_id}/result.json

    Existing test-outcome lists in each row are left untouched — this is a pure
    metadata annotation.  Intended for uncollected rows (null test-outcome
    fields) so the incoming rows stay small; use ``load_test_outcomes(row)`` on
    the consumer side to fetch the actual test lists from GCS on demand.
    """
    incoming = read_jsonl(jsonl)
    if not incoming:
        typer.echo(f"No rows in {jsonl}; nothing to do.")
        return

    resolved_run_name = run_name or _default_run_name_from_jsonl(jsonl)
    typer.echo(f"Using run_name={resolved_run_name!r}, remote_root={remote_root!r}")

    incoming_by_id: dict[str, dict[str, Any]] = {}
    for row in incoming:
        row_dict = row.model_dump()
        gke_path = _gke_result_url(
            remote_root=remote_root,
            run_name=resolved_run_name,
            instance_id=row.instance_id,
        )
        incoming_by_id[row.instance_id] = _row_with_gke_path(row_dict, gke_path)

    sample_id = next(iter(incoming_by_id))
    typer.echo(f"Sample gke_path: {incoming_by_id[sample_id]['metadata']['gke_path']}")

    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as handle:
            for row_dict in incoming_by_id.values():
                handle.write(json.dumps(row_dict, sort_keys=True) + "\n")
        typer.echo(f"Wrote {len(incoming_by_id)} annotated row(s) to {out}. Not pushed.")
        return

    existing = load_existing_rows(repo_id)
    existing_ids = {row["instance_id"] for row in existing}
    collisions = sorted(set(incoming_by_id) & existing_ids)
    if collisions and not override:
        incoming_by_id = {iid: row for iid, row in incoming_by_id.items() if iid not in existing_ids}
        typer.echo(
            f"--skip-existing: dropping {len(collisions)} incoming id(s) already in {repo_id}; "
            f"{len(incoming_by_id)} row(s) remain to push."
        )
        if not incoming_by_id:
            typer.echo("Nothing to push.")
            return

    typer.echo(
        f"Upserting {len(incoming_by_id)} annotated row(s) into {repo_id} "
        f"(mode={'override' if override else 'skip-existing'})."
    )
    if not _confirm("Proceed?", assume_yes=yes, default=True):
        typer.echo("Aborted.")
        raise typer.Exit(code=1)

    merged = upsert_rows(existing, incoming_by_id)
    push_rows(
        repo_id,
        merged,
        commit_message=f"Add gke_path metadata for run {resolved_run_name} ({len(incoming_by_id)} row(s))",
        commit_description=commit_description(list(incoming_by_id)),
    )
    typer.echo(f"Done. Dataset now has {len(merged)} total rows in {repo_id}.")


@app.command("list")
def list_rows(
    repo_id: str = typer.Argument(..., help="Target Hugging Face dataset repo id."),
    filled_only: bool = typer.Option(
        False,
        "--filled-only",
        help="Only list rows where every test-outcome field is non-None.",
    ),
    partial_only: bool = typer.Option(
        False,
        "--partial-only",
        help="Only list rows where at least one test-outcome field is None.",
    ),
    show_status: bool = typer.Option(
        False,
        "--show-status",
        help="Annotate each instance_id with 'filled' or 'partial'.",
    ),
) -> None:
    """List instance_ids currently in the dataset."""
    if filled_only and partial_only:
        raise typer.BadParameter(
            "--filled-only and --partial-only are mutually exclusive."
        )

    existing = load_existing_rows(repo_id)
    if not existing:
        typer.echo(f"Dataset {repo_id} is empty or does not exist.")
        return

    filled_count = 0
    for row in sorted(existing, key=lambda r: r["instance_id"]):
        is_filled = _is_filled(row)
        if is_filled:
            filled_count += 1
        if filled_only and not is_filled:
            continue
        if partial_only and is_filled:
            continue
        if show_status:
            typer.echo(f"{row['instance_id']}\t{'filled' if is_filled else 'partial'}")
        else:
            typer.echo(row["instance_id"])

    typer.echo(
        f"# total={len(existing)} filled={filled_count} partial={len(existing) - filled_count}",
        err=True,
    )


@app.command()
def remove(
    repo_id: str = typer.Argument(..., help="Target Hugging Face dataset repo id."),
    instance_id: list[str] = typer.Option(
        ...,
        "--instance-id",
        help="Instance id to remove. Repeat to remove multiple.",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
) -> None:
    """Remove rows by instance_id (rewrites the dataset without them)."""
    existing = load_existing_rows(repo_id)
    if not existing:
        typer.echo(f"Dataset {repo_id} is empty or does not exist; nothing to remove.")
        return

    to_remove = set(instance_id)
    existing_ids = {row["instance_id"] for row in existing}
    matched = sorted(to_remove & existing_ids)
    missing = sorted(to_remove - existing_ids)

    if missing:
        typer.echo(f"{len(missing)} instance_ids not present in {repo_id}:")
        for missing_id in missing:
            typer.echo(f"  - {missing_id}")

    if not matched:
        typer.echo("Nothing to remove.")
        raise typer.Exit(code=1 if missing else 0)

    typer.echo(f"About to remove {len(matched)} row(s) from {repo_id}:")
    for matched_id in matched[:10]:
        typer.echo(f"  - {matched_id}")
    if len(matched) > 10:
        typer.echo(f"  ... and {len(matched) - 10} more")
    if not _confirm("Proceed?", assume_yes=yes, default=False):
        typer.echo("Aborted.")
        raise typer.Exit(code=1)

    kept = [row for row in existing if row["instance_id"] not in to_remove]
    if kept:
        push_rows(
            repo_id,
            kept,
            commit_message="Remove dataset points",
            commit_description=commit_description(matched),
        )
        typer.echo(f"Pushed {len(kept)} rows to {repo_id} (removed {len(matched)}).")
    else:
        typer.echo(
            "Refusing to push an empty dataset — would leave the repo with no data. "
            "Delete the repo manually if that's what you want.",
            err=True,
        )
        raise typer.Exit(code=1)
