import re
from pathlib import Path

import typer

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
from sourceworldbench_benchmarks.test_runner_registry import (
    DEFAULT_RUN_TESTS_COMMAND_REGISTRY,
    DEFAULT_TEST_SCOPE_REGISTRY,
)

_GITHUB_REPO_URL = re.compile(r"https://github\.com/([^/\s\"']+/[^/\s\"']+)\.git")


def _instance_id_from_container(container: str) -> str:
    """`ghcr.io/foo/hvac__09902dea@sha256:…` -> `hvac__09902dea` (the `<repo>__<base_commit>` stem)."""
    last = container.rsplit("/", 1)[-1]
    return last.split("@", 1)[0].split(":", 1)[0]


def _repo_from_environment(base_stem: str) -> str:
    """Read `<owner>/<repo>` from the github.com URL inside `environments/<base_stem>/Dockerfile`."""
    dockerfile = Path.cwd() / "environments" / base_stem / "Dockerfile"
    if not dockerfile.is_file():
        raise ValueError(f"no environments/{base_stem}/Dockerfile; pass --repo")
    match = _GITHUB_REPO_URL.search(dockerfile.read_text(encoding="utf-8"))
    if match is None:
        raise ValueError(f"no github.com repo URL in environments/{base_stem}/Dockerfile; pass --repo")
    return match.group(1)


def add_base(
    container: str = typer.Option(..., "--container", help="Full container image reference."),
    base_commit: str = typer.Option(..., "--base-commit", help="40-character SHA-1 of the base state."),
    command: str | None = typer.Option(
        None,
        "--command",
        help="Test command run inside the container. Defaults to the registered command for (repo, base_commit).",
    ),
    test_scope: list[str] = typer.Option(
        [],
        "--test-scope",
        help=(
            "Repo-relative path(s) holding the tests this row evaluates. "
            "Defaults to DEFAULT_TEST_SCOPE_REGISTRY[(repo, base_commit)]. Repeat to add multiple."
        ),
    ),
    repo: str | None = typer.Option(
        None,
        "--repo",
        help="`<owner>/<repo>` short form. Defaults to the github.com URL in environments/<base>/Dockerfile.",
    ),
    instance_id: str | None = typer.Option(
        None,
        "--instance-id",
        help=(
            "The `<repo>__<base_commit>` stem (names the environments dir and the base row's "
            "instance_id, which appends the reserved `base` segment). "
            "Defaults to the stem parsed from --container."
        ),
    ),
    overwrite: bool = typer.Option(
        False,
        "--overwrite",
        help="Replace an existing row with the same instance_id instead of failing.",
    ),
    in_path: str = typer.Option(
        DEFAULT_DATASET_REPO_ID,
        "--in",
        help="Dataset to add into: a local JSONL file or an HF dataset repo id.",
    ),
    push: bool = typer.Option(True, "--push/--no-push", help="Push the dataset to the hub after adding."),
    out_path: str | None = typer.Option(
        None, "--out", help="Write the resulting dataset to this JSONL path. Skipped when omitted."
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip interactive prompts and accept defaults.",
    ),
) -> None:
    """Append a base row (patch=None) built from a Docker environment to the dataset on the Hub."""
    # `base_stem` is the `<repo>__<base_commit>` stem that names the environments dir and the
    # base row's own instance_id (which appends the reserved `base` segment). The three
    # registries are keyed by the `(repo, base_commit)` base key, so `repo` is resolved first.
    base_stem = instance_id or _instance_id_from_container(container)

    if repo is None:
        try:
            derived_repo = _repo_from_environment(base_stem)
        except ValueError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        typer.echo(f"Derived repo from Dockerfile: {derived_repo}")
        if yes or typer.confirm("Use this repo?", default=True):
            repo = derived_repo
        else:
            repo = typer.prompt("Enter `<owner>/<repo>`").strip()
            if not repo:
                typer.echo("error: repo must be non-empty", err=True)
                raise typer.Exit(code=2)

    base_key = (repo, base_commit)

    if command is None:
        try:
            command = DEFAULT_RUN_TESTS_COMMAND_REGISTRY[base_key]
        except KeyError:
            typer.echo(
                f"error: no --command and {base_key!r} not in DEFAULT_RUN_TESTS_COMMAND_REGISTRY",
                err=True,
            )
            raise typer.Exit(code=2) from None

    if not test_scope:
        try:
            test_scope = DEFAULT_TEST_SCOPE_REGISTRY[base_key]
        except KeyError:
            typer.echo(
                f"error: no --test-scope and {base_key!r} not in DEFAULT_TEST_SCOPE_REGISTRY",
                err=True,
            )
            raise typer.Exit(code=2) from None

    try:
        row = StateDatapoint(
            instance_id=f"{base_stem}__base",
            repo=repo,
            base_commit=base_commit,
            container=container,
            command=command,
            test_scope=test_scope,
            patch=None,
        )
    except Exception as exc:
        typer.echo(f"error: invalid StateDatapoint: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    push_repo = push_repo_for(in_path)
    changed = {row.instance_id: row.model_dump()}

    if out_path is not None:
        source_rows, _ = resolve_source(in_path)
        write_jsonl(Path(out_path), upsert_rows(source_rows, changed))
        typer.echo(f"Wrote dataset with {row.instance_id} to {out_path}.")

    if not push:
        typer.echo("Skipping push (--no-push).")
        return

    existing = load_existing_rows(push_repo)
    existed = row.instance_id in {r["instance_id"] for r in existing}
    if existed and not overwrite:
        typer.echo(
            f"error: instance_id {row.instance_id!r} already exists in {push_repo}; pass --overwrite to replace it",
            err=True,
        )
        raise typer.Exit(code=2)

    action = "Replace" if existed else "Add"
    merged = upsert_rows(existing, changed)
    push_rows(
        push_repo,
        merged,
        commit_message=f"{action} base row {row.instance_id}",
        commit_description=commit_description([row.instance_id]),
    )
    typer.echo(f"{action.lower()} base row {row.instance_id} in {push_repo} (total rows: {len(merged)}).")
