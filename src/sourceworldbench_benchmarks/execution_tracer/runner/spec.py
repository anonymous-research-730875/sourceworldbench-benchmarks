"""Tracer specifications.

A `TracerSpec` ties together one of the four tracer source files under
`sourceworldbench_benchmarks.execution_tracer/tracers/` with the env-var prefix the tracer expects
and the canonical name we use to address it from the CLI.

The env-var prefix mirrors the legacy `SWEBENCH_<TOOL>_*` convention
used by the tracer sources themselves; the runner sets those vars at
container-launch time so the tracer source files don't need any
modification (Phase 1).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

_TRACERS_DIR = Path(__file__).resolve().parents[1] / "tracers"

Mode = Literal["pytest", "unittest"]


@dataclass(frozen=True)
class TracerSpec:
    """How to wire one tracer into a containerised run.

    Attributes:
        name: stable identifier exposed on the CLI (`trace`, `walltime`, ...).
        source_file: absolute path on the host to the tracer .py file shipped
            with this package.
        output_filename: the file the tracer writes inside
            `/sourceworldbench-tracing-out/`. Kept identical to the legacy harness so
            already-collected JSONs are still loadable.
        env_prefix: the env-var prefix the tracer source reads
            (e.g. `SWEBENCH_TRACE`, `SWEBENCH_TIMER`).
        mode: the tracer activation mode — `pytest` registers hook impls
            via the plugin shim; `unittest` monkey-patches
            `unittest.TestCase.run` at import time.
        extra_env: tracer-specific knobs (e.g. trace level, memory mode)
            keyed by their full env-var name. Layered on top of the
            standardised `<PREFIX>_OUTPUT` / `<PREFIX>_MODE` / `<PREFIX>_PATHS`.
    """

    name: str
    source_file: Path
    output_filename: str
    env_prefix: str
    mode: Mode = "pytest"
    extra_env: Mapping[str, str] = field(default_factory=dict)

    def env_vars(
        self,
        *,
        output_path_in_container: str,
        repo_dir: str,
        trace_paths: list[str] | None = None,
    ) -> dict[str, str]:
        """Build the env-var dict for one tracer invocation."""
        # Every tracer reads SWEBENCH_REPO_DIR as a colon-separated list of repo
        # roots and tries each in order. Containers where /app is a symlink to
        # /testbed can report either form in `co_filename` (it depends on
        # sys.path ordering during pytest bootstrap), so pass both.
        roots = [d.rstrip("/") for d in repo_dir.split(":") if d.strip()]
        if "/testbed" not in roots:
            roots.append("/testbed")
        env: dict[str, str] = {
            f"{self.env_prefix}_OUTPUT": output_path_in_container,
            f"{self.env_prefix}_MODE": self.mode,
            "SWEBENCH_REPO_DIR": ":".join(roots),
        }
        if trace_paths:
            env[f"{self.env_prefix}_PATHS"] = ",".join(trace_paths)
        env.update(self.extra_env)
        return env


TRACE = TracerSpec(
    name="trace",
    source_file=_TRACERS_DIR / "injectable_tracer.py",
    output_filename="trace_output.json",
    env_prefix="SWEBENCH_TRACE",
)

WALLTIME = TracerSpec(
    name="walltime",
    source_file=_TRACERS_DIR / "test_timer.py",
    output_filename="walltime_output.json",
    env_prefix="SWEBENCH_TIMER",
)

MEMPROF = TracerSpec(
    name="memprof",
    source_file=_TRACERS_DIR / "memory_test_profiler.py",
    output_filename="memprof_output.json",
    env_prefix="SWEBENCH_MEM",
    extra_env={"SWEBENCH_MEM_MEASURE": "traced"},
)

# Same profiler, measuring RSS instead of the tracemalloc heap. A separate unit
# because tracemalloc's bookkeeping roughly doubles the RSS it would measure.
MEMRSS = TracerSpec(
    name="memrss",
    source_file=_TRACERS_DIR / "memory_test_profiler.py",
    output_filename="memrss_output.json",
    env_prefix="SWEBENCH_MEM",
    extra_env={"SWEBENCH_MEM_MEASURE": "rss"},
)

CPROFILE = TracerSpec(
    name="cprofile",
    source_file=_TRACERS_DIR / "cprofile_test_profiler.py",
    output_filename="cprofile_output.json",
    env_prefix="SWEBENCH_PROF",
)


BY_NAME: dict[str, TracerSpec] = {s.name: s for s in (TRACE, WALLTIME, MEMPROF, MEMRSS, CPROFILE)}
