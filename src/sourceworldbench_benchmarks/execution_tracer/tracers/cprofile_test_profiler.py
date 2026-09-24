"""Per-test cProfile-based profiler (function-level time, no memory, no per-line).

Runs `cProfile.Profile()` around every test method. cProfile is implemented in
C and adds ~3-10% overhead in line-mode-free workloads, an order of magnitude
better than `sys.settrace` in line mode. Output is per-function (no per-line)
and contains no memory information by design.

Function naming aligns with `sourceworldbench_benchmarks.execution_tracer/tracer/injectable_tracer.py`'s
format `<module>.<qualname>` so the ground-truth lists match what the
benchmark prompts ask the model to return:
  - module: derived from `co_filename` relative to `SWEBENCH_REPO_DIR`
            (default `/testbed`), with `/` -> `.` and `.py` stripped
  - qualname: `code.co_qualname` (Python 3.11+) falling back to `code.co_name`

Filters to in-project functions only via `SWEBENCH_PROF_PATHS=<dir1>,<dir2>`
(extracted from the gold patch by the harness). All cProfile entries whose
filename starts with a path outside this whitelist are dropped from the
output, exactly matching the existing tracer's scope.

Activation mode is selected by `SWEBENCH_PROF_MODE`:

    SWEBENCH_PROF_MODE=pytest    -> register pytest_runtest_call hook
    SWEBENCH_PROF_MODE=unittest  -> monkeypatch unittest.TestCase.run

Output JSON shape (written atexit + at pytest_sessionfinish):

    {
      "profiler_version": "0.1.0",
      "mode": "pytest" | "unittest",
      "trace_paths": [...],
      "tests": {
        "<test_id>": {
          "wall_time_s": <float>,           # clean perf_counter around the test
          "profile_time_s": <float>,         # cProfile's own measure (with overhead)
          "outcome": <str or null>,
          # The next four appear only under SWEBENCH_PROF_KEEP_STDOUT=1, which
          # the worker sets for `--workload` passes. See `_KEEP_STDOUT`.
          "stdout": <str>,                   # what the test printed (tail)
          "stdout_truncated": <bool>,
          "workload_mean_s": <float>,        # `Mean:` line, if the test printed one
          "workload_stdev_s": <float>,       # `Std Dev:` line, if the test printed one
          "functions": {
             "<module.qualname>": {
               "call_count": <int>,
               "exclusive_time_s": <float>,  # cProfile inlinetime
               "inclusive_time_s": <float>   # cProfile totaltime
             }
          }
        }
      }
    }

Compatible with Python 3.5+ (Django SWE-bench instances use 3.5/3.6).
No type hints, no `from __future__ import annotations`, no f-strings used
inside conditionals or complex expressions that would parse-fail on 3.5.
"""
import atexit
import cProfile
import glob
import inspect
import json
import os
import re
import time

_VERSION = "0.1.0"
_OUT_PATH = os.environ.get(
    "SWEBENCH_PROF_OUTPUT", "/testbed/cprofile_output.json")
_MODE = os.environ.get("SWEBENCH_PROF_MODE", "pytest")
# Set inside a pytest-xdist worker (e.g. "gw0"); absent in the controller and
# in a single-process run. Under xdist the tests run in the workers, so each
# worker writes its own side file and the controller merges them -- otherwise
# the controller (which runs no tests) would clobber the data with an empty result.
_WORKER_ID = os.environ.get("PYTEST_XDIST_WORKER")
# Set by the worker only for `--workload` passes: a suite pass can run 178k
# tests, and holding each one's stdout would dwarf the profile.
_KEEP_STDOUT = os.environ.get("SWEBENCH_PROF_KEEP_STDOUT") == "1"
_STDOUT_TAIL = 16384  # the `timeit` summary is printed last
# Loose like swefficiency's `parse_perf_output` (harness/test_spec.py), except
# anchored: unanchored, `Mean:` also matches inside its own `Before Mean:` lines.
_MEAN_RE = re.compile(r"^[ \t]*Mean:[ \t]*(\S+)", re.M)
_SD_RE = re.compile(r"^[ \t]*Std\s*Dev:[ \t]*(\S+)", re.M)
# Accept colon-separated list of repo roots so that both the symlink path
# (/app) and the resolved path (/testbed) are handled in containers where
# /app -> /testbed but co_filename may use either form depending on sys.path
# ordering during pytest bootstrap.
_raw_repo_dirs = os.environ.get("SWEBENCH_REPO_DIR", "/testbed")
_REPO_DIRS = [d.rstrip("/") for d in _raw_repo_dirs.split(":") if d.strip()]
_TRACE_PATHS = [p for p in os.environ.get("SWEBENCH_PROF_PATHS", "").split(",") if p]

# co_flags constants from inspect (available since 3.6); raw-value fallbacks
# keep the file compatible with Python 3.5.
_CO_OPTIMIZED       = inspect.CO_OPTIMIZED
_CO_GENERATOR       = inspect.CO_GENERATOR
_CO_COROUTINE       = inspect.CO_COROUTINE
_CO_ASYNC_GENERATOR = inspect.CO_ASYNC_GENERATOR

_RESULTS = {}


def _is_in_project(filename):
    """True if `filename` is inside one of the repo roots and within an allowed dir."""
    if not filename or filename.startswith("<"):
        return False
    for repo_dir in _REPO_DIRS:
        if filename.startswith(repo_dir):
            rel = filename[len(repo_dir):].lstrip("/")
            if not _TRACE_PATHS:
                return True
            for d in _TRACE_PATHS:
                if rel.startswith(d):
                    return True
    return False


def _format_func_name(code):
    """Build `<module>.<qualname>` exactly like the injectable_tracer."""
    filename = getattr(code, "co_filename", "") or ""
    name = getattr(code, "co_name", "") or "<unknown>"
    qualname = getattr(code, "co_qualname", name)
    # Module from filename — try each repo root in order
    module = ""
    for repo_dir in _REPO_DIRS:
        if filename.startswith(repo_dir):
            rel = filename[len(repo_dir):].lstrip("/")
            if rel.endswith(".py"):
                module = rel[:-3].replace("/", ".").replace("\\", ".")
            break
    if module:
        return module + "." + qualname
    return qualname


def _rel_filename(filename):
    """Return `filename` relative to the first matching repo root, else the input."""
    for repo_dir in _REPO_DIRS:
        if filename.startswith(repo_dir):
            return filename[len(repo_dir):].lstrip("/")
    return filename


def _profile_to_funcs(p):
    """Convert cProfile.Profile().getstats() to {func_name: dict}.

    Filters out:
      - C built-ins (no code.co_filename attr)
      - Files outside the repo / outside allowed patch dirs
    Aggregates entries that resolve to the same `<module>.<qualname>`
    (rare; happens when the same name is defined more than once).
    """
    out = {}
    for entry in p.getstats():
        code = entry.code
        # Built-ins: code is a string like "<built-in method ...>".
        # Skip — they aren't in the project.
        if not hasattr(code, "co_filename"):
            continue
        if not _is_in_project(code.co_filename):
            continue
        co_name = getattr(code, "co_name", "") or ""
        co_flags = int(getattr(code, "co_flags", 0) or 0)
        rec = {
            "filename": _rel_filename(code.co_filename),
            "firstlineno": int(getattr(code, "co_firstlineno", 0) or 0),
            "qualname": getattr(code, "co_qualname", co_name),
            "name": co_name,
            "co_optimized":       bool(co_flags & _CO_OPTIMIZED),
            "co_generator":       bool(co_flags & _CO_GENERATOR),
            "co_coroutine":       bool(co_flags & _CO_COROUTINE),
            "co_async_generator": bool(co_flags & _CO_ASYNC_GENERATOR),
            "call_count": int(entry.callcount),
            "exclusive_time_s": float(entry.inlinetime),
            "inclusive_time_s": float(entry.totaltime),
        }
        key = _format_func_name(code)
        prev = out.get(key)
        if prev is None:
            out[key] = rec
            continue
        # Two distinct code objects share a `<module.qualname>` string.
        # Disambiguate the new one; keep the first at the plain key.
        if prev["firstlineno"] != rec["firstlineno"] or prev["filename"] != rec["filename"]:
            out[key + "#L" + str(rec["firstlineno"])] = rec
        else:
            prev["call_count"] += rec["call_count"]
            prev["exclusive_time_s"] += rec["exclusive_time_s"]
            prev["inclusive_time_s"] += rec["inclusive_time_s"]
    return out


def _record(test_id, wall_s, profile_s, funcs, outcome):
    prior = _RESULTS.get(test_id)
    # If we see a duplicate test_id (parametrized retries, etc.) keep the
    # longer-running record — matches what test_timer.py does.
    if prior is not None and prior.get("wall_time_s", 0.0) >= wall_s:
        return
    _RESULTS[test_id] = {
        "wall_time_s": wall_s,
        "profile_time_s": profile_s,
        "outcome": outcome,
        "functions": funcs,
    }


def _attach_stdout(test_id, text):
    """Store what the test printed, plus any `timeit` summary parsed out of it.

    A workload prints its own mean and standard deviation, which beat
    `wall_time_s`: they average repeats and exclude the setup around the
    measured call. pytest reprints captured stdout only for failing tests, so on
    a passing row it is lost unless taken off the report. The raw tail is kept
    too, so an unrecognised format stays recoverable without rerunning.
    """
    rec = _RESULTS.get(test_id)
    # First writer wins, so stdout stays with whichever record `_record` kept.
    if rec is None or "stdout" in rec:
        return
    rec["stdout"] = text[-_STDOUT_TAIL:]
    rec["stdout_truncated"] = len(text) > _STDOUT_TAIL
    for key, pattern in (("workload_mean_s", _MEAN_RE), ("workload_stdev_s", _SD_RE)):
        match = pattern.search(text)
        if match is None:
            continue
        try:
            rec[key] = float(match.group(1))
        except ValueError:
            pass  # `stdout` still holds the line as printed


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
            _RESULTS.setdefault(test_id, rec)


def _dump():
    try:
        payload = {
            "profiler_version": _VERSION,
            "mode": _MODE,
            "trace_paths": _TRACE_PATHS,
            "tests": _RESULTS,
        }
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
            p = cProfile.Profile()
            outcome = "passed"
            p.enable()
            try:
                yield
            except BaseException as e:
                outcome = type(e).__name__
                raise
            finally:
                p.disable()
                wall = time.perf_counter() - t0
                # cProfile profile time = sum of inlinetime for every entry.
                try:
                    funcs = _profile_to_funcs(p)
                    total = 0.0
                    for entry in p.getstats():
                        total += float(getattr(entry, "inlinetime", 0.0))
                    _record(item.nodeid, wall, total, funcs, outcome)
                except Exception:
                    pass

        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_makereport(item, call):
            outcome = yield
            report = outcome.get_result()
            # The report is the last place a passing test's stdout is reachable.
            # `getattr` because old pytest versions predate the attribute.
            if _KEEP_STDOUT and report.when == "call":
                _attach_stdout(item.nodeid, getattr(report, "capstdout", "") or "")
            # pytest_runtest_call never fires when a test fails at setup (e.g. a
            # fixture raises, or an import error in the test module). Record such
            # tests with empty functions so callers know the test was attempted.
            if report.when == "setup" and report.failed:
                if item.nodeid not in _RESULTS:
                    _record(item.nodeid, 0.0, 0.0, {}, "setup_error")

        def pytest_sessionfinish(session, exitstatus):
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
        return "%s (%s.%s)" % (
            case._testMethodName, cls.__module__, cls.__name__)

    def _patched_run(self, result=None):
        t0 = time.perf_counter()
        p = cProfile.Profile()
        p.enable()
        try:
            return _orig_run(self, result)
        finally:
            p.disable()
            wall = time.perf_counter() - t0
            try:
                funcs = _profile_to_funcs(p)
                total = 0.0
                for entry in p.getstats():
                    total += float(getattr(entry, "inlinetime", 0.0))
                _record(_django_id(self), wall, total, funcs, None)
            except Exception:
                pass

    _ut.TestCase.run = _patched_run
