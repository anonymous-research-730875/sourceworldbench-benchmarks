"""Per-test function-level memory profiler. NO time, NO line-level.

One process measures one quantity, chosen with `SWEBENCH_MEM_MEASURE`:

    traced (default)  Python heap as seen by tracemalloc. Exact for every
                      block that goes through the Python allocator; numpy
                      reports its array buffers to tracemalloc too.
    rss               Resident set size of the process from /proc/self/statm,
                      with the process high-water mark from
                      `getrusage(RUSAGE_SELF).ru_maxrss`. Catches C-extension
                      memory that bypasses the Python allocator. Linux only.

The two are never measured in the same process. tracemalloc keeps a record
per live block in tables that are themselves part of RSS, and the glibc
overhead of those records plus the freed old tables stay resident after the
blocks are gone (measured: 585 MB of extra RSS for 311 MB of tracemalloc
tables, 460 MB of it still resident after every tracked block was freed).
Subtracting `tracemalloc.get_tracemalloc_memory()` recovers only half of it,
so the `rss` measure runs with tracemalloc off, as its own tracer unit
(`memrss`), and the two outputs are combined downstream.

The kernel's RSS high-water mark cannot be reset: the workers run under
gVisor, whose procfs has neither `VmHWM` nor a writable `clear_refs`. Per-call
`rss` peaks therefore come from a sampler. A sidecar process forked at import
reads this process's `/proc/<pid>/statm` in a loop and publishes the largest
value since the last reset in a shared memory page; the tracer resets it at
every segment start (a generation counter) and reads it at every segment end,
exactly like tracemalloc's peak counter. The resolution is one sampling gap
(`SWEBENCH_MEM_RSS_SAMPLE_SLEEP`, default 0.0001 s between reads, 0 = spin at
about 6 us under gVisor for one core): a transient has to stay resident that
long to be seen, so spikes of a few MB can be missed while anything at
hotspot scale is caught. The test-level peak stays `ru_maxrss` when the test
raised the process maximum (`rss_peak_exact` true, which the largest scale of
a workload does), else the sampled peak; the sampled peak is always kept in
`rss_peak_sampled_bytes`, so comparing the two measures the sampler's coverage.

Per-call accounting: every repository-function call is split
into segments, the stretches in which it is the innermost repository frame.
The peak counter is reset at every segment start (the call itself, or a
callee's return) and read at every segment end (a callee's call, or the
return itself), so no segment's high-water mark is lost to a callee's reset.

    inclusive peak  = max over segments of (peak - base), and over callees of
                      (callee base - base + callee inclusive peak)
    exclusive peak  = max over segments of (peak - base - memory retained by
                      the callees returned so far): the function's own
                      high-water mark, callees' transients excluded and their
                      retained memory subtracted
    inclusive delta = current at return - base: memory retained through return
    exclusive delta = inclusive delta - memory retained by callees

Hook: `sys.setprofile` (function-level only, no line events).

Output JSON shape (`<m>` is `traced` or `rss`, `<p>` is `alloc` or `rss`):

    {
      "profiler_version": "0.5.0",
      "measure": "<m>",
      "mode": "pytest" | "unittest",
      "trace_paths": [...],
      "tests": {
        "<test_id>": {
          "start_<m>_bytes":  <int>,
          "peak_<m>_bytes":   <int>,
          "rss_peak_exact":   <bool>,   # rss only
          "rss_peak_sampled_bytes": <int>,  # rss only
          "rss_samples":      <int>,    # rss only: sampler reads during the test
          "outcome":          <str or null>,
          "functions": {
            "<module.qualname>": {
              "filename":            <str>,   # repo-relative
              "firstlineno":         <int>,
              "qualname":            <str>,
              "name":                <str>,
              "co_optimized":        <bool>,
              "co_generator":        <bool>,
              "co_coroutine":        <bool>,
              "co_async_generator":  <bool>,
              "call_count":          <int>,
              "inclusive_<p>_peak_sum":    <int>,
              "inclusive_<p>_peak_max":    <int>,
              "exclusive_<p>_peak_sum":    <int>,
              "exclusive_<p>_peak_max":    <int>,
              "inclusive_<p>_delta_sum":   <int>,
              "inclusive_<p>_delta_max":   <int>,
              "exclusive_<p>_delta_sum":   <int>,
              "exclusive_<p>_delta_max":   <int>
            }
          }
        }
      }
    }

Selected via `SWEBENCH_MEM_MODE`:
    SWEBENCH_MEM_MODE=pytest    -> register pytest_runtest_call hook
    SWEBENCH_MEM_MODE=unittest  -> monkeypatch unittest.TestCase.run

`SWEBENCH_MEM_DUMP_EACH_TEST` (default 1): rewrite the output after every
test so a later kill keeps finished tests; 0 writes only at process exit.

`SWEBENCH_MEM_RSS_SAMPLE_SLEEP` (rss only, default 0.0001): seconds the
sidecar sleeps between two RSS reads; 0 spins and costs one core.

Compatible with Python 3.5+ (Django SWE-bench instances use 3.5/3.6). On
Python < 3.9 (no `tracemalloc.reset_peak`) the per-call `traced` peaks are
unreliable; test-level values are fine.
"""
import atexit
import gc
import glob
import inspect
import json
import mmap
import os
import signal
import struct
import sys
import time
import tracemalloc

# 0.2.0: fix test-level peak tracking. The per-call `reset_peak()` in
# `_profile_cb` clobbered tracemalloc's global peak counter, so by the time
# `_stop_test` read peak it only reflected activity since the last
# project-frame entry — not the high-water mark across the test. Now we
# ratchet the test-level peak (both tracemalloc AND RSS) on every callback
# event, BEFORE any reset, so the global peak is preserved into
# `peak_traced_bytes`.
# 0.2.1: performance — only the tracemalloc ratchet runs on every event
# (~0.8 us per call). RSS is sampled only at project-frame entry/exit
# (where v0.1.0 already read it), so heavy tests with millions of
# `setprofile` events don't pay a `/proc/self/statm` read per event.
# Test-level peak_rss is now ratcheted at those project-frame samples
# instead of only at start/end.
# 0.3.0: per-function identity fields (filename, firstlineno, qualname, name,
# co_* flags) matching the cprofile tracer, with `#L<firstlineno>` suffixes
# disambiguating distinct code objects that share a `<module.qualname>` key.
# Function keys are cached per code object.
# 0.3.1: the output file is rewritten after every test, atomically, instead of
# only at process exit. A process killed by a timeout or the OOM killer during
# a later test (the heavy scale of an amplified workload) keeps the records of
# the tests that had already finished. SWEBENCH_MEM_DUMP_EACH_TEST=0 restores
# the exit-only dump for suites with very many instrumented tests, where
# rewriting the whole result set per test would cost more than it saves.
# 0.4.0: one measure per process (`SWEBENCH_MEM_MEASURE`). RSS moves to its own
# process because tracemalloc's tables inflated it by about twice their size.
# The test-level RSS peak comes from the kernel's high-water mark instead of
# samples at repository-frame boundaries (a 200 MB numpy buffer allocated and
# freed inside one call read as 0.1 MB), and the test starts from a trimmed
# heap so memory freed by an earlier test is not reused invisibly (a test's
# RSS delta read 182 MB instead of 569 MB). Per-call `traced` peaks are
# computed per segment: a parent's own transient was lost when a callee reset
# the peak counter (100 MB read as 0), and the exclusive peak subtracted the
# sum of the callees' peaks (100 MB read as 0 with two 60 MB callees).
# 0.4.1: /proc/self/statm is opened once and sampled with pread at offset 0.
# Opening it per sample cost about 18 us under gVisor, which made memrss
# units run 2-4x longer than memprof on call-heavy rows. The descriptor is
# reopened after fork so a child does not keep reading the parent's RSS.
# 0.5.0: per-call `rss` peaks from a sidecar sampler process (module
# docstring): `inclusive/exclusive_rss_peak_*` per function,
# `rss_peak_sampled_bytes` and `rss_samples` per test.
_VERSION = "0.5.0"
_OUT_PATH = os.environ.get(
    "SWEBENCH_MEM_OUTPUT", "/testbed/memprof_output.json")
_MODE = os.environ.get("SWEBENCH_MEM_MODE", "pytest")
_MEASURE = os.environ.get("SWEBENCH_MEM_MEASURE", "traced")
if _MEASURE not in ("traced", "rss"):
    raise ValueError("SWEBENCH_MEM_MEASURE must be 'traced' or 'rss', got %r" % _MEASURE)
_DUMP_EACH_TEST = os.environ.get("SWEBENCH_MEM_DUMP_EACH_TEST", "1") != "0"
# Set inside a pytest-xdist worker (e.g. "gw0"); absent in the controller and
# in a single-process run. Under xdist the tests run in the workers, so each
# worker writes its own side file and the controller merges them -- otherwise
# the controller (which runs no tests) would clobber the data with an empty result.
_WORKER_ID = os.environ.get("PYTEST_XDIST_WORKER")
# Colon-separated list of repo roots. Containers where /app is a symlink to
# /testbed can report either form in `co_filename` (it depends on sys.path
# ordering during pytest bootstrap), so the harness passes both and every root
# is tried in order.
_REPO_DIRS = [d.rstrip("/") for d in os.environ.get("SWEBENCH_REPO_DIR", "/testbed").split(":") if d.strip()]
_TRACE_PATHS = [p for p in os.environ.get("SWEBENCH_MEM_PATHS", "").split(",") if p]
# Only instrument these tests. Empty set = no filter (every test gets
# instrumented). Critical for matplotlib-class instances where pytest
# discovers ~600 tests in a module but we only need profile data for the
# FAIL_TO_PASS test — instrumenting all 600 multiplies overhead 600x.
def _decode_f2p(s):
    items = [t.replace("%2C", ",") for t in s.split(",") if t]
    return set(items)

_FAIL_TO_PASS = _decode_f2p(os.environ.get("SWEBENCH_MEM_FAIL_TO_PASS", ""))

_START_KEY = "start_%s_bytes" % _MEASURE
_PEAK_KEY = "peak_%s_bytes" % _MEASURE
# Per-function (sum, max) key pairs, in the order the return handler produces
# the values: inclusive peak, exclusive peak, inclusive delta, exclusive
# delta. The `traced` measure keeps its historical `alloc` prefix.
_FIELD_PREFIX = "alloc" if _MEASURE == "traced" else "rss"
_AGG_KEYS = tuple(
    ("%s_%s_%s_sum" % (kind, _FIELD_PREFIX, metric), "%s_%s_%s_max" % (kind, _FIELD_PREFIX, metric))
    for metric in ("peak", "delta")
    for kind in ("inclusive", "exclusive")
)

_RESULTS = {}
_active_test_id = None
_maxrss_at_start = 0
_rss_samples_at_start = 0
# One entry per open repository frame:
# [key, base, own_peak, child_peak, child_delta, self_peak]
#   base        measure at entry
#   own_peak    max over finished segments of (peak - base)
#   child_peak  max over returned callees of (callee base - base + callee inclusive peak)
#   child_delta sum over returned callees of their inclusive delta (memory they retained)
#   self_peak   max over finished segments of (peak - base - child_delta)
_call_stack = []
_BASE, _OWN_PEAK, _CHILD_PEAK, _CHILD_DELTA, _SELF_PEAK = 1, 2, 3, 4, 5


_HAS_RESET_PEAK = hasattr(tracemalloc, "reset_peak")


def _read_traced():
    return tracemalloc.get_traced_memory()


def _reset_traced():
    """Reset tracemalloc peak. Only safe to call PER-CALL on Python 3.9+.

    On older Python the stop()+start() fallback is O(N) where N = currently
    tracked allocations. For matplotlib-class tests (~100K live allocations,
    ~30K in-project function calls), that is catastrophic — the matplotlib
    SWE-bench instances using Python 3.8 timed out at 1200s under per-call
    fallback. We skip the fallback at per-call sites.
    """
    if _HAS_RESET_PEAK:
        tracemalloc.reset_peak()


def _tracemalloc_reset_peak_test_start():
    """Per-TEST reset_peak. Safe on any Python — we do it once per test, so
    even the stop+start fallback is cheap. Used at test boundaries to
    isolate the test's high-water mark from session baseline."""
    if _HAS_RESET_PEAK:
        tracemalloc.reset_peak()
    else:
        if tracemalloc.is_tracing():
            tracemalloc.stop()
            tracemalloc.start(1)


def _get_rss():
    """Current RSS in bytes from /proc/self/statm.

    One pread at offset 0 on a descriptor opened once: the kernel regenerates
    the file on every read from offset 0, and opening it per sample cost
    three syscalls where gVisor makes each one expensive. A raw fd, NOT
    builtin open(): tests that `mock.patch("builtins.open")` and assert its
    call sequence would otherwise record this sampling read and fail only
    under tracing.
    """
    return int(os.pread(_STATM_FD, 128, 0).split()[1]) * _PAGE_SIZE


# Shared page between this process and the sampler sidecar, four 8-byte slots:
# the generation we are in (written here), the generation and the maximum the
# sidecar last published (written there, maximum first, so a matching
# generation guarantees the maximum belongs to it), and its read count.
_SHM_GEN, _SHM_SAMPLE_GEN, _SHM_MAX, _SHM_COUNT = 0, 8, 16, 24
_rss_gen = 0
_sampler_pid = None


def _read_rss():
    """(current, peak since the last reset); the peak is the sidecar's maximum.

    A maximum published for an older generation is stale (the sidecar has not
    seen the reset yet) and is ignored, the current value stands in for it.
    """
    cur = _get_rss()
    if struct.unpack_from("Q", _SHM, _SHM_SAMPLE_GEN)[0] != _rss_gen:
        return cur, cur
    return cur, max(cur, struct.unpack_from("Q", _SHM, _SHM_MAX)[0])


def _reset_rss():
    global _rss_gen
    _rss_gen += 1
    struct.pack_into("Q", _SHM, _SHM_GEN, _rss_gen)


def _rss_sample_count():
    return struct.unpack_from("Q", _SHM, _SHM_COUNT)[0]


def _rss_sampler_loop(parent_pid, sleep):
    """Sidecar body: read the parent's RSS in a loop, publish the max per generation."""
    sys.setprofile(None)
    fd = os.open("/proc/%d/statm" % parent_pid, os.O_RDONLY)
    seen = peak = count = 0
    while True:
        gen = struct.unpack_from("Q", _SHM, _SHM_GEN)[0]
        if gen != seen:
            seen, peak = gen, 0
        try:
            rss = int(os.pread(fd, 128, 0).split()[1]) * _PAGE_SIZE
        except OSError:
            os._exit(0)  # the parent is gone
        if rss > peak:
            peak = rss
            struct.pack_into("Q", _SHM, _SHM_MAX, peak)
            struct.pack_into("Q", _SHM, _SHM_SAMPLE_GEN, seen)
        count += 1
        struct.pack_into("Q", _SHM, _SHM_COUNT, count)
        if sleep:
            time.sleep(sleep)


def _start_rss_sampler():
    global _sampler_pid
    pid = os.fork()
    if pid == 0:
        _rss_sampler_loop(os.getppid(), _RSS_SAMPLE_SLEEP)
    _sampler_pid = pid
    atexit.register(_stop_rss_sampler)


def _stop_rss_sampler():
    if _sampler_pid is None:
        return
    try:
        os.kill(_sampler_pid, signal.SIGTERM)
        os.waitpid(_sampler_pid, 0)
    except OSError:
        pass


def _ru_maxrss():
    """Process high-water mark of RSS in bytes (Linux reports kilobytes)."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


if _MEASURE == "traced":
    _read, _reset = _read_traced, _reset_traced
else:
    import resource
    _PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
    _STATM_FD = os.open("/proc/self/statm", os.O_RDONLY)
    _RSS_SAMPLE_SLEEP = float(os.environ.get("SWEBENCH_MEM_RSS_SAMPLE_SLEEP", "0.0001"))
    _SHM = mmap.mmap(-1, mmap.PAGESIZE)
    _start_rss_sampler()

    def _after_fork_in_child():
        # `/proc/self` was resolved by the parent, so a forked child would
        # keep reading the parent's RSS through the inherited descriptor; and
        # its resets must land on a private page, not on the parent's sampler,
        # which it must not stop at exit either.
        global _STATM_FD, _SHM, _sampler_pid
        os.close(_STATM_FD)
        _STATM_FD = os.open("/proc/self/statm", os.O_RDONLY)
        _SHM = mmap.mmap(-1, mmap.PAGESIZE)
        _sampler_pid = None

    # Registered after the sidecar fork: the hook must not run in the sidecar.
    if hasattr(os, "register_at_fork"):
        os.register_at_fork(after_in_child=_after_fork_in_child)
    _read, _reset = _read_rss, _reset_rss
    try:
        import ctypes
        _malloc_trim = ctypes.CDLL(None).malloc_trim
    except (OSError, AttributeError):
        # Not glibc (e.g. musl): freed memory stays with the allocator, so an
        # earlier test's freed blocks can be reused without moving RSS.
        _malloc_trim = None


def _trim_heap():
    """Give freed memory back to the OS so the test's RSS starts from what is live."""
    gc.collect()
    if _malloc_trim is not None:
        _malloc_trim(0)


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
    filename = getattr(code, "co_filename", "") or ""
    name = getattr(code, "co_name", "") or "<unknown>"
    qualname = getattr(code, "co_qualname", name)
    # Module from filename -- try each repo root in order
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


# Resolved output key per code object, so the hot path formats each function's
# name once. When two distinct code objects share a `<module.qualname>` string,
# the later one gets a `#L<firstlineno>` suffix -- same convention as the
# cprofile tracer.
_KEY_BY_CODE = {}
_CODE_BY_KEY = {}


def _func_key(code):
    key = _KEY_BY_CODE.get(code)
    if key is not None:
        return key
    key = _format_func_name(code)
    owner = _CODE_BY_KEY.get(key)
    if owner is not None and owner is not code:
        key = key + "#L" + str(getattr(code, "co_firstlineno", 0) or 0)
    _CODE_BY_KEY.setdefault(key, code)
    _KEY_BY_CODE[code] = key
    return key


def _new_func_record(code):
    """Identity fields (matching the cprofile tracer) plus zeroed aggregates."""
    name = getattr(code, "co_name", "") or ""
    co_flags = int(getattr(code, "co_flags", 0) or 0)
    rec = {
        "filename": _rel_filename(getattr(code, "co_filename", "") or ""),
        "firstlineno": int(getattr(code, "co_firstlineno", 0) or 0),
        "qualname": getattr(code, "co_qualname", name),
        "name": name,
        "co_optimized": bool(co_flags & inspect.CO_OPTIMIZED),
        "co_generator": bool(co_flags & inspect.CO_GENERATOR),
        "co_coroutine": bool(co_flags & inspect.CO_COROUTINE),
        "co_async_generator": bool(co_flags & inspect.CO_ASYNC_GENERATOR),
        "call_count": 0,
    }
    for sum_key, max_key in _AGG_KEYS:
        rec[sum_key] = 0
        rec[max_key] = 0
    return rec


def _sample(test_rec):
    """Read the measure at a segment end and ratchet the test-level peak.

    Every read happens BEFORE the reset that follows it, so the peak since the
    previous reset is never lost: the test-level peak is the max over all
    segments plus the final read in `_stop_test`.
    """
    cur, peak = _read()
    if peak > test_rec[_PEAK_KEY]:
        test_rec[_PEAK_KEY] = peak
    return cur, peak


def _profile_cb(frame, event, arg):
    if _active_test_id is None:
        return
    code = frame.f_code
    if event == "call":
        if not _is_in_project(code.co_filename):
            return
        cur, peak = _sample(_RESULTS[_active_test_id])
        if _call_stack:
            parent = _call_stack[-1]
            seg = peak - parent[_BASE]
            if seg > parent[_OWN_PEAK]:
                parent[_OWN_PEAK] = seg
            seg -= parent[_CHILD_DELTA]
            if seg > parent[_SELF_PEAK]:
                parent[_SELF_PEAK] = seg
        _reset()
        _call_stack.append([_func_key(code), cur, 0, 0, 0, 0])
        return
    if event == "return":
        if not _call_stack:
            return
        if not _is_in_project(code.co_filename):
            return
        test_rec = _RESULTS[_active_test_id]
        cur, peak = _sample(test_rec)
        name, base, own_peak, child_peak, child_delta, self_peak = _call_stack.pop()
        # For `rss` the peak primitive equals the current value, so the peak
        # terms below are meaningless and are left out of the record.
        seg = peak - base
        if seg > own_peak:
            own_peak = seg
        if seg - child_delta > self_peak:
            self_peak = seg - child_delta
        incl_peak = own_peak if own_peak > child_peak else child_peak
        excl_peak = self_peak if self_peak > 0 else 0
        incl_delta = cur - base
        if incl_delta < 0:
            incl_delta = 0
        excl_delta = incl_delta - child_delta
        if excl_delta < 0:
            excl_delta = 0
        if _call_stack:
            parent = _call_stack[-1]
            via_child = base - parent[_BASE] + incl_peak
            if via_child > parent[_CHILD_PEAK]:
                parent[_CHILD_PEAK] = via_child
            parent[_CHILD_DELTA] += incl_delta
        # The parent's next segment starts here.
        _reset()
        bucket = test_rec["functions"]
        rec = bucket.get(name)
        if rec is None:
            rec = _new_func_record(code)
            bucket[name] = rec
        rec["call_count"] += 1
        values = (incl_peak, excl_peak, incl_delta, excl_delta)
        for (sum_key, max_key), value in zip(_AGG_KEYS, values):
            rec[sum_key] += value
            if value > rec[max_key]:
                rec[max_key] = value


def _start_test(test_id):
    global _active_test_id, _maxrss_at_start, _rss_samples_at_start
    if _MEASURE == "traced":
        if not tracemalloc.is_tracing():
            tracemalloc.start(1)
        _tracemalloc_reset_peak_test_start()
    else:
        _trim_heap()
        _maxrss_at_start = _ru_maxrss()
        _rss_samples_at_start = _rss_sample_count()
        _reset_rss()
    cur, _ = _read()
    _RESULTS[test_id] = {
        _START_KEY: cur,
        _PEAK_KEY: cur,
        "outcome": None,
        "functions": {},
    }
    _active_test_id = test_id
    del _call_stack[:]
    sys.setprofile(_profile_cb)


def _stop_test(test_id, outcome=None):
    global _active_test_id
    sys.setprofile(None)
    if test_id in _RESULTS:
        rec = _RESULTS[test_id]
        _sample(rec)
        if _MEASURE == "rss":
            rec["rss_peak_sampled_bytes"] = rec[_PEAK_KEY]
            rec["rss_samples"] = _rss_sample_count() - _rss_samples_at_start
            maxrss = _ru_maxrss()
            rec["rss_peak_exact"] = maxrss > _maxrss_at_start
            if rec["rss_peak_exact"]:
                rec[_PEAK_KEY] = maxrss
        if outcome is not None:
            rec["outcome"] = outcome
    _active_test_id = None
    del _call_stack[:]
    if _DUMP_EACH_TEST:
        _dump()


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
    """Write `_RESULTS` to the output path, replacing the previous file atomically.

    Called at exit and, unless SWEBENCH_MEM_DUMP_EACH_TEST=0, after every test,
    so a kill during a later test leaves the last complete file, never a
    truncated one. The temp file lives under a
    dotted name so the xdist merge glob (`<out>.*`) never picks it up.
    """
    try:
        payload = {
            "profiler_version": _VERSION,
            "measure": _MEASURE,
            "mode": _MODE,
            "trace_paths": _TRACE_PATHS,
            "tests": _RESULTS,
        }
        out = _output_path()
        tmp = os.path.join(os.path.dirname(out), "." + os.path.basename(out) + ".tmp")
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, out)
    except Exception:
        pass


atexit.register(_dump)


if _MODE == "pytest":
    try:
        import pytest

        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_call(item):
            # Skip non-FAIL_TO_PASS tests entirely (no setprofile installed).
            # This is the big saver on matplotlib-class instances where pytest
            # runs a whole module of ~600 tests but we only need data for the
            # F2P test(s).
            if _FAIL_TO_PASS and item.nodeid not in _FAIL_TO_PASS:
                yield
                return
            _start_test(item.nodeid)
            outcome = "passed"
            try:
                yield
            except BaseException as e:
                outcome = type(e).__name__
                raise
            finally:
                _stop_test(item.nodeid, outcome=outcome)

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
        tid = _django_id(self)
        # Skip non-FAIL_TO_PASS tests entirely.
        if _FAIL_TO_PASS and tid not in _FAIL_TO_PASS:
            return _orig_run(self, result)
        _start_test(tid)
        try:
            return _orig_run(self, result)
        finally:
            _stop_test(tid, outcome=None)

    _ut.TestCase.run = _patched_run
