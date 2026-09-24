"""End-to-end driver: trace one single-state Datapoint with one tracer."""

from __future__ import annotations

import gzip
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from sourceworldbench_benchmarks.docker_runner import DockerError, RunResult
from sourceworldbench_benchmarks.execution_tracer.bootstrap import PYTEST_PLUGIN, SITECUSTOMIZE
from sourceworldbench_benchmarks.execution_tracer.runner.docker import run_with_tracer
from sourceworldbench_benchmarks.execution_tracer.runner.script import build_tracer_script
from sourceworldbench_benchmarks.execution_tracer.runner.spec import TracerSpec
from sourceworldbench_benchmarks.schema import StateDatapoint

logger = logging.getLogger(__name__)


@dataclass
class TraceResult:
    instance_id: str
    tracer: str
    run_result: RunResult
    trace_path: Path | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _stage_bundle(spec: TracerSpec, dest: Path) -> None:
    """Copy bootstrap files + the chosen tracer (renamed `tracer.py`) into `dest`."""
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy(SITECUSTOMIZE, dest / "sitecustomize.py")
    shutil.copy(PYTEST_PLUGIN, dest / "sourceworldbench_tracer_plugin.py")
    shutil.copy(spec.source_file, dest / "tracer.py")


def _collect_trace_artifact(
    *,
    output_dir: Path,
    filename: str,
    dest_dir: Path,
) -> Path | None:
    """Move the tracer's JSON (or `.gz`) out of the run-local output dir.

    Returns the destination path on success, `None` if the tracer
    produced nothing (e.g. the test command failed before any test ran).
    """
    candidates = [output_dir / filename, output_dir / (filename + ".gz")]
    for src in candidates:
        if src.exists():
            dest_dir.mkdir(parents=True, exist_ok=True)
            target = dest_dir / src.name
            shutil.move(str(src), str(target))
            return target
    return None


def trace_one(
    datapoint: StateDatapoint,
    *,
    tracer_spec: TracerSpec,
    out_dir: Path,
    timeout: int,
    repo_dir: str = "/app",
    trace_paths: list[str] | None = None,
    command_override: str | None = None,
) -> TraceResult:
    """Run `datapoint`'s test command with `tracer_spec` enabled, once.

    The trace JSON is written to `out_dir/<instance_id>/<spec.output_filename>[.gz]`.
    The state's `patch` (when present) is staged into a tempdir and
    bind-mounted at the standard sourceworldbench-benchmarks path; the tracer bundle
    is bind-mounted at `/sourceworldbench-tracing/` and the trace-output dir at
    `/sourceworldbench-tracing-out/`.

    `trace_paths` scopes instrumentation to those repo-relative directories.
    The default (`None`, and equivalently `[]`) applies no filter, so the
    whole repo is traced — every caller gets the same scope, whether it runs
    locally or as a cluster worker.
    """
    paths = trace_paths or []

    with tempfile.TemporaryDirectory(prefix="sourceworldbench_trace_") as tmp:
        tmp_dir = Path(tmp)
        bundle_dir = tmp_dir / "tracing"
        _stage_bundle(tracer_spec, bundle_dir)

        patch_path: Path | None = None
        if datapoint.patch is not None:
            patch_path = tmp_dir / "patch.diff"
            patch_path.write_text(datapoint.patch, encoding="utf-8")

        results_dir = tmp_dir / "results"
        trace_dir = tmp_dir / "trace"
        results_dir.mkdir()
        trace_dir.mkdir()
        os.chmod(results_dir, 0o777)
        os.chmod(trace_dir, 0o777)

        script = build_tracer_script(
            datapoint,
            spec=tracer_spec,
            repo_dir=repo_dir,
            trace_paths=paths,
            command_override=command_override,
        )

        try:
            run_result = run_with_tracer(
                image=datapoint.container,
                script=script,
                host_results=results_dir,
                patch_path=patch_path,
                bundle_dir=bundle_dir,
                trace_output_dir=trace_dir,
                timeout=timeout,
            )
        except DockerError as exc:
            logger.warning("trace_one: %s docker error: %s", datapoint.instance_id, exc)
            return TraceResult(
                instance_id=datapoint.instance_id,
                tracer=tracer_spec.name,
                run_result=RunResult(exit_code=-1, stdout=exc.stdout, stderr=exc.stderr),
                trace_path=None,
                error=type(exc).__name__,
            )

        trace_path = _collect_trace_artifact(
            output_dir=trace_dir,
            filename=tracer_spec.output_filename,
            dest_dir=out_dir / datapoint.instance_id,
        )
        return TraceResult(
            instance_id=datapoint.instance_id,
            tracer=tracer_spec.name,
            run_result=run_result,
            trace_path=trace_path,
            error=None if trace_path is not None else "no_trace_output",
        )


def decompress_trace(path: Path) -> Path:
    """Convenience: if `path` is `.gz`, write the decompressed sibling and return it."""
    if path.suffix != ".gz":
        return path
    target = path.with_suffix("")
    with gzip.open(path, "rb") as src, target.open("wb") as dst:
        shutil.copyfileobj(src, dst)
    return target
