"""Zero-instrumentation per-test wall-clock recorder.

Times each test with two `time.perf_counter()` calls -- nothing else. No
`sys.settrace`, no `sys.setprofile`, no `sys.monitoring`, no `tracemalloc`.

This file is copied INTO the Docker container and either auto-loaded by
pytest (as `conftest.py`) or imported by `_timer_wrapper.py` (Django).
The mode is selected by `SWEBENCH_TIMER_MODE`:

    SWEBENCH_TIMER_MODE=pytest    -> register pytest_runtest_call hook
    SWEBENCH_TIMER_MODE=unittest  -> monkeypatch unittest.TestCase.run

Output JSON shape (written atexit + at pytest_sessionfinish):

    {
      "timer_version": "0.1.0",
      "mode": "pytest" | "unittest",
      "tests": {
        "<test_id>": {"wall_time_s": <float>, "outcome": <str or null>}
      }
    }

Compatible with Python 3.5+ (Django SWE-bench instances use 3.5/3.6).
No type hints (PEP 604 / `from __future__ import annotations` are 3.7+).
"""
import atexit
import glob
import json
import os
import time

_VERSION = "0.1.0"
_OUT_PATH = os.environ.get(
    "SWEBENCH_TIMER_OUTPUT", "/testbed/walltime_output.json")
_MODE = os.environ.get("SWEBENCH_TIMER_MODE", "pytest")
# Set by pytest-xdist inside a worker process (e.g. "gw0"); absent in the
# controller and in a plain single-process run. Under xdist the tests run in
# the workers, so each worker writes its own side file and the controller
# merges them -- otherwise the controller (which runs no tests) would write an
# empty result and clobber the workers' data.
_WORKER_ID = os.environ.get("PYTEST_XDIST_WORKER")
_RESULTS = {}


def _record(test_id, wall_s, outcome):
    prior = _RESULTS.get(test_id)
    if prior is None or wall_s > prior.get("wall_time_s", 0.0):
        _RESULTS[test_id] = {"wall_time_s": wall_s, "outcome": outcome}


def _output_path():
    return _OUT_PATH + "." + _WORKER_ID if _WORKER_ID else _OUT_PATH


def _merge_worker_outputs():
    """Controller-only: fold each xdist worker's side file into `_RESULTS`."""
    for path in glob.glob(_OUT_PATH + ".*"):
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception:
            continue
        for test_id, rec in (data.get("tests") or {}).items():
            _record(test_id, rec.get("wall_time_s", 0.0), rec.get("outcome"))


def _dump():
    try:
        payload = {"timer_version": _VERSION, "mode": _MODE, "tests": _RESULTS}
        with open(_output_path(), "w") as f:
            json.dump(payload, f)
    except Exception:
        pass


atexit.register(_dump)


if _MODE == "pytest":
    try:
        import pytest

        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_call(item):
            t0 = time.perf_counter()
            outcome = "passed"
            try:
                yield
            except BaseException as e:
                outcome = type(e).__name__
                raise
            finally:
                _record(item.nodeid, time.perf_counter() - t0, outcome)

        def pytest_sessionfinish(session, exitstatus):
            # The controller aggregates the workers' side files; a worker (or a
            # single-process run) just writes its own.
            if not _WORKER_ID:
                _merge_worker_outputs()
            _dump()
    except ImportError:
        pass


elif _MODE == "unittest":
    import unittest as _ut

    _orig_run = _ut.TestCase.run

    def _django_id(case):
        cls = type(case)
        return "%s (%s.%s)" % (case._testMethodName, cls.__module__, cls.__name__)

    def _patched_run(self, result=None):
        t0 = time.perf_counter()
        try:
            return _orig_run(self, result)
        finally:
            _record(_django_id(self), time.perf_counter() - t0, None)

    _ut.TestCase.run = _patched_run
