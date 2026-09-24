"""
injectable_tracer.py — Zero-dependency tracer injected into SWE-bench Docker containers.

v0.3 — adds tracemalloc-based allocation tracking, test outcome capture
(pytest), and per-line allocation attribution.

Collects:

  - Function-level: entry/exit with wall + exclusive time, call depth, RSS,
    and (when enabled) tracemalloc allocation deltas.
  - Line-level: per-line hits, time, and (when enabled) per-line allocation.
  - Test outcome: pass/fail/error/skipped with exception type, message,
    failure file/line, and phase (setup/call/teardown). Pytest only.
  - Memory: RSS at function boundaries via /proc/self/statm AND
    tracemalloc.get_traced_memory() for Python-heap allocation tracking.

Three tracing backends are available:
  - setprofile: sys.setprofile (function-level only, Python 3.6+)
  - settrace:   sys.settrace (function + line-level, Python 3.6+)
  - sysmon:     sys.monitoring / PEP 669 (Python 3.12+, lower overhead)

The tracer writes JSON (or gzip-compressed JSON) to a file.
It does NOT write to stdout/stderr to avoid interfering with test output parsing.

Configuration via environment variables:
  SWEBENCH_TRACE_ENABLED     = "1" to enable (default: "1")
  SWEBENCH_TRACE_LEVEL       = "function" | "line" (default: "function")
  SWEBENCH_TRACE_MEMORY      = "rss" | "tracemalloc" | "both" (default: "rss")
  SWEBENCH_TRACE_OUTPUT      = path to output file (default: /testbed/trace_output.json)
  SWEBENCH_TRACE_PATHS       = comma-separated path prefixes relative to REPO_DIR
  SWEBENCH_TRACE_MODULES     = comma-separated module prefixes to trace
  SWEBENCH_REPO_DIR          = repo root(s) inside container, colon-separated (default: /testbed)
  SWEBENCH_TRACE_COMPRESS    = "1" to enable gzip (default: "0")
  SWEBENCH_TRACE_BACKEND     = "auto"|"setprofile"|"settrace"|"sysmon" (default: "auto")
  SWEBENCH_TRACE_MAX_EVENTS  = max events per test (default: 5000000)
  SWEBENCH_TRACE_MAX_DEPTH   = max call depth (default: 200)
  SWEBENCH_TRACE_MAX_FUNCTIONS = max distinct functions (default: 10000)
  SWEBENCH_TRACE_MAX_DURATION  = max wall-clock seconds per test (default: 300)
  SWEBENCH_TRACE_MAX_CALL_SEQ = max call_sequence entries in output (default: 1000)
  SWEBENCH_TRACE_DJANGO      = "1" for Django/sympy whole-session mode
  SWEBENCH_TRACE_INSTANCE_ID = SWE-bench instance id (recorded in output)
"""

import json
import os
import sys
import time
from collections import defaultdict

try:
    import tracemalloc
    _HAS_TRACEMALLOC = True
except ImportError:
    _HAS_TRACEMALLOC = False

# ---------------------------------------------------------------------------
#  Python 3.6 compatibility
# ---------------------------------------------------------------------------

if hasattr(time, "perf_counter_ns"):
    perf_counter_ns = time.perf_counter_ns
else:
    def perf_counter_ns():
        return int(time.perf_counter() * 1000000000)

_HAS_SYS_MONITORING = hasattr(sys, "monitoring")


# ---------------------------------------------------------------------------
#  TraceConfig — all env-var configuration in one place
# ---------------------------------------------------------------------------

class TraceConfig(object):
    """Configuration for the tracer, read from environment variables."""

    def __init__(self,
                 enabled=None,
                 trace_level=None,
                 memory_tracking=None,
                 output_path=None,
                 trace_paths=None,
                 module_prefixes=None,
                 repo_dir=None,
                 compress=None,
                 backend=None,
                 max_events=None,
                 max_depth=None,
                 max_functions=None,
                 max_duration=None,
                 max_call_seq=None,
                 instance_id=None):
        self.enabled = enabled if enabled is not None else (os.environ.get("SWEBENCH_TRACE_ENABLED", "1") == "1")
        self.trace_level = trace_level or os.environ.get("SWEBENCH_TRACE_LEVEL", "function")
        self.memory_tracking = memory_tracking or os.environ.get("SWEBENCH_TRACE_MEMORY", "rss")
        self.output_path = output_path or os.environ.get("SWEBENCH_TRACE_OUTPUT", "/testbed/trace_output.json")
        # SWEBENCH_REPO_DIR may carry a colon-separated list of roots: containers
        # where /app is a symlink to /testbed can report either form in
        # `co_filename` (it depends on sys.path ordering during pytest
        # bootstrap), so both are passed and every root is tried in order.
        # `repo_dir` stays the first root, for callers that want a single path.
        raw_repo_dirs = repo_dir or os.environ.get("SWEBENCH_REPO_DIR", "/testbed")
        self.repo_dirs = [d.rstrip("/") for d in raw_repo_dirs.split(":") if d.strip()] or ["/testbed"]
        self.repo_dir = self.repo_dirs[0]
        self.compress = compress if compress is not None else (os.environ.get("SWEBENCH_TRACE_COMPRESS", "0") == "1")
        self.backend = backend or os.environ.get("SWEBENCH_TRACE_BACKEND", "auto")
        self.instance_id = instance_id or os.environ.get("SWEBENCH_TRACE_INSTANCE_ID", "")
        # tracemalloc only works if the runtime supports it
        if self.memory_tracking in ("tracemalloc", "both") and not _HAS_TRACEMALLOC:
            self.memory_tracking = "rss"

        if trace_paths is not None:
            self.trace_paths = trace_paths
        else:
            raw = os.environ.get("SWEBENCH_TRACE_PATHS", "")
            self.trace_paths = [p.strip() for p in raw.split(",") if p.strip()]

        if module_prefixes is not None:
            self.module_prefixes = module_prefixes
        else:
            raw = os.environ.get("SWEBENCH_TRACE_MODULES", "")
            self.module_prefixes = [p.strip() for p in raw.split(",") if p.strip()]

        self.max_events = (
            max_events
            if max_events is not None
            else _safe_int(os.environ.get("SWEBENCH_TRACE_MAX_EVENTS"), 5000000)
        )
        self.max_depth = (
            max_depth
            if max_depth is not None
            else _safe_int(os.environ.get("SWEBENCH_TRACE_MAX_DEPTH"), 200)
        )
        self.max_functions = (
            max_functions
            if max_functions is not None
            else _safe_int(os.environ.get("SWEBENCH_TRACE_MAX_FUNCTIONS"), 10000)
        )
        self.max_duration = (
            max_duration
            if max_duration is not None
            else _safe_float(os.environ.get("SWEBENCH_TRACE_MAX_DURATION"), 300.0)
        )
        self.max_call_seq = (
            max_call_seq
            if max_call_seq is not None
            else _safe_int(os.environ.get("SWEBENCH_TRACE_MAX_CALL_SEQ"), 1000)
        )

        # Session-mode (Django/sympy) test-boundary detection. When True,
        # the tracer watches for calls into in-project functions whose
        # unqualified name starts with "test_" and routes events into
        # per-test buckets until that frame returns. The output's `tests`
        # dict then contains one record per detected test method,
        # matching the pytest-mode shape.
        self.session_mode_test_detect = (
            os.environ.get("SWEBENCH_TRACE_SESSION_TEST_DETECT", "0") == "1"
        )
        # test_id format for session-mode detected tests:
        #   "django" -> "test_method (module.ClassName)"  (Django runtests format)
        #   "pytest" -> "<rel_path>::<qualname>"          (default, sympy)
        self.test_id_format = (
            os.environ.get("SWEBENCH_TRACE_TEST_ID_FORMAT", "pytest")
        )


def _safe_int(val, default):
    if val is None:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _safe_float(val, default):
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
#  RSS helper (Linux only, via /proc/self/statm)
# ---------------------------------------------------------------------------

_PAGE_SIZE = None


def _get_page_size():
    global _PAGE_SIZE
    if _PAGE_SIZE is None:
        try:
            _PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
        except (AttributeError, ValueError):
            _PAGE_SIZE = 4096
    return _PAGE_SIZE


_HAS_RESOURCE = False
try:
    import resource as _resource
    _HAS_RESOURCE = True
except ImportError:
    pass


# Detect macOS/BSD once so we can convert ru_maxrss correctly.
_IS_DARWIN = sys.platform == "darwin"


def get_rss_bytes():
    """Return current RSS in bytes.

    Fast path on Linux: /proc/self/statm (resident pages × page size).
    Fallback: resource.getrusage(RUSAGE_SELF).ru_maxrss. This is the
    process's PEAK RSS to-date, not the current value — so on Mac/BSD
    the per-call delta becomes a high-water-mark delta (still useful
    for hotspot ordering; it just never decreases). On Linux without
    /proc, ru_maxrss is in kilobytes; on Mac it's in bytes.
    """
    try:
        # Read via a raw fd (os.open/os.read), NOT builtin open(): tests that
        # `mock.patch("builtins.open")` and assert its call sequence would
        # otherwise record this sampling read and fail only under tracing.
        fd = os.open("/proc/self/statm", os.O_RDONLY)
        try:
            parts = os.read(fd, 128).split()
        finally:
            os.close(fd)
        return int(parts[1]) * _get_page_size()
    except (OSError, IndexError, ValueError):
        pass
    if _HAS_RESOURCE:
        try:
            ru = _resource.getrusage(_resource.RUSAGE_SELF).ru_maxrss
            return int(ru) if _IS_DARWIN else int(ru) * 1024
        except (OSError, ValueError):
            return 0
    return 0


# ---------------------------------------------------------------------------
#  Tracemalloc helpers (Python heap allocation tracking)
# ---------------------------------------------------------------------------

def tracemalloc_enabled(config):
    """True if tracemalloc-based tracking is requested AND available."""
    return _HAS_TRACEMALLOC and config.memory_tracking in ("tracemalloc", "both")


def tracemalloc_ensure_started():
    """Start tracemalloc if it is not already running. Cheap no-op otherwise."""
    if _HAS_TRACEMALLOC and not tracemalloc.is_tracing():
        # nframe=1 keeps overhead low; we don't need full tracebacks for hotspot data.
        tracemalloc.start(1)


def tracemalloc_reset_peak():
    """Reset tracemalloc peak to current usage. Compatible with Python 3.5+."""
    if not _HAS_TRACEMALLOC:
        return
    if hasattr(tracemalloc, "reset_peak"):
        tracemalloc.reset_peak()
    else:
        # On Python < 3.9 reset_peak was unavailable; restart to clear peak.
        try:
            tracemalloc.stop()
        except Exception:
            pass
        try:
            tracemalloc.start(1)
        except Exception:
            pass


def tracemalloc_get():
    """Return (current_bytes, peak_bytes) or (0, 0) if not running."""
    if _HAS_TRACEMALLOC and tracemalloc.is_tracing():
        try:
            return tracemalloc.get_traced_memory()
        except Exception:
            return 0, 0
    return 0, 0


# ---------------------------------------------------------------------------
#  Path filtering
# ---------------------------------------------------------------------------

_SKIP_BASENAMES = frozenset(("conftest.py", "_swebench_tracer.py", "_trace_wrapper.py"))


def _should_trace_file(filename, config):
    """Check if a file should be traced based on configuration."""
    if not filename:
        return False
    if "site-packages" in filename:
        return False
    if filename.startswith("<"):
        return False
    basename = os.path.basename(filename)
    if basename in _SKIP_BASENAMES:
        return False
    for repo_dir in config.repo_dirs:
        if not filename.startswith(repo_dir):
            continue
        if not config.trace_paths:
            return True
        rel = filename[len(repo_dir):].lstrip("/")
        if any(rel.startswith(p) for p in config.trace_paths):
            return True
    return False


def _rel_path(filename, config):
    """Get path relative to the first matching repo root."""
    for repo_dir in config.repo_dirs:
        if filename.startswith(repo_dir):
            return filename[len(repo_dir):].lstrip("/")
    return filename


# ---------------------------------------------------------------------------
#  Backend base class
# ---------------------------------------------------------------------------

class _BaseBackend(object):
    """Base class for tracing backends."""

    name = "base"

    def __init__(self, collector, config):
        self.collector = collector
        self.config = config

    def install(self):
        raise NotImplementedError

    def uninstall(self):
        raise NotImplementedError


# ---------------------------------------------------------------------------
#  SetprofileBackend — function-level via sys.setprofile
# ---------------------------------------------------------------------------

class _SetprofileBackend(_BaseBackend):
    """Function-level tracing via sys.setprofile."""

    name = "setprofile"

    def _callback(self, frame, event, arg):
        c = self.collector
        cfg = self.config

        if c._stopped:
            return

        # Check duration limit
        if cfg.max_duration > 0:
            elapsed = time.perf_counter() - c.start_time
            if elapsed >= cfg.max_duration:
                c._record_trim("max_duration", cfg.max_duration, 0)
                c._stopped = True
                return

        if c.event_count >= cfg.max_events:
            if not c._events_trimmed:
                c._record_trim("max_events", cfg.max_events, 0)
                c._events_trimmed = True
            return

        filename = frame.f_code.co_filename
        if not _should_trace_file(filename, cfg):
            return

        if event == "call":
            if c._depth >= cfg.max_depth:
                if not c._depth_trimmed:
                    c._record_trim("max_depth", cfg.max_depth, 0)
                    c._depth_trimmed = True
                c._depth += 1
                return

            func_name = frame.f_code.co_name
            module = frame.f_globals.get("__name__", "")
            qualname = getattr(frame.f_code, "co_qualname", func_name)
            func_key = "%s.%s" % (module, qualname)

            # Check max_functions limit
            if func_key not in c._seen_functions:
                if len(c._seen_functions) >= cfg.max_functions:
                    if not c._functions_trimmed:
                        c._record_trim("max_functions", cfg.max_functions, 0)
                        c._functions_trimmed = True
                    c._depth += 1
                    return
                c._seen_functions.add(func_key)

            rss = get_rss_bytes()
            if rss > c.peak_rss:
                c.peak_rss = rss
            alloc = tracemalloc_get()[0] if c._tracemalloc_active else 0

            c._call_stack.append([
                func_key,
                perf_counter_ns(),
                rss,
                c._depth,
                0,        # children_ns
                alloc,    # start_alloc
                0,        # children_alloc
                0,        # children_rss
            ])
            c._depth += 1
            c._maybe_enter_test(func_name, qualname, module,
                                filename, c._depth - 1, frame=frame)

        elif event == "return":
            c._depth -= 1
            if c._depth < 0:
                c._depth = 0

            if c._call_stack and c._call_stack[-1][3] == c._depth:
                entry = c._call_stack.pop()
                func_key, start_ns, start_rss, depth, children_ns, start_alloc, children_alloc, children_rss = entry
                end_ns = perf_counter_ns()
                end_rss = get_rss_bytes()
                if end_rss > c.peak_rss:
                    c.peak_rss = end_rss
                end_alloc = tracemalloc_get()[0] if c._tracemalloc_active else 0

                inclusive_ns = end_ns - start_ns
                exclusive_ns = max(0, inclusive_ns - children_ns)
                inclusive_alloc = max(0, end_alloc - start_alloc) if c._tracemalloc_active else 0
                exclusive_alloc = max(0, inclusive_alloc - children_alloc)
                inclusive_rss = max(0, end_rss - start_rss)
                exclusive_rss = max(0, inclusive_rss - children_rss)

                if c._call_stack:
                    c._call_stack[-1][4] += inclusive_ns
                    c._call_stack[-1][6] += inclusive_alloc
                    c._call_stack[-1][7] += inclusive_rss

                c.call_events.append({
                    "func": func_key,
                    "file": _rel_path(filename, cfg),
                    "lineno": frame.f_code.co_firstlineno,
                    "depth": depth,
                    "wall_ns": inclusive_ns,
                    "exclusive_ns": exclusive_ns,
                    "rss_before": start_rss,
                    "rss_after": end_rss,
                    "alloc_inclusive": inclusive_alloc,
                    "alloc_exclusive": exclusive_alloc,
                    "rss_inclusive": inclusive_rss,
                    "rss_exclusive": exclusive_rss,
                })
                c.event_count += 1
            # Test-boundary check AFTER the call_event is appended so the
            # test method's own event lands in its own bucket. depth here
            # is the depth at which the returning frame ran (post-decrement).
            c._exit_test_if_matching(c._depth)

    def install(self):
        sys.setprofile(self._callback)

    def uninstall(self):
        sys.setprofile(None)


# ---------------------------------------------------------------------------
#  SettraceBackend — function + line level via sys.settrace
# ---------------------------------------------------------------------------

class _SettraceBackend(_BaseBackend):
    """Function and line-level tracing via sys.settrace."""

    name = "settrace"

    def _callback(self, frame, event, arg):
        c = self.collector
        cfg = self.config

        if c._stopped:
            return None

        # Check duration limit
        if cfg.max_duration > 0:
            elapsed = time.perf_counter() - c.start_time
            if elapsed >= cfg.max_duration:
                c._record_trim("max_duration", cfg.max_duration, 0)
                c._stopped = True
                return None

        filename = frame.f_code.co_filename

        if event == "call":
            if not _should_trace_file(filename, cfg):
                return None

            # Close the caller's current per-line accumulator BEFORE entering
            # the callee, so the callee's frame-prologue + own runtime is
            # not double-counted against (caller_file, caller_lineno).
            # After this, the line anchor is invalidated; the next line
            # event (the callee's first line, or back in the caller after
            # return) will re-anchor without accumulating.
            if cfg.trace_level == "line" and c._last_line_file is not None \
                    and c._last_line_time_ns > 0:
                now_ns_call = perf_counter_ns()
                delta = now_ns_call - c._last_line_time_ns
                c.line_time_ns[c._last_line_file][c._last_line_no] += delta
                if c._tracemalloc_active:
                    now_alloc_call = tracemalloc_get()[0]
                    a_delta = now_alloc_call - c._last_line_alloc
                    if a_delta > 0:
                        c.line_alloc_bytes[c._last_line_file][c._last_line_no] += a_delta
                c._last_line_time_ns = 0

            if c._depth < cfg.max_depth and c.event_count < cfg.max_events:
                func_name = frame.f_code.co_name
                module = frame.f_globals.get("__name__", "")
                qualname = getattr(frame.f_code, "co_qualname", func_name)
                func_key = "%s.%s" % (module, qualname)

                # Check max_functions limit
                if func_key not in c._seen_functions:
                    if len(c._seen_functions) >= cfg.max_functions:
                        if not c._functions_trimmed:
                            c._record_trim("max_functions", cfg.max_functions, 0)
                            c._functions_trimmed = True
                        c._depth += 1
                        return self._callback
                    c._seen_functions.add(func_key)

                rss = get_rss_bytes()
                if rss > c.peak_rss:
                    c.peak_rss = rss
                alloc = tracemalloc_get()[0] if c._tracemalloc_active else 0

                c._call_stack.append([
                    func_key,
                    perf_counter_ns(),
                    rss,
                    c._depth,
                    0,        # children_ns
                    alloc,    # start_alloc
                    0,        # children_alloc
                    0,        # children_rss
                ])
                pushed_test_check = True
            elif c._depth >= cfg.max_depth and not c._depth_trimmed:
                c._record_trim("max_depth", cfg.max_depth, 0)
                c._depth_trimmed = True
                pushed_test_check = False
            elif c.event_count >= cfg.max_events and not c._events_trimmed:
                c._record_trim("max_events", cfg.max_events, 0)
                c._events_trimmed = True
                pushed_test_check = False
            else:
                pushed_test_check = False

            c._depth += 1
            if pushed_test_check:
                c._maybe_enter_test(func_name, qualname, module,
                                    filename, c._depth - 1, frame=frame)
            return self._callback

        if event == "line":
            if not _should_trace_file(filename, cfg):
                return self._callback

            now_ns = perf_counter_ns()
            now_alloc = tracemalloc_get()[0] if c._tracemalloc_active else 0
            # NOTE: We deliberately do NOT read RSS in line events.
            # /proc/self/statm reads at every line event cost ~10-20µs each
            # and accumulate to 100s+ of seconds on tests with millions of
            # line events (e.g. sympy). Per-line RSS at 4 KB page granularity
            # was noisy anyway; per-method RSS at call/return boundaries is
            # already attributed and captures large allocations.
            lineno = frame.f_lineno
            rel_filename = _rel_path(filename, cfg)

            c.line_hits[rel_filename][lineno] += 1

            if c._last_line_file is not None and c._last_line_time_ns > 0:
                delta = now_ns - c._last_line_time_ns
                c.line_time_ns[c._last_line_file][c._last_line_no] += delta
                if c._tracemalloc_active:
                    a_delta = now_alloc - c._last_line_alloc
                    if a_delta > 0:
                        c.line_alloc_bytes[c._last_line_file][c._last_line_no] += a_delta

            c._last_line_time_ns = now_ns
            c._last_line_alloc = now_alloc
            c._last_line_file = rel_filename
            c._last_line_no = lineno

            c.event_count += 1
            if c.event_count >= cfg.max_events:
                if not c._events_trimmed:
                    c._record_trim("max_events", cfg.max_events, 0)
                    c._events_trimmed = True
                return None

        elif event == "return":
            if c._last_line_file is not None and c._last_line_time_ns > 0:
                now_ns = perf_counter_ns()
                now_alloc = tracemalloc_get()[0] if c._tracemalloc_active else 0
                delta = now_ns - c._last_line_time_ns
                c.line_time_ns[c._last_line_file][c._last_line_no] += delta
                if c._tracemalloc_active:
                    a_delta = now_alloc - c._last_line_alloc
                    if a_delta > 0:
                        c.line_alloc_bytes[c._last_line_file][c._last_line_no] += a_delta
            # Invalidate the line anchor so post-return latency until the
            # caller's next line event is not charged to this frame's last
            # line. The caller's next line event will re-anchor cleanly.
            c._last_line_time_ns = 0

            c._depth -= 1
            if c._depth < 0:
                c._depth = 0

            if c._call_stack and c._call_stack[-1][3] == c._depth:
                entry = c._call_stack.pop()
                func_key, start_ns, start_rss, depth, children_ns, start_alloc, children_alloc, children_rss = entry
                end_ns = perf_counter_ns()
                end_rss = get_rss_bytes()
                if end_rss > c.peak_rss:
                    c.peak_rss = end_rss
                end_alloc = tracemalloc_get()[0] if c._tracemalloc_active else 0

                inclusive_ns = end_ns - start_ns
                exclusive_ns = max(0, inclusive_ns - children_ns)
                inclusive_alloc = max(0, end_alloc - start_alloc) if c._tracemalloc_active else 0
                exclusive_alloc = max(0, inclusive_alloc - children_alloc)
                inclusive_rss = max(0, end_rss - start_rss)
                exclusive_rss = max(0, inclusive_rss - children_rss)

                if c._call_stack:
                    c._call_stack[-1][4] += inclusive_ns
                    c._call_stack[-1][6] += inclusive_alloc
                    c._call_stack[-1][7] += inclusive_rss

                c.call_events.append({
                    "func": func_key,
                    "file": _rel_path(filename, cfg),
                    "lineno": frame.f_code.co_firstlineno,
                    "depth": depth,
                    "wall_ns": inclusive_ns,
                    "exclusive_ns": exclusive_ns,
                    "rss_before": start_rss,
                    "rss_after": end_rss,
                    "alloc_inclusive": inclusive_alloc,
                    "alloc_exclusive": exclusive_alloc,
                    "rss_inclusive": inclusive_rss,
                    "rss_exclusive": exclusive_rss,
                })
                c.event_count += 1
            # Test-boundary check AFTER the call_event lands, so the test
            # method's own event ends up in its own bucket.
            c._exit_test_if_matching(c._depth)

        return self._callback

    def install(self):
        sys.settrace(self._callback)

    def uninstall(self):
        sys.settrace(None)


# ---------------------------------------------------------------------------
#  SysMonBackend — Python 3.12+ sys.monitoring / PEP 669
# ---------------------------------------------------------------------------

if _HAS_SYS_MONITORING:

    class _SysMonBackend(_BaseBackend):
        """Tracing via sys.monitoring (PEP 669, Python 3.12+).

        Uses PY_START/PY_RETURN events (not CALL) so that the code object
        passed to callbacks is the callee's code, not the caller's.
        LINE events are enabled per-code-object via set_local_events()
        to avoid tracing non-project code.
        """

        name = "sysmon"
        _TOOL_ID = sys.monitoring.DEBUGGER_ID

        def __init__(self, collector, config):
            _BaseBackend.__init__(self, collector, config)
            self._traced_codes = set()
            self._skipped_codes = set()

        def _should_trace_code(self, code):
            if code in self._traced_codes:
                return True
            if code in self._skipped_codes:
                return False
            if _should_trace_file(code.co_filename, self.config):
                self._traced_codes.add(code)
                return True
            else:
                self._skipped_codes.add(code)
                return False

        def _on_py_start(self, code, instruction_offset):
            """Called when entering a Python function."""
            c = self.collector
            cfg = self.config

            if c._stopped:
                return sys.monitoring.DISABLE

            if cfg.max_duration > 0:
                elapsed = time.perf_counter() - c.start_time
                if elapsed >= cfg.max_duration:
                    c._record_trim("max_duration", cfg.max_duration, 0)
                    c._stopped = True
                    return sys.monitoring.DISABLE

            if not self._should_trace_code(code):
                return sys.monitoring.DISABLE

            # Close the caller's current per-line accumulator BEFORE
            # entering the callee, so the callee's frame-prologue + own
            # runtime is not double-counted against (caller_file, caller_lineno).
            if cfg.trace_level == "line" and c._last_line_file is not None \
                    and c._last_line_time_ns > 0:
                now_ns_call = perf_counter_ns()
                delta = now_ns_call - c._last_line_time_ns
                c.line_time_ns[c._last_line_file][c._last_line_no] += delta
                if c._tracemalloc_active:
                    now_alloc_call = tracemalloc_get()[0]
                    a_delta = now_alloc_call - c._last_line_alloc
                    if a_delta > 0:
                        c.line_alloc_bytes[c._last_line_file][c._last_line_no] += a_delta
                c._last_line_time_ns = 0

            if c.event_count >= cfg.max_events:
                if not c._events_trimmed:
                    c._record_trim("max_events", cfg.max_events, 0)
                    c._events_trimmed = True
                return sys.monitoring.DISABLE

            if c._depth >= cfg.max_depth:
                if not c._depth_trimmed:
                    c._record_trim("max_depth", cfg.max_depth, 0)
                    c._depth_trimmed = True
                # Increment depth without pushing to call_stack; the
                # corresponding PY_RETURN will decrement depth and find
                # no matching stack entry, so no event is recorded.
                c._depth += 1
                return

            func_name = code.co_name
            qualname = getattr(code, "co_qualname", func_name)
            # Get module name via frame globals to match setprofile/settrace
            module = ""
            callee_frame = None
            try:
                frame = sys._getframe(0)
                # Walk up to find the frame whose code matches (the callee)
                # PY_START fires inside the callee, so the current frame
                # chain includes it. We look for __name__ in the frame
                # whose code object matches.
                f = frame
                while f is not None:
                    if f.f_code is code:
                        module = f.f_globals.get("__name__", "")
                        callee_frame = f
                        break
                    f = f.f_back
            except (AttributeError, ValueError):
                pass
            if not module:
                # Fallback: derive from filename
                filename = code.co_filename
                for repo_dir in cfg.repo_dirs:
                    if filename.startswith(repo_dir):
                        rel = filename[len(repo_dir):].lstrip("/")
                        if rel.endswith(".py"):
                            module = rel[:-3].replace("/", ".").replace("\\", ".")
                        break
            func_key = "%s.%s" % (module, qualname)

            if func_key not in c._seen_functions:
                if len(c._seen_functions) >= cfg.max_functions:
                    if not c._functions_trimmed:
                        c._record_trim("max_functions", cfg.max_functions, 0)
                        c._functions_trimmed = True
                    c._depth += 1
                    return
                c._seen_functions.add(func_key)

            rss = get_rss_bytes()
            if rss > c.peak_rss:
                c.peak_rss = rss
            alloc = tracemalloc_get()[0] if c._tracemalloc_active else 0

            c._call_stack.append([
                func_key,
                perf_counter_ns(),
                rss,
                c._depth,
                0,        # children_ns
                alloc,    # start_alloc
                0,        # children_alloc
                0,        # children_rss
            ])
            c._depth += 1
            c._maybe_enter_test(func_name, qualname, module,
                                code.co_filename, c._depth - 1,
                                frame=callee_frame)

            # Enable LINE events for this code object if in line mode
            if cfg.trace_level == "line":
                sys.monitoring.set_local_events(
                    self._TOOL_ID, code,
                    sys.monitoring.events.LINE,
                )

        def _on_py_return(self, code, instruction_offset, retval):
            """Called when returning from a Python function."""
            c = self.collector
            cfg = self.config

            if c._stopped:
                return sys.monitoring.DISABLE

            # Flush line timing & alloc, then invalidate the anchor so the
            # post-return latency before the caller's next line event is
            # not attributed to this frame's last line.
            if cfg.trace_level == "line":
                if c._last_line_file is not None and c._last_line_time_ns > 0:
                    now_ns = perf_counter_ns()
                    now_alloc = tracemalloc_get()[0] if c._tracemalloc_active else 0
                    delta = now_ns - c._last_line_time_ns
                    c.line_time_ns[c._last_line_file][c._last_line_no] += delta
                    if c._tracemalloc_active:
                        a_delta = now_alloc - c._last_line_alloc
                        if a_delta > 0:
                            c.line_alloc_bytes[c._last_line_file][c._last_line_no] += a_delta
                c._last_line_time_ns = 0

            c._depth -= 1
            if c._depth < 0:
                c._depth = 0

            if c._call_stack and c._call_stack[-1][3] == c._depth:
                entry = c._call_stack.pop()
                func_key, start_ns, start_rss, depth, children_ns, start_alloc, children_alloc, children_rss = entry
                end_ns = perf_counter_ns()
                end_rss = get_rss_bytes()
                if end_rss > c.peak_rss:
                    c.peak_rss = end_rss
                end_alloc = tracemalloc_get()[0] if c._tracemalloc_active else 0

                inclusive_ns = end_ns - start_ns
                exclusive_ns = max(0, inclusive_ns - children_ns)
                inclusive_alloc = max(0, end_alloc - start_alloc) if c._tracemalloc_active else 0
                exclusive_alloc = max(0, inclusive_alloc - children_alloc)
                inclusive_rss = max(0, end_rss - start_rss)
                exclusive_rss = max(0, inclusive_rss - children_rss)

                if c._call_stack:
                    c._call_stack[-1][4] += inclusive_ns
                    c._call_stack[-1][6] += inclusive_alloc
                    c._call_stack[-1][7] += inclusive_rss

                c.call_events.append({
                    "func": func_key,
                    "file": _rel_path(code.co_filename, cfg),
                    "lineno": code.co_firstlineno,
                    "depth": depth,
                    "wall_ns": inclusive_ns,
                    "exclusive_ns": exclusive_ns,
                    "rss_before": start_rss,
                    "rss_after": end_rss,
                    "alloc_inclusive": inclusive_alloc,
                    "alloc_exclusive": exclusive_alloc,
                    "rss_inclusive": inclusive_rss,
                    "rss_exclusive": exclusive_rss,
                })
                c.event_count += 1
            # Test-boundary check AFTER the call_event lands.
            c._exit_test_if_matching(c._depth)

        def _on_line(self, code, line_number):
            """Called for each line executed in traced code objects."""
            c = self.collector
            cfg = self.config

            if c._stopped:
                return sys.monitoring.DISABLE

            if c.event_count >= cfg.max_events:
                if not c._events_trimmed:
                    c._record_trim("max_events", cfg.max_events, 0)
                    c._events_trimmed = True
                return sys.monitoring.DISABLE

            now_ns = perf_counter_ns()
            now_alloc = tracemalloc_get()[0] if c._tracemalloc_active else 0
            # See _SettraceBackend._callback line-event branch: RSS reads are
            # too expensive to do per-line on tests with millions of events.
            rel_filename = _rel_path(code.co_filename, cfg)

            c.line_hits[rel_filename][line_number] += 1

            if c._last_line_file is not None and c._last_line_time_ns > 0:
                delta = now_ns - c._last_line_time_ns
                c.line_time_ns[c._last_line_file][c._last_line_no] += delta
                if c._tracemalloc_active:
                    a_delta = now_alloc - c._last_line_alloc
                    if a_delta > 0:
                        c.line_alloc_bytes[c._last_line_file][c._last_line_no] += a_delta

            c._last_line_time_ns = now_ns
            c._last_line_alloc = now_alloc
            c._last_line_file = rel_filename
            c._last_line_no = line_number

            c.event_count += 1

        def install(self):
            mon = sys.monitoring
            tool_id = self._TOOL_ID
            mon.use_tool_id(tool_id, "swebench_tracer")

            # Use PY_START/PY_RETURN for function entry/exit
            # LINE events are enabled per-code-object in _on_py_start
            events = mon.events.PY_START | mon.events.PY_RETURN
            mon.set_events(tool_id, events)

            mon.register_callback(tool_id, mon.events.PY_START, self._on_py_start)
            mon.register_callback(tool_id, mon.events.PY_RETURN, self._on_py_return)
            if self.config.trace_level == "line":
                mon.register_callback(tool_id, mon.events.LINE, self._on_line)

        def uninstall(self):
            mon = sys.monitoring
            tool_id = self._TOOL_ID
            try:
                mon.set_events(tool_id, 0)
                mon.register_callback(tool_id, mon.events.PY_START, None)
                mon.register_callback(tool_id, mon.events.PY_RETURN, None)
                mon.register_callback(tool_id, mon.events.LINE, None)
                mon.free_tool_id(tool_id)
            except (ValueError, RuntimeError):
                pass


# ---------------------------------------------------------------------------
#  Backend factory
# ---------------------------------------------------------------------------

def _make_backend(collector, config):
    """Create the appropriate tracing backend based on config."""
    backend_name = config.backend

    if backend_name == "auto":
        if config.trace_level == "line":
            if _HAS_SYS_MONITORING:
                return _SysMonBackend(collector, config)
            return _SettraceBackend(collector, config)
        else:
            if _HAS_SYS_MONITORING:
                return _SysMonBackend(collector, config)
            return _SetprofileBackend(collector, config)

    if backend_name == "sysmon":
        if not _HAS_SYS_MONITORING:
            sys.stderr.write("[swebench_tracer] sys.monitoring not available, falling back to settrace\n")
            if config.trace_level == "line":
                return _SettraceBackend(collector, config)
            return _SetprofileBackend(collector, config)
        return _SysMonBackend(collector, config)

    if backend_name == "settrace":
        return _SettraceBackend(collector, config)

    if backend_name == "setprofile":
        if config.trace_level == "line":
            sys.stderr.write("[swebench_tracer] setprofile cannot do line-level, using settrace\n")
            return _SettraceBackend(collector, config)
        return _SetprofileBackend(collector, config)

    # Unknown backend, fall back to auto selection
    sys.stderr.write("[swebench_tracer] unknown backend '%s', using auto\n" % backend_name)
    config.backend = "auto"
    return _make_backend(collector, config)


# ---------------------------------------------------------------------------
#  Per-test trace collector
# ---------------------------------------------------------------------------

class TestTraceCollector(object):
    """Collects trace data for a single test function."""

    def __init__(self, test_nodeid, config=None):
        self.test_nodeid = test_nodeid
        cfg = config or _global_config
        self.config = cfg
        self.trace_level = cfg.trace_level

        # Function-level / line-level data is split into "buckets" so that
        # session-mode traces (Django/sympy) can produce per-test records.
        # In pytest mode the conftest plugin starts a fresh collector per
        # test, so only the session bucket is ever used.
        # In session mode, the call handler watches for test-method entry
        # (any in-project frame whose unqualified name starts with `test_`,
        # entered while no other test is active) and routes subsequent
        # events into a per-test bucket until that frame returns.
        self._session_bucket = self._new_bucket()
        self._test_buckets = {}            # test_id -> bucket
        self._scope_stack = []             # [(test_id, depth_at_entry, bucket)]
        self._active_test_id = None        # None when only session is active
        # Aliases point at the currently-active bucket. Hot-path code uses
        # `self.line_hits[...]` etc. as before.
        self._activate_bucket(self._session_bucket)

        # Per-collector counters
        self.event_count = 0
        self._call_stack = []
        self._depth = 0
        self._seen_functions = set()

        # Line-anchor cursor — global across buckets. The v0.3.1 call/return
        # invalidation (set _last_line_time_ns=0) covers the test-entry
        # boundary too, so no extra reset is needed when swapping buckets.
        self._last_line_time_ns = 0
        self._last_line_alloc = 0
        self._last_line_rss = 0
        self._last_line_file = None
        self._last_line_no = 0

        # Timing
        self.start_time = None
        self.end_time = None
        self.start_rss = 0
        self.peak_rss = 0

        # tracemalloc
        self._tracemalloc_active = tracemalloc_enabled(cfg)
        self.start_traced_bytes = 0
        self.peak_traced_bytes = 0

        # Outcome (set externally via record_outcome())
        self.outcome = None
        self.outcome_details = None
        self.setup_time_s = None
        self.call_time_s = None
        self.teardown_time_s = None

        # Trim tracking
        self.trimmed = False
        self.trim_info = []
        self._events_trimmed = False
        self._depth_trimmed = False
        self._functions_trimmed = False
        self._stopped = False

        # Backend
        self._backend = _make_backend(self, cfg)

    @staticmethod
    def _new_bucket():
        """Allocate a fresh accumulator bucket (session or per-test)."""
        return {
            "call_events": [],
            "line_hits": defaultdict(lambda: defaultdict(int)),
            "line_time_ns": defaultdict(lambda: defaultdict(int)),
            "line_alloc_bytes": defaultdict(lambda: defaultdict(int)),
            "line_rss_bytes": defaultdict(lambda: defaultdict(int)),
            # Per-test timing/memory snapshots, only populated when this
            # bucket is a test bucket (session bucket leaves these None).
            "start_ns": None,
            "end_ns": None,
            "start_traced_bytes": None,
            "peak_traced_bytes": None,
            "start_rss": None,
            "peak_rss": None,
            "outcome": None,
        }

    def _activate_bucket(self, b):
        """Swap accumulator aliases to point at the given bucket. Hot-path
        code reads `self.line_hits[...]` etc. and never has to branch on
        the active scope."""
        self.line_hits = b["line_hits"]
        self.line_time_ns = b["line_time_ns"]
        self.line_alloc_bytes = b["line_alloc_bytes"]
        self.line_rss_bytes = b["line_rss_bytes"]
        self.call_events = b["call_events"]

    def _enter_test(self, test_id, depth):
        """Open a per-test bucket and route subsequent events into it."""
        b = self._new_bucket()
        b["start_ns"] = perf_counter_ns()
        if self._tracemalloc_active:
            # Reset the global tracemalloc peak counter so peak_traced_bytes
            # at test exit reflects THIS test's high water mark, not the
            # session's accumulated peak from earlier tests.
            tracemalloc_reset_peak()
            cur, _ = tracemalloc_get()
            b["start_traced_bytes"] = cur
            b["peak_traced_bytes"] = cur
        b["start_rss"] = get_rss_bytes()
        b["peak_rss"] = b["start_rss"]
        self._test_buckets[test_id] = b
        self._scope_stack.append((test_id, depth, b))
        self._active_test_id = test_id
        self._activate_bucket(b)
        # Invalidate the line anchor so the first line of the test method
        # doesn't inherit a delta from whatever was running before.
        self._last_line_time_ns = 0

    def _exit_test_if_matching(self, depth):
        """If we're returning at the depth where the current test started,
        close that test's bucket and restore the previous active bucket."""
        if not self._scope_stack:
            return
        tid, entry_depth, b = self._scope_stack[-1]
        if depth != entry_depth:
            return
        b["end_ns"] = perf_counter_ns()
        if self._tracemalloc_active:
            _, pk = tracemalloc_get()
            b["peak_traced_bytes"] = pk
        end_rss = get_rss_bytes()
        if end_rss > (b["peak_rss"] or 0):
            b["peak_rss"] = end_rss
        self._scope_stack.pop()
        if self._scope_stack:
            self._active_test_id = self._scope_stack[-1][0]
            self._activate_bucket(self._scope_stack[-1][2])
        else:
            self._active_test_id = None
            self._activate_bucket(self._session_bucket)
        self._last_line_time_ns = 0   # same anchor invalidation

    def _maybe_enter_test(self, func_name, qualname, module, filename, depth,
                          frame=None):
        """Heuristic test-method detection. Only active in session mode
        and only when no other test is currently scoped (prevents nested
        helper functions named `test_*` from opening another scope).

        `frame` is optional but recommended for the Django id format —
        co_qualname is only on Python 3.11+, and SWE-bench Django
        containers often run older Python where qualname falls back to
        just the function name. Reading `self` from the frame gives us
        the class even on older Python.
        """
        if not self.config.session_mode_test_detect:
            return
        if self._active_test_id is not None:
            return
        if not func_name or not func_name.startswith("test_"):
            return
        test_id = self._format_test_id(func_name, qualname, module,
                                       filename, frame)
        self._enter_test(test_id, depth)

    def _format_test_id(self, func_name, qualname, module, filename,
                        frame=None):
        """Produce a test_id whose shape matches the dataset's
        FAIL_TO_PASS convention so the benchmark builder can match by
        equality.

        - "django" → "test_method (module.ClassName)"
        - "bare"   → "func_name" (sympy's FAIL_TO_PASS shape — bare name
          only; sympy tests are top-level functions and the dataset
          records just the function name)
        - "pytest" → "<rel_path>::<qualname-with-:: separators>"
          (generic fallback)

        Class name is taken from qualname if it has a dot prefix
        (Python 3.11+); otherwise read type(frame.f_locals['self']).__name__
        as a fallback (works on older Python).
        """
        fmt = self.config.test_id_format
        cls = ""
        if "." in qualname:
            cls = qualname.rsplit(".", 1)[0]
        elif frame is not None:
            try:
                self_obj = frame.f_locals.get("self")
                if self_obj is not None:
                    cls = type(self_obj).__name__
            except (AttributeError, KeyError, TypeError):
                pass
        if fmt == "django":
            if module and cls:
                return f"{func_name} ({module}.{cls})"
            return func_name
        if fmt == "bare":
            return func_name
        # pytest format
        rel = _rel_path(filename, self.config)
        if cls:
            return f"{rel}::{cls}::{func_name}"
        return f"{rel}::{func_name}"

    def _record_trim(self, reason, limit, events_dropped):
        """Record that a limit was hit."""
        if not self.trimmed:
            self.trimmed = True
        self.trim_info.append({
            "reason": reason,
            "limit": limit,
            "events_dropped": events_dropped,
        })

    def start(self):
        """Begin tracing."""
        if self._tracemalloc_active:
            tracemalloc_ensure_started()
            tracemalloc_reset_peak()
            self.start_traced_bytes, _ = tracemalloc_get()
            self._last_line_alloc = self.start_traced_bytes
        self.start_time = time.perf_counter()
        self.start_rss = get_rss_bytes()
        self.peak_rss = self.start_rss
        self._last_line_rss = self.start_rss
        self._backend.install()

    def stop(self):
        """Stop tracing."""
        self._backend.uninstall()
        self.end_time = time.perf_counter()
        end_rss = get_rss_bytes()
        if end_rss > self.peak_rss:
            self.peak_rss = end_rss
        if self._tracemalloc_active:
            _, self.peak_traced_bytes = tracemalloc_get()

    def record_outcome(self, outcome, details=None,
                       setup_time_s=None, call_time_s=None, teardown_time_s=None):
        """Record test outcome and per-phase timing.

        outcome: "passed" | "failed" | "error" | "skipped"
        details: dict or None
        """
        self.outcome = outcome
        self.outcome_details = details
        if setup_time_s is not None:
            self.setup_time_s = setup_time_s
        if call_time_s is not None:
            self.call_time_s = call_time_s
        if teardown_time_s is not None:
            self.teardown_time_s = teardown_time_s

    def to_dict_all_records(self):
        """Return {test_id: record} for every record this collector produced.

        Pytest mode: one record under `self.test_nodeid`.
        Session mode: one synthetic "session" record (catch-all for events
        outside any detected test method) plus one record per detected
        per-test bucket.
        """
        records = {}
        # Session bucket — always emitted, keyed by self.test_nodeid (which is
        # the per-test nodeid in pytest mode, or "session" in session mode).
        sess = self._bucket_to_record(
            self._session_bucket,
            test_nodeid=self.test_nodeid,
            wall_time_s=(self.end_time - self.start_time)
                        if self.end_time and self.start_time else 0,
            start_rss=self.start_rss,
            peak_rss=self.peak_rss,
            start_traced=self.start_traced_bytes,
            peak_traced=self.peak_traced_bytes,
            outcome=self.outcome,
            outcome_details=self.outcome_details,
            setup_time_s=self.setup_time_s,
            call_time_s=self.call_time_s,
            teardown_time_s=self.teardown_time_s,
        )
        records[self.test_nodeid] = sess
        # Per-test buckets (session-mode only — pytest mode never opens these).
        for tid, b in self._test_buckets.items():
            wall = ((b["end_ns"] or 0) - (b["start_ns"] or 0)) / 1e9
            rec = self._bucket_to_record(
                b,
                test_nodeid=tid,
                wall_time_s=wall,
                start_rss=b["start_rss"],
                peak_rss=b["peak_rss"],
                start_traced=b["start_traced_bytes"],
                peak_traced=b["peak_traced_bytes"],
                outcome=b.get("outcome"),
                outcome_details=None,
                setup_time_s=None,
                call_time_s=None,
                teardown_time_s=None,
            )
            records[tid] = rec
        return records

    def to_dict(self):
        """Backwards-compatible single-record shape.

        Pytest mode: this is the per-test record (unchanged).
        Session mode: this is the session catch-all bucket only —
        use to_dict_all_records() to also get per-test records.
        """
        return self._bucket_to_record(
            self._session_bucket,
            test_nodeid=self.test_nodeid,
            wall_time_s=(self.end_time - self.start_time)
                        if self.end_time and self.start_time else 0,
            start_rss=self.start_rss,
            peak_rss=self.peak_rss,
            start_traced=self.start_traced_bytes,
            peak_traced=self.peak_traced_bytes,
            outcome=self.outcome,
            outcome_details=self.outcome_details,
            setup_time_s=self.setup_time_s,
            call_time_s=self.call_time_s,
            teardown_time_s=self.teardown_time_s,
        )

    def _bucket_to_record(self, bucket, *, test_nodeid, wall_time_s,
                          start_rss, peak_rss, start_traced, peak_traced,
                          outcome, outcome_details, setup_time_s,
                          call_time_s, teardown_time_s):
        """Serialize one accumulator bucket into a per-record dict.

        Reads `call_events`, `line_*` from the bucket; the wrapping
        timing/memory fields come in as kwargs so the same code path
        works for both the session bucket (collector-level timing) and
        per-test buckets (their own start/end timestamps)."""
        call_events = bucket["call_events"]
        line_hits = bucket["line_hits"]
        line_time_ns = bucket["line_time_ns"]
        line_alloc_bytes = bucket["line_alloc_bytes"]
        line_rss_bytes = bucket["line_rss_bytes"]

        result = {
            "test_nodeid": test_nodeid,
            "trace_level": self.trace_level,
            "memory_tracking": self.config.memory_tracking,
            "backend": self._backend.name,
            "wall_time_s": round(wall_time_s or 0, 6),
            "start_rss_bytes": start_rss,
            "peak_rss_bytes": peak_rss,
            "event_count": len(call_events),
        }

        # Outcome
        result["outcome"] = outcome
        if outcome_details is not None:
            result["outcome_details"] = outcome_details
        if setup_time_s is not None:
            result["setup_time_s"] = round(setup_time_s, 6)
        if call_time_s is not None:
            result["call_time_s"] = round(call_time_s, 6)
        if teardown_time_s is not None:
            result["teardown_time_s"] = round(teardown_time_s, 6)

        # Tracemalloc summary
        if self._tracemalloc_active:
            result["tracemalloc_enabled"] = True
            result["start_traced_bytes"] = start_traced or 0
            result["peak_traced_bytes"] = peak_traced or 0

        if self.trimmed:
            result["trimmed"] = True
            result["trim_info"] = self.trim_info

        # Function summary
        if call_events:
            func_summary = defaultdict(lambda: {
                "count": 0, "total_ns": 0, "total_exclusive_ns": 0,
                "max_ns": 0, "file": "", "lineno": 0, "max_depth": 0,
                "max_rss_delta": 0,
                "total_alloc": 0, "total_exclusive_alloc": 0, "max_alloc": 0,
                "total_rss_inclusive": 0, "total_rss_exclusive": 0,
                "max_rss_inclusive": 0,
            })
            for ev in call_events:
                key = ev["func"]
                s = func_summary[key]
                s["count"] += 1
                s["total_ns"] += ev["wall_ns"]
                s["total_exclusive_ns"] += ev.get("exclusive_ns", ev["wall_ns"])
                if ev["wall_ns"] > s["max_ns"]:
                    s["max_ns"] = ev["wall_ns"]
                s["file"] = ev["file"]
                s["lineno"] = ev["lineno"]
                if ev["depth"] > s["max_depth"]:
                    s["max_depth"] = ev["depth"]
                rss_delta = ev["rss_after"] - ev["rss_before"]
                if rss_delta > s["max_rss_delta"]:
                    s["max_rss_delta"] = rss_delta
                inc_alloc = ev.get("alloc_inclusive", 0)
                exc_alloc = ev.get("alloc_exclusive", 0)
                s["total_alloc"] += inc_alloc
                s["total_exclusive_alloc"] += exc_alloc
                if inc_alloc > s["max_alloc"]:
                    s["max_alloc"] = inc_alloc
                inc_rss = ev.get("rss_inclusive", 0)
                exc_rss = ev.get("rss_exclusive", 0)
                s["total_rss_inclusive"] += inc_rss
                s["total_rss_exclusive"] += exc_rss
                if inc_rss > s["max_rss_inclusive"]:
                    s["max_rss_inclusive"] = inc_rss

            sorted_funcs = sorted(
                func_summary.items(),
                key=lambda x: x[1]["total_ns"],
                reverse=True,
            )

            include_alloc = self._tracemalloc_active
            functions_out = []
            for name, data in sorted_funcs:
                entry = {
                    "func": name,
                    "file": data["file"],
                    "lineno": data["lineno"],
                    "call_count": data["count"],
                    "total_time_s": round(data["total_ns"] / 1e9, 9),
                    "exclusive_time_s": round(data["total_exclusive_ns"] / 1e9, 9),
                    "max_time_s": round(data["max_ns"] / 1e9, 9),
                    "max_depth": data["max_depth"],
                    "max_rss_delta_bytes": data["max_rss_delta"],
                    "total_rss_inclusive_bytes": data["total_rss_inclusive"],
                    "exclusive_rss_bytes": data["total_rss_exclusive"],
                    "max_rss_inclusive_bytes": data["max_rss_inclusive"],
                }
                if include_alloc:
                    entry["total_alloc_bytes"] = data["total_alloc"]
                    entry["exclusive_alloc_bytes"] = data["total_exclusive_alloc"]
                    entry["max_alloc_bytes"] = data["max_alloc"]
                functions_out.append(entry)
            result["functions"] = functions_out

            max_raw = self.config.max_call_seq
            result["call_sequence"] = [
                {
                    "func": ev["func"],
                    "depth": ev["depth"],
                    "wall_ns": ev["wall_ns"],
                }
                for ev in call_events[:max_raw]
            ]
            result["call_sequence_truncated"] = len(call_events) > max_raw
            result["total_call_events"] = len(call_events)

        if self.trace_level == "line":
            line_data = {}
            include_alloc = self._tracemalloc_active
            for filename, lines in line_hits.items():
                file_lines = {}
                time_data = line_time_ns.get(filename, {})
                alloc_data = line_alloc_bytes.get(filename, {}) if include_alloc else {}
                rss_data = line_rss_bytes.get(filename, {})
                for lineno, hits in lines.items():
                    line_entry = {
                        "hits": hits,
                        "time_ns": time_data.get(lineno, 0),
                    }
                    if include_alloc:
                        line_entry["alloc_bytes"] = alloc_data.get(lineno, 0)
                    line_entry["rss_bytes"] = rss_data.get(lineno, 0)
                    file_lines[str(lineno)] = line_entry
                line_data[filename] = file_lines
            result["lines"] = line_data

        return result


# ---------------------------------------------------------------------------
#  Global state
# ---------------------------------------------------------------------------

_global_config = TraceConfig()
_all_traces = {}
_current_collector = None
_pending_outcomes = {}  # test_nodeid -> outcome dict captured before stop_trace


def start_trace(test_nodeid, config=None):
    """Start tracing for a test."""
    global _current_collector
    cfg = config or _global_config
    if not cfg.enabled:
        return
    _current_collector = TestTraceCollector(test_nodeid, config=cfg)
    _current_collector.start()


def stop_trace(test_nodeid):
    """Stop tracing for a test and store results."""
    global _current_collector
    if _current_collector is None:
        return
    _current_collector.stop()
    # Apply any outcome that was captured between start_trace and stop_trace
    pending = _pending_outcomes.pop(test_nodeid, None)
    if pending is not None:
        _current_collector.record_outcome(**pending)
    # In session-mode with test-boundary detection enabled, the collector
    # may have produced per-test sub-records in addition to the session
    # bucket; merge all of them into _all_traces.
    for tid, rec in _current_collector.to_dict_all_records().items():
        _all_traces[tid] = rec
    _current_collector = None


def record_test_outcome(test_nodeid, outcome, details=None,
                        setup_time_s=None, call_time_s=None, teardown_time_s=None):
    """Public API: record test outcome.

    Can be called between start_trace and stop_trace (applied to current
    collector at stop), or after stop_trace (patches the stored result).
    """
    payload = {
        "outcome": outcome,
        "details": details,
        "setup_time_s": setup_time_s,
        "call_time_s": call_time_s,
        "teardown_time_s": teardown_time_s,
    }
    if _current_collector is not None and _current_collector.test_nodeid == test_nodeid:
        # Outcome captured during the test window: stash so stop_trace applies it.
        _pending_outcomes[test_nodeid] = payload
        return
    # Outcome captured after the trace was already stored: patch the stored dict.
    if test_nodeid in _all_traces:
        rec = _all_traces[test_nodeid]
        rec["outcome"] = outcome
        if details is not None:
            rec["outcome_details"] = details
        if setup_time_s is not None:
            rec["setup_time_s"] = round(setup_time_s, 6)
        if call_time_s is not None:
            rec["call_time_s"] = round(call_time_s, 6)
        if teardown_time_s is not None:
            rec["teardown_time_s"] = round(teardown_time_s, 6)
    else:
        # Stash for whenever stop_trace fires (e.g. in session-mode races).
        _pending_outcomes[test_nodeid] = payload


# Set inside a pytest-xdist worker (e.g. "gw0"); absent in the controller and
# in a single-process run. Under xdist the tests run in the workers, so each
# worker writes its own side file (`<output>.worker-<id>`) and the controller
# merges them -- otherwise every worker would write the shared output path and
# clobber the others, keeping only one worker's subset of tests.
_XDIST_WORKER = os.environ.get("PYTEST_XDIST_WORKER")


def _merge_worker_traces(base_path):
    """Controller-only: fold each xdist worker's trace side file into `_all_traces`."""
    import glob as _glob
    for wpath in _glob.glob(base_path + ".worker-*"):
        try:
            if wpath.endswith(".gz"):
                import gzip
                with gzip.open(wpath, "rt", encoding="utf-8") as f:
                    data = json.load(f)
            else:
                with open(wpath) as f:
                    data = json.load(f)
        except Exception:
            continue
        for test_id, rec in (data.get("tests") or {}).items():
            _all_traces.setdefault(test_id, rec)


def write_output(output_path=None, config=None):
    """Write all trace data to JSON (or gzip-compressed JSON)."""
    cfg = config or _global_config
    if not cfg.enabled:
        return
    path = output_path or cfg.output_path
    # An xdist worker writes to its own side file; the controller merges those
    # into the shared output path at session finish.
    if _XDIST_WORKER:
        path = path + ".worker-" + _XDIST_WORKER

    result = {
        "tracer_version": "0.4.0",
        "trace_level": cfg.trace_level,
        "memory_tracking": cfg.memory_tracking,
        "backend": cfg.backend,
        "compress": cfg.compress,
        "python_version": sys.version,
        "instance_id": cfg.instance_id,
        "test_count": len(_all_traces),
        "tests": _all_traces,
    }

    try:
        dir_name = os.path.dirname(path)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)

        if cfg.compress:
            import gzip
            if not path.endswith(".gz"):
                path = path + ".gz"
            data = json.dumps(result, separators=(",", ":"))
            with gzip.open(path, "wt", encoding="utf-8") as f:
                f.write(data)
        else:
            with open(path, "w") as f:
                json.dump(result, f, separators=(",", ":"))
    except Exception as e:
        sys.stderr.write("[swebench_tracer] Failed to write trace: %s\n" % e)


def reset_state():
    """Reset global state (useful for testing)."""
    global _all_traces, _current_collector
    _all_traces = {}
    _current_collector = None


# ---------------------------------------------------------------------------
#  Pytest plugin hooks (when used as conftest.py)
# ---------------------------------------------------------------------------

_pytest_phase_durations = {}  # nodeid -> {"setup": s, "call": s, "teardown": s}
_pytest_outcomes = {}         # nodeid -> {"outcome": ..., "details": ...}


def _format_traceback(longrepr, max_chars=4096):
    """Best-effort string-repr of a pytest longrepr, capped."""
    if longrepr is None:
        return None
    try:
        text = str(longrepr)
    except Exception:
        text = "<unrepresentable longrepr>"
    if len(text) > max_chars:
        return text[:max_chars] + "\n... [truncated]"
    return text


def _extract_failure_location(longrepr):
    """Try to extract (file, line) of the failing assertion from pytest longrepr.

    pytest's longrepr structure varies by version, so we use best-effort
    attribute access and fall back to (None, None).
    """
    try:
        crash = getattr(longrepr, "reprcrash", None)
        if crash is not None:
            return getattr(crash, "path", None), getattr(crash, "lineno", None)
    except Exception:
        pass
    return None, None


# Set of FAIL_TO_PASS test nodeids the harness wants traced. Empty set means
# "trace every test" (legacy behavior). Populated from
# SWEBENCH_TRACE_FAIL_TO_PASS env var (newline-or-comma-separated).
_fail_to_pass = set()
_raw_f2p = os.environ.get("SWEBENCH_TRACE_FAIL_TO_PASS", "").strip()
if _raw_f2p:
    sep = "\n" if "\n" in _raw_f2p else ","
    _fail_to_pass = {x.strip() for x in _raw_f2p.split(sep) if x.strip()}


def _should_trace_nodeid(nodeid):
    """True if this test nodeid is in the FAIL_TO_PASS set (or set is empty)."""
    if not _fail_to_pass:
        return True
    if nodeid in _fail_to_pass:
        return True
    # Pytest may have parametrized variants like "test_x[case_a]" that match
    # the bare "test_x" form in the SWE-bench list.
    bare = nodeid.split("[")[0] if "[" in nodeid else nodeid
    return bare in _fail_to_pass


def pytest_runtest_setup(item):
    """Called before each test."""
    if _should_trace_nodeid(item.nodeid):
        start_trace(item.nodeid)


def pytest_runtest_teardown(item, nextitem):
    """Called after each test."""
    if _should_trace_nodeid(item.nodeid):
        stop_trace(item.nodeid)


def pytest_runtest_logreport(report):
    """Capture per-phase outcome and timing.

    The `call` phase is the test function itself. setup/teardown are fixture
    phases. We aggregate so that:
      - Test outcome = call.outcome unless setup/teardown errored.
      - If setup errored → outcome="error", failure_phase="setup".
    """
    nodeid = report.nodeid
    if not _should_trace_nodeid(nodeid):
        return
    durations = _pytest_phase_durations.setdefault(nodeid, {})
    durations[report.when] = float(getattr(report, "duration", 0.0))

    existing = _pytest_outcomes.get(nodeid)
    cur_outcome = report.outcome  # "passed" | "failed" | "skipped"

    if cur_outcome == "failed":
        details = {
            "exception_type": None,
            "exception_message": None,
            "is_assertion": False,
            "failure_file": None,
            "failure_line": None,
            "failure_phase": report.when,
            "traceback_text": _format_traceback(getattr(report, "longrepr", None)),
        }
        longrepr = getattr(report, "longrepr", None)
        # Try to extract type/message
        try:
            crash = getattr(longrepr, "reprcrash", None)
            msg = getattr(crash, "message", None) if crash is not None else None
            if msg:
                # pytest formats as "ExceptionType: message"
                if ": " in msg:
                    typ, m = msg.split(": ", 1)
                    details["exception_type"] = typ
                    details["exception_message"] = m
                else:
                    details["exception_type"] = msg
                details["is_assertion"] = (details["exception_type"] == "AssertionError")
        except Exception:
            pass
        f, ln = _extract_failure_location(longrepr)
        details["failure_file"] = f
        details["failure_line"] = ln
        # error vs failed: pytest treats setup/teardown failure as "error"
        outcome_label = "error" if report.when != "call" else "failed"
        _pytest_outcomes[nodeid] = {"outcome": outcome_label, "details": details}
    elif cur_outcome == "skipped":
        # Skipped only matters if we don't already have a fail/error
        if existing is None:
            details = {
                "exception_type": "Skipped",
                "exception_message": _format_traceback(getattr(report, "longrepr", None), 256),
                "is_assertion": False,
                "failure_file": None, "failure_line": None,
                "failure_phase": report.when, "traceback_text": None,
            }
            _pytest_outcomes[nodeid] = {"outcome": "skipped", "details": details}
    elif cur_outcome == "passed" and report.when == "call" and existing is None:
        _pytest_outcomes[nodeid] = {"outcome": "passed", "details": None}


def _apply_pytest_outcome(nodeid):
    """Apply captured outcome+timing for nodeid (called from teardown after stop_trace).
    Internal helper invoked by pytest_runtest_teardown via a wrapper if needed.
    """
    rec = _pytest_outcomes.get(nodeid)
    durations = _pytest_phase_durations.get(nodeid, {})
    if rec is None:
        return
    record_test_outcome(
        nodeid,
        outcome=rec["outcome"],
        details=rec["details"],
        setup_time_s=durations.get("setup"),
        call_time_s=durations.get("call"),
        teardown_time_s=durations.get("teardown"),
    )


def pytest_sessionfinish(session, exitstatus):
    """Called at the end of the test session.

    Outcomes captured via pytest_runtest_logreport are applied here so that
    every stored trace has its outcome attached.
    """
    for nodeid in list(_pytest_outcomes.keys()):
        _apply_pytest_outcome(nodeid)
    if not _XDIST_WORKER:
        _merge_worker_traces(_global_config.output_path)
    write_output()


# ---------------------------------------------------------------------------
#  Django / generic support
# ---------------------------------------------------------------------------

_django_mode = os.environ.get("SWEBENCH_TRACE_DJANGO", "0") == "1"

if _django_mode and _global_config.enabled:
    import atexit as _atexit

    # In session mode, the harness sets SWEBENCH_TRACE_TEST_NODEID per
    # invocation so each invocation produces a per-test trace.
    _session_test_nodeid = os.environ.get("SWEBENCH_TRACE_TEST_NODEID", "session")

    def _django_session_trace():
        start_trace(_session_test_nodeid)

    def _django_session_finish():
        stop_trace(_session_test_nodeid)
        write_output()

    _django_session_trace()
    _atexit.register(_django_session_finish)
