"""Bootstrap files copied into the container to activate a tracer.

These files are staged by the runner alongside one of the
`sourceworldbench_benchmarks.execution_tracer/tracers/*.py` files (renamed to `tracer.py`) and
bind-mounted at `/sourceworldbench-tracing/` inside the container. The runner adds
`/sourceworldbench-tracing` to `PYTHONPATH` and sets `PYTEST_PLUGINS=sourceworldbench_tracer_plugin`
so Python's `sitecustomize` mechanism imports the tracer at interpreter
startup and pytest registers the plugin shim during collection.

This package is *not* meant to be imported on the host; the files are
read as source and copied into the bind-mount.
"""

from pathlib import Path

BOOTSTRAP_DIR = Path(__file__).resolve().parent
SITECUSTOMIZE = BOOTSTRAP_DIR / "sitecustomize.py"
PYTEST_PLUGIN = BOOTSTRAP_DIR / "sourceworldbench_tracer_plugin.py"

__all__ = ["BOOTSTRAP_DIR", "SITECUSTOMIZE", "PYTEST_PLUGIN"]
