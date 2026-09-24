"""Thin wrapper around `sourceworldbench_benchmarks.docker_runner.run_state`.

Builds the bind-mount + env arguments for a single tracer invocation
and forwards them. Keeping this as a dedicated seam makes the runner
unit-testable without monkey-patching sourceworldbench_benchmarks internals.
"""

from __future__ import annotations

from pathlib import Path

from sourceworldbench_benchmarks.docker_runner import RunResult, run_state
from sourceworldbench_benchmarks.execution_tracer.runner.script import CONTAINER_BUNDLE_DIR, CONTAINER_OUT_DIR


def run_with_tracer(
    *,
    image: str,
    script: str,
    host_results: Path,
    patch_path: Path | None,
    bundle_dir: Path,
    trace_output_dir: Path,
    timeout: int,
) -> RunResult:
    """Run `script` inside `image` with the tracer bundle and output dir mounted.

    `host_results` keeps the sourceworldbench-benchmarks `/results` mount semantics so
    the user's test command can still drop its parser output there.
    """
    return run_state(
        image=image,
        script=script,
        host_results=host_results,
        patch_path=patch_path,
        timeout=timeout,
        extra_mounts=[
            (bundle_dir, CONTAINER_BUNDLE_DIR, True),
            (trace_output_dir, CONTAINER_OUT_DIR, False),
        ],
    )
