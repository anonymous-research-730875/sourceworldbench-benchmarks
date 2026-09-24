"""End-to-end smoke tests for the v0.3 tracer features.

Tests tracemalloc-based allocation tracking, outcome capture, and per-line
allocation attribution without requiring Docker.
"""

import importlib
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sourceworldbench_benchmarks.execution_tracer", "tracers"))


def _fresh_tracer(env_vars):
    """Reload the tracer module with fresh env vars and return it."""
    for k, v in env_vars.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    if "injectable_tracer" in sys.modules:
        del sys.modules["injectable_tracer"]
    return importlib.import_module("injectable_tracer")


@pytest.fixture
def tmpdir_path(tmp_path):
    return tmp_path


def _make_workload_dir(tmp_path):
    """Create a small workload .py file and return the dir + path."""
    workload = tmp_path / "workload.py"
    workload.write_text(
        "def allocate_a(n):\n"
        "    return [i * 2 for i in range(n)]\n"
        "\n"
        "def allocate_b(n):\n"
        "    return {i: 'x' * 64 for i in range(n)}\n"
        "\n"
        "def parent(n):\n"
        "    a = allocate_a(n)\n"
        "    b = allocate_b(n)\n"
        "    return len(a) + len(b)\n"
    )
    return tmp_path, workload


def test_tracemalloc_function_alloc(tmp_path):
    """Function-level allocation tracking attributes bytes to functions."""
    repo_dir, workload = _make_workload_dir(tmp_path)
    out = tmp_path / "trace.json"

    tracer = _fresh_tracer({
        "SWEBENCH_TRACE_ENABLED": "1",
        "SWEBENCH_TRACE_LEVEL": "function",
        "SWEBENCH_TRACE_MEMORY": "tracemalloc",
        "SWEBENCH_TRACE_OUTPUT": str(out),
        "SWEBENCH_REPO_DIR": str(repo_dir),
        "SWEBENCH_TRACE_BACKEND": "settrace",
        "SWEBENCH_TRACE_COMPRESS": "0",
        "SWEBENCH_TRACE_DJANGO": None,
        "SWEBENCH_TRACE_TEST_NODEID": None,
    })

    sys.path.insert(0, str(repo_dir))
    import workload as wl  # noqa: E402

    tracer.start_trace("test_a")
    wl.parent(2000)
    tracer.stop_trace("test_a")
    tracer.write_output()

    sys.path.remove(str(repo_dir))
    if "workload" in sys.modules:
        del sys.modules["workload"]

    data = json.loads(out.read_text())
    assert data["tracer_version"] == "0.4.0"
    assert data["memory_tracking"] == "tracemalloc"

    test = data["tests"]["test_a"]
    assert test.get("tracemalloc_enabled") is True
    assert test["peak_traced_bytes"] > 0

    funcs_by_name = {f["func"]: f for f in test["functions"]}
    # Each function should have alloc fields populated
    for name, f in funcs_by_name.items():
        assert "exclusive_alloc_bytes" in f, f"missing in {name}"
        assert "total_alloc_bytes" in f, f"missing in {name}"
        assert "max_alloc_bytes" in f, f"missing in {name}"

    # parent's exclusive_alloc should be < its total_alloc since it has children
    parent_fn = next(
        (f for n, f in funcs_by_name.items() if n.endswith(".parent")), None
    )
    if parent_fn is not None:
        assert parent_fn["exclusive_alloc_bytes"] <= parent_fn["total_alloc_bytes"]


def test_line_level_alloc(tmp_path):
    """Line-level allocation attribution: per-line alloc_bytes is populated."""
    repo_dir, workload = _make_workload_dir(tmp_path)
    out = tmp_path / "trace.json"

    tracer = _fresh_tracer({
        "SWEBENCH_TRACE_ENABLED": "1",
        "SWEBENCH_TRACE_LEVEL": "line",
        "SWEBENCH_TRACE_MEMORY": "both",
        "SWEBENCH_TRACE_OUTPUT": str(out),
        "SWEBENCH_REPO_DIR": str(repo_dir),
        "SWEBENCH_TRACE_BACKEND": "settrace",
        "SWEBENCH_TRACE_COMPRESS": "0",
        "SWEBENCH_TRACE_DJANGO": None,
        "SWEBENCH_TRACE_TEST_NODEID": None,
    })

    sys.path.insert(0, str(repo_dir))
    import workload as wl  # noqa: E402

    tracer.start_trace("test_lines")
    wl.parent(1500)
    tracer.stop_trace("test_lines")
    tracer.write_output()

    sys.path.remove(str(repo_dir))
    if "workload" in sys.modules:
        del sys.modules["workload"]

    data = json.loads(out.read_text())
    test = data["tests"]["test_lines"]
    lines_data = test.get("lines", {})
    assert lines_data, "expected line-level data"

    # Find any file/line with alloc_bytes > 0
    has_alloc = False
    for filename, file_lines in lines_data.items():
        for lineno, info in file_lines.items():
            assert "hits" in info
            assert "time_ns" in info
            assert "alloc_bytes" in info, f"missing alloc_bytes for {filename}:{lineno}"
            if info["alloc_bytes"] > 0:
                has_alloc = True
    assert has_alloc, "expected at least one line with positive alloc_bytes"


def test_outcome_recording(tmp_path):
    """record_outcome stores outcome data into the trace dict."""
    repo_dir, workload = _make_workload_dir(tmp_path)
    out = tmp_path / "trace.json"

    tracer = _fresh_tracer({
        "SWEBENCH_TRACE_ENABLED": "1",
        "SWEBENCH_TRACE_LEVEL": "function",
        "SWEBENCH_TRACE_MEMORY": "rss",
        "SWEBENCH_TRACE_OUTPUT": str(out),
        "SWEBENCH_REPO_DIR": str(repo_dir),
        "SWEBENCH_TRACE_BACKEND": "settrace",
        "SWEBENCH_TRACE_COMPRESS": "0",
        "SWEBENCH_TRACE_DJANGO": None,
        "SWEBENCH_TRACE_TEST_NODEID": None,
    })

    sys.path.insert(0, str(repo_dir))
    import workload as wl  # noqa: E402

    tracer.start_trace("test_outcome")
    wl.parent(100)
    tracer.record_test_outcome(
        "test_outcome",
        outcome="failed",
        details={
            "exception_type": "AssertionError",
            "exception_message": "expected 5, got 4",
            "is_assertion": True,
            "failure_file": "tests/test_x.py",
            "failure_line": 42,
            "failure_phase": "call",
            "traceback_text": "Traceback...",
        },
        setup_time_s=0.001, call_time_s=0.020, teardown_time_s=0.001,
    )
    tracer.stop_trace("test_outcome")
    tracer.write_output()

    sys.path.remove(str(repo_dir))
    if "workload" in sys.modules:
        del sys.modules["workload"]

    data = json.loads(out.read_text())
    test = data["tests"]["test_outcome"]
    assert test["outcome"] == "failed"
    assert test["outcome_details"]["is_assertion"] is True
    assert test["outcome_details"]["exception_type"] == "AssertionError"
    assert test["outcome_details"]["failure_line"] == 42
    assert test["call_time_s"] == 0.02
    assert test["setup_time_s"] == 0.001


def test_outcome_post_hoc_patch(tmp_path):
    """record_test_outcome works after stop_trace (patches stored trace)."""
    repo_dir, _ = _make_workload_dir(tmp_path)
    out = tmp_path / "trace.json"

    tracer = _fresh_tracer({
        "SWEBENCH_TRACE_ENABLED": "1",
        "SWEBENCH_TRACE_LEVEL": "function",
        "SWEBENCH_TRACE_MEMORY": "rss",
        "SWEBENCH_TRACE_OUTPUT": str(out),
        "SWEBENCH_REPO_DIR": str(repo_dir),
        "SWEBENCH_TRACE_BACKEND": "settrace",
        "SWEBENCH_TRACE_COMPRESS": "0",
        "SWEBENCH_TRACE_DJANGO": None,
        "SWEBENCH_TRACE_TEST_NODEID": None,
    })

    sys.path.insert(0, str(repo_dir))
    import workload as wl  # noqa: E402

    tracer.start_trace("test_late")
    wl.parent(100)
    tracer.stop_trace("test_late")
    # Outcome recorded AFTER stop_trace
    tracer.record_test_outcome(
        "test_late", outcome="passed", details=None, call_time_s=0.123
    )
    tracer.write_output()

    sys.path.remove(str(repo_dir))
    if "workload" in sys.modules:
        del sys.modules["workload"]

    data = json.loads(out.read_text())
    test = data["tests"]["test_late"]
    assert test["outcome"] == "passed"
    assert test["call_time_s"] == 0.123


def test_memory_tracking_rss_only_no_alloc_fields(tmp_path):
    """When memory_tracking='rss', no alloc fields appear in functions."""
    repo_dir, _ = _make_workload_dir(tmp_path)
    out = tmp_path / "trace.json"

    tracer = _fresh_tracer({
        "SWEBENCH_TRACE_ENABLED": "1",
        "SWEBENCH_TRACE_LEVEL": "function",
        "SWEBENCH_TRACE_MEMORY": "rss",
        "SWEBENCH_TRACE_OUTPUT": str(out),
        "SWEBENCH_REPO_DIR": str(repo_dir),
        "SWEBENCH_TRACE_BACKEND": "settrace",
        "SWEBENCH_TRACE_COMPRESS": "0",
        "SWEBENCH_TRACE_DJANGO": None,
        "SWEBENCH_TRACE_TEST_NODEID": None,
    })

    sys.path.insert(0, str(repo_dir))
    import workload as wl  # noqa: E402
    tracer.start_trace("test_rss")
    wl.parent(100)
    tracer.stop_trace("test_rss")
    tracer.write_output()
    sys.path.remove(str(repo_dir))
    if "workload" in sys.modules:
        del sys.modules["workload"]

    data = json.loads(out.read_text())
    test = data["tests"]["test_rss"]
    assert test.get("tracemalloc_enabled") is None or test.get("tracemalloc_enabled") is False
    for f in test["functions"]:
        assert "max_rss_delta_bytes" in f
        assert "exclusive_alloc_bytes" not in f
        assert "total_alloc_bytes" not in f
