"""Pytest plugin shim imported via the PYTEST_PLUGINS env var.

Pytest's plugin discovery imports this module by name and harvests any
top-level `pytest_*` hook implementations from it. Those hooks live in
the sibling `tracer.py`, already imported as `sourceworldbench_tracer` by
`sitecustomize.py` at interpreter startup; we re-export them here so
pytest can find them on this module.

If `sourceworldbench_tracer` isn't loaded (e.g. sitecustomize didn't run because
PYTHONPATH was scrubbed), this module is a harmless no-op rather than a
hard failure.
"""
import sys

_tracer = sys.modules.get("sourceworldbench_tracer")

if _tracer is not None:
    for _name in dir(_tracer):
        if _name.startswith("pytest_"):
            globals()[_name] = getattr(_tracer, _name)


# Force single-process under pytest-xdist so the tracer — which is active only
# in the controller process — actually observes the tests (under `-n auto` the
# tests run in worker subprocesses and the controller writes an empty trace).
# This overrides the *parsed* options, so it beats `-n auto` given on the
# command line (e.g. metricflow), which PYTEST_ADDOPTS=-n0 cannot. `tryfirst`
# runs it before xdist's own pytest_configure; it is a no-op when xdist is not
# installed (the options don't exist). Wraps any tracer-provided pytest_configure.
_chained_pytest_configure = globals().get("pytest_configure")

try:
    import pytest as _pytest
except ImportError:
    _pytest = None

if _pytest is not None:

    @_pytest.hookimpl(tryfirst=True)
    def pytest_configure(config):
        option = config.option
        if hasattr(option, "numprocesses"):
            option.numprocesses = 0
        if hasattr(option, "dist"):
            option.dist = "no"
        if _chained_pytest_configure is not None:
            _chained_pytest_configure(config)
