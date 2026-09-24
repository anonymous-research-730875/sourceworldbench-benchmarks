import os
import tempfile
from pathlib import Path
from typing import cast

import typer

from sourceworldbench_benchmarks.execution import CheckoutExecutionBackend, execute_worker_row, execute_worker_row_gcs
from sourceworldbench_benchmarks.execution_tracer.runner.script import TRACE_SCOPES, TraceScope
from sourceworldbench_benchmarks.execution_tracer.runner.spec import BY_NAME
from sourceworldbench_benchmarks.gcs import GcsError, GcsFiles, is_gs_uri
from sourceworldbench_benchmarks.trace_worker import execute_worker_trace, execute_worker_trace_gcs, stage_all_bundles


def execute(
    input_path: str = typer.Option(
        ..., "--input", help="Run input JSONL holding all rows; one is selected by --instance-id."
    ),
    instance_id: str = typer.Option(..., "--instance-id", help="instance_id of the input row to execute."),
    repo_dir: Path = typer.Option(..., "--repo-dir", help="Repository checkout inside the benchmark environment."),
    out_path: str = typer.Option(..., "--out", help="Where to write the filled row (result.json)."),
    artifacts_dir: str = typer.Option(
        ...,
        "--artifacts-dir",
        help="Directory for logs, raw runner output, and failure.json on failure.",
    ),
    timeout: int = typer.Option(7200, "--timeout", help="Seconds per row command."),
) -> None:
    """Worker entrypoint: fill one input row inside its benchmark environment.

    This is what the worker Jobs submitted by `k8s collect` run inside the
    row's environment container; it is not meant to be invoked by hand. It
    selects the row matching --instance-id from the input JSONL, applies the
    row's patch to the checkout at --repo-dir, runs the row command, parses
    the runner output, and writes the filled row to --out. Logs, raw runner
    output, and (on failure) failure.json land under --artifacts-dir.

    --input, --out, and --artifacts-dir are either all local paths or all
    gs:// URIs. With gs:// the worker stages everything locally and uploads
    outputs at the end (result.json/failure.json last, as the completion
    marker `k8s download` looks for).
    """
    gs_flags = [is_gs_uri(value) for value in (input_path, out_path, artifacts_dir)]
    if any(gs_flags) and not all(gs_flags):
        typer.echo("error: --input, --out, and --artifacts-dir must be all gs:// URIs or all local paths", err=True)
        raise typer.Exit(code=2)
    backend = CheckoutExecutionBackend(repo_dir=repo_dir)
    try:
        if all(gs_flags):
            outcome = execute_worker_row_gcs(
                input_uri=input_path,
                instance_id=instance_id,
                backend=backend,
                out_uri=out_path,
                artifacts_uri=artifacts_dir,
                timeout=timeout,
                gcs=GcsFiles(),
            )
        else:
            outcome = execute_worker_row(
                input_path=Path(input_path),
                instance_id=instance_id,
                backend=backend,
                out_path=Path(out_path),
                artifacts_dir=Path(artifacts_dir),
                timeout=timeout,
            )
    except (OSError, GcsError) as exc:
        typer.echo(f"error: cannot write worker outputs (uid={os.getuid()}): {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if not outcome.success:
        typer.echo(f"row failed: {instance_id} reason={outcome.failure_reason}")
        # A row failure is a successful run of the worker protocol — recorded in
        # failure.json, and it must not mark the Kubernetes Job failed
        # (backoffLimit: 0). Harness and infrastructure failures exit 1.
        raise typer.Exit(code=0 if outcome.failure_category == "row" else 1)
    raise typer.Exit(code=0)


def execute_trace(
    input_path: str = typer.Option(
        ..., "--input", help="Run input JSONL holding all rows; one is selected by --instance-id."
    ),
    instance_id: str = typer.Option(..., "--instance-id", help="instance_id of the input row to trace."),
    tracer: str = typer.Option(..., "--tracer", help=f"Which tracer to run: one of {', '.join(BY_NAME)}."),
    repo_dir: Path = typer.Option(..., "--repo-dir", help="Repository checkout inside the benchmark environment."),
    artifacts_dir: str = typer.Option(
        ...,
        "--artifacts-dir",
        help="Directory for the tracer's output and the trace.json completion marker.",
    ),
    timeout: int = typer.Option(7200, "--timeout", help="Seconds for the tracer pass."),
    workload: bool = typer.Option(
        False,
        "--workload",
        help="Use the row's workload column instead of command. Fails if that column is None.",
    ),
    amplified: bool = typer.Option(
        False,
        "--amplified",
        help="With --workload: run command_workload_amplified instead of command_workload.",
    ),
    trace_scope: str = typer.Option(
        "repo",
        "--trace-scope",
        help=(
            "Which part of the repository the tracer records: `repo` (default) covers the whole "
            "repository, identically for every row; `patch` covers only the directories the row's "
            "patch touches, so it varies with the patch."
        ),
    ),
) -> None:
    """Worker entrypoint: run one tracer over one input row inside its environment.

    Runs the row command once with --tracer attached, writes that tracer's
    output plus the trace.json marker (last). Produces one tracer's artifacts,
    not a filled row (no --out); the per-tracer outputs are merged at download.

    --input and --artifacts-dir are either both local paths or both gs:// URIs.
    A pass that produces no output is recorded in the marker but is not fatal;
    only a harness/infrastructure failure exits non-zero.
    """
    if tracer not in BY_NAME:
        typer.echo(f"error: unknown --tracer {tracer!r}; expected one of {', '.join(BY_NAME)}", err=True)
        raise typer.Exit(code=2)
    if trace_scope not in TRACE_SCOPES:
        typer.echo(
            f"error: unknown --trace-scope {trace_scope!r}; expected one of {', '.join(TRACE_SCOPES)}", err=True
        )
        raise typer.Exit(code=2)
    gs_flags = [is_gs_uri(value) for value in (input_path, artifacts_dir)]
    if any(gs_flags) and not all(gs_flags):
        typer.echo("error: --input and --artifacts-dir must be both gs:// URIs or both local paths", err=True)
        raise typer.Exit(code=2)
    if amplified and not workload:
        typer.echo("error: --amplified requires --workload", err=True)
        raise typer.Exit(code=2)
    workload_column = ("command_workload_amplified" if amplified else "command_workload") if workload else None
    try:
        if all(gs_flags):
            outcome = execute_worker_trace_gcs(
                input_uri=input_path,
                instance_id=instance_id,
                tracer=tracer,
                repo_dir=repo_dir,
                artifacts_uri=artifacts_dir,
                timeout=timeout,
                gcs=GcsFiles(),
                workload_column=workload_column,
                trace_scope=cast(TraceScope, trace_scope),
            )
        else:
            with tempfile.TemporaryDirectory(prefix="sourceworldbench_trace_") as tmp:
                outcome = execute_worker_trace(
                    input_path=Path(input_path),
                    instance_id=instance_id,
                    tracer=tracer,
                    repo_dir=repo_dir,
                    artifacts_dir=Path(artifacts_dir),
                    timeout=timeout,
                    tempdir=Path(tmp),
                    workload_column=workload_column,
                    trace_scope=cast(TraceScope, trace_scope),
                )
    except (OSError, GcsError) as exc:
        typer.echo(f"error: cannot trace row (uid={os.getuid()}): {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"traced {instance_id} [{tracer}]: ok={outcome.ok}")
    raise typer.Exit(code=0)


def selfcheck_trace_bundle() -> None:
    """Build-time check: stage every tracer bundle from this binary, then exit.

    Run against the frozen runner binary so a broken `--add-data` wiring fails
    the image build rather than the first in-pod trace run.
    """
    with tempfile.TemporaryDirectory(prefix="sourceworldbench_selfcheck_") as tmp:
        names = stage_all_bundles(Path(tmp))
    typer.echo(f"tracer bundle OK: {', '.join(names)}")
