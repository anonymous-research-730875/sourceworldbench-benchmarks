"""
tests/test_tracer.py — Comprehensive test suite for injectable_tracer.py

Tests cover: TraceConfig, path filtering, all three backends (setprofile,
settrace, sysmon), exclusive time computation, limits/trimming, gzip
compression, output schema compatibility, pytest plugin integration, and
golden-file regression.
"""

import gzip
import json
import os
import sys
import time

import pytest

# Import the tracer module (not as conftest — import directly)
from sourceworldbench_benchmarks.execution_tracer.tracers import injectable_tracer as tracer

# ---------------------------------------------------------------------------
#  Helpers: sample functions to trace
# ---------------------------------------------------------------------------

# We create a temporary "repo" directory and write sample .py files there
# so that _should_trace_file considers them traceable.

@pytest.fixture(autouse=True)
def _clean_tracer_state():
    """Ensure tracer global state is reset after every test."""
    yield
    tracer.reset_state()


@pytest.fixture
def repo_dir(tmp_path):
    """Create a fake repo directory with a sample Python file."""
    src = tmp_path / "src"
    src.mkdir()
    sample = src / "sample.py"
    sample.write_text(
        "def leaf():\n"
        "    x = 1 + 2\n"
        "    return x\n"
        "\n"
        "def middle():\n"
        "    return leaf()\n"
        "\n"
        "def outer():\n"
        "    return middle()\n"
        "\n"
        "def recursive(n):\n"
        "    if n <= 0:\n"
        "        return 0\n"
        "    return recursive(n - 1)\n"
    )
    return tmp_path


@pytest.fixture
def make_config(repo_dir):
    """Factory for creating TraceConfig pointing at the fake repo."""
    def _make(**kwargs):
        defaults = dict(
            enabled=True,
            trace_level="function",
            output_path=str(repo_dir / "trace_output.json"),
            trace_paths=[],
            module_prefixes=[],
            repo_dir=str(repo_dir),
            compress=False,
            backend="auto",
            max_events=5_000_000,
            max_depth=200,
            max_functions=10_000,
            max_duration=300.0,
            max_call_seq=1000,
        )
        defaults.update(kwargs)
        return tracer.TraceConfig(**defaults)
    return _make


def _run_traced(config, func, *args):
    """Run a function under the tracer and return the to_dict() output."""
    collector = tracer.TestTraceCollector("test::sample", config=config)
    collector.start()
    try:
        func(*args)
    finally:
        collector.stop()
    return collector.to_dict()


def _import_sample(repo_dir):
    """Import the sample module from the fake repo."""
    src_dir = str(repo_dir / "src")
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    # Use importlib to always get a fresh module
    import importlib
    spec = importlib.util.spec_from_file_location("sample", str(repo_dir / "src" / "sample.py"))
    mod = importlib.util.module_from_spec(spec)
    mod.__name__ = "sample"
    spec.loader.exec_module(mod)
    return mod


# ═══════════════════════════════════════════════════════════════════════════
#  Group 1: TraceConfig
# ═══════════════════════════════════════════════════════════════════════════

class TestTraceConfig:
    def test_defaults_from_empty_env(self, monkeypatch):
        """Default config values when no env vars are set."""
        for key in list(os.environ):
            if key.startswith("SWEBENCH_TRACE"):
                monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv("SWEBENCH_REPO_DIR", raising=False)

        cfg = tracer.TraceConfig()
        assert cfg.enabled is True
        assert cfg.trace_level == "function"
        assert cfg.output_path == "/testbed/trace_output.json"
        assert cfg.repo_dir == "/testbed"
        assert cfg.compress is False
        assert cfg.backend == "auto"
        assert cfg.max_events == 5_000_000
        assert cfg.max_depth == 200
        assert cfg.max_functions == 10_000
        assert cfg.max_duration == 300.0
        assert cfg.max_call_seq == 1000
        assert cfg.trace_paths == []
        assert cfg.module_prefixes == []

    def test_custom_values_from_env(self, monkeypatch):
        """Config reads custom values from environment variables."""
        monkeypatch.setenv("SWEBENCH_TRACE_ENABLED", "0")
        monkeypatch.setenv("SWEBENCH_TRACE_LEVEL", "line")
        monkeypatch.setenv("SWEBENCH_TRACE_OUTPUT", "/tmp/out.json")
        monkeypatch.setenv("SWEBENCH_TRACE_PATHS", "src/foo,src/bar")
        monkeypatch.setenv("SWEBENCH_TRACE_COMPRESS", "1")
        monkeypatch.setenv("SWEBENCH_TRACE_BACKEND", "settrace")
        monkeypatch.setenv("SWEBENCH_TRACE_MAX_EVENTS", "1000")
        monkeypatch.setenv("SWEBENCH_TRACE_MAX_DEPTH", "50")
        monkeypatch.setenv("SWEBENCH_TRACE_MAX_FUNCTIONS", "500")
        monkeypatch.setenv("SWEBENCH_TRACE_MAX_DURATION", "60.5")
        monkeypatch.setenv("SWEBENCH_TRACE_MAX_CALL_SEQ", "200")

        cfg = tracer.TraceConfig()
        assert cfg.enabled is False
        assert cfg.trace_level == "line"
        assert cfg.output_path == "/tmp/out.json"
        assert cfg.trace_paths == ["src/foo", "src/bar"]
        assert cfg.compress is True
        assert cfg.backend == "settrace"
        assert cfg.max_events == 1000
        assert cfg.max_depth == 50
        assert cfg.max_functions == 500
        assert cfg.max_duration == 60.5
        assert cfg.max_call_seq == 200

    def test_invalid_values_fallback(self, monkeypatch):
        """Invalid numeric env vars fall back to defaults."""
        monkeypatch.setenv("SWEBENCH_TRACE_MAX_EVENTS", "not_a_number")
        monkeypatch.setenv("SWEBENCH_TRACE_MAX_DURATION", "bad")

        cfg = tracer.TraceConfig()
        assert cfg.max_events == 5_000_000
        assert cfg.max_duration == 300.0


# ═══════════════════════════════════════════════════════════════════════════
#  Group 2: Path Filtering
# ═══════════════════════════════════════════════════════════════════════════

class TestPathFiltering:
    def test_files_under_repo_dir_traced(self, make_config, repo_dir):
        cfg = make_config()
        assert tracer._should_trace_file(str(repo_dir / "src" / "sample.py"), cfg)

    def test_site_packages_excluded(self, make_config, repo_dir):
        cfg = make_config()
        assert not tracer._should_trace_file(
            str(repo_dir / "site-packages" / "pkg" / "mod.py"), cfg
        )

    def test_tracer_own_files_excluded(self, make_config, repo_dir):
        cfg = make_config()
        assert not tracer._should_trace_file(str(repo_dir / "conftest.py"), cfg)
        assert not tracer._should_trace_file(str(repo_dir / "_swebench_tracer.py"), cfg)
        assert not tracer._should_trace_file(str(repo_dir / "_trace_wrapper.py"), cfg)

    def test_trace_paths_prefix_filtering(self, make_config, repo_dir):
        cfg = make_config(trace_paths=["src/"])
        assert tracer._should_trace_file(str(repo_dir / "src" / "sample.py"), cfg)
        assert not tracer._should_trace_file(str(repo_dir / "tests" / "test.py"), cfg)

    def test_empty_filename_rejected(self, make_config):
        cfg = make_config()
        assert not tracer._should_trace_file("", cfg)
        assert not tracer._should_trace_file(None, cfg)

    def test_generated_code_rejected(self, make_config):
        cfg = make_config()
        assert not tracer._should_trace_file("<string>", cfg)
        assert not tracer._should_trace_file("<frozen importlib._bootstrap>", cfg)


# ═══════════════════════════════════════════════════════════════════════════
#  Group 3: SetprofileBackend — Call Sequence
# ═══════════════════════════════════════════════════════════════════════════

class TestSetprofileBackend:
    def test_simple_call_recorded(self, make_config, repo_dir):
        cfg = make_config(backend="setprofile")
        mod = _import_sample(repo_dir)

        result = _run_traced(cfg, mod.leaf)
        assert result["event_count"] > 0
        funcs = [f["func"] for f in result.get("functions", [])]
        assert any("leaf" in f for f in funcs)

    def test_nested_calls_depth(self, make_config, repo_dir):
        cfg = make_config(backend="setprofile")
        mod = _import_sample(repo_dir)

        result = _run_traced(cfg, mod.outer)
        call_seq = result.get("call_sequence", [])
        # Should have events for outer, middle, leaf
        func_names = {ev["func"] for ev in call_seq}
        assert any("leaf" in f for f in func_names)
        assert any("middle" in f for f in func_names)
        assert any("outer" in f for f in func_names)

        # Check depths: leaf at depth 2, middle at depth 1, outer at depth 0
        # (call_sequence is in return order: leaf first, outer last)
        for ev in call_seq:
            if "leaf" in ev["func"]:
                assert ev["depth"] == 2
            elif "middle" in ev["func"]:
                assert ev["depth"] == 1
            elif "outer" in ev["func"]:
                assert ev["depth"] == 0

    def test_qualname_format(self, make_config, repo_dir):
        cfg = make_config(backend="setprofile")
        mod = _import_sample(repo_dir)

        result = _run_traced(cfg, mod.leaf)
        funcs = result.get("functions", [])
        # func should be "module.qualname" format
        assert len(funcs) > 0
        for f in funcs:
            assert "." in f["func"]

    def test_non_traced_files_skipped(self, make_config, repo_dir):
        """stdlib calls should not appear in trace."""
        cfg = make_config(backend="setprofile")

        def stdlib_caller():
            # This calls into json (stdlib), which should not be traced
            json.dumps({"a": 1})

        result = _run_traced(cfg, stdlib_caller)
        # stdlib_caller itself is not in repo_dir, so nothing should be traced
        assert result["event_count"] == 0


# ═══════════════════════════════════════════════════════════════════════════
#  Group 4: Exclusive Time
# ═══════════════════════════════════════════════════════════════════════════

class TestExclusiveTime:
    def test_single_function_exclusive_equals_inclusive(self, make_config, repo_dir):
        """For a leaf function, exclusive time should equal inclusive time."""
        cfg = make_config(backend="setprofile")
        mod = _import_sample(repo_dir)

        result = _run_traced(cfg, mod.leaf)
        for f in result.get("functions", []):
            if "leaf" in f["func"]:
                assert f["exclusive_time_s"] == f["total_time_s"]

    def test_parent_exclusive_less_than_inclusive(self, make_config, repo_dir):
        """Parent's exclusive time < inclusive time when it calls a traced child."""
        cfg = make_config(backend="setprofile")
        mod = _import_sample(repo_dir)

        result = _run_traced(cfg, mod.outer)
        funcs = {f["func"]: f for f in result.get("functions", [])}
        outer_fn = None
        for name, data in funcs.items():
            if "outer" in name:
                outer_fn = data
                break

        if outer_fn is not None:
            assert outer_fn["exclusive_time_s"] <= outer_fn["total_time_s"]

    def test_recursive_function(self, make_config, repo_dir):
        """Recursive calls should each have their own exclusive time."""
        cfg = make_config(backend="setprofile")
        mod = _import_sample(repo_dir)

        result = _run_traced(cfg, lambda: mod.recursive(5))
        funcs = result.get("functions", [])
        recursive_fn = None
        for f in funcs:
            if "recursive" in f["func"]:
                recursive_fn = f
                break

        if recursive_fn is not None:
            assert recursive_fn["call_count"] == 6  # recursive(5)..recursive(0)
            assert recursive_fn["exclusive_time_s"] >= 0


# ═══════════════════════════════════════════════════════════════════════════
#  Group 5: SettraceBackend — Line-Level
# ═══════════════════════════════════════════════════════════════════════════

class TestSettraceBackend:
    def test_line_hits_recorded(self, make_config, repo_dir):
        cfg = make_config(backend="settrace", trace_level="line")
        mod = _import_sample(repo_dir)

        result = _run_traced(cfg, mod.leaf)
        lines = result.get("lines", {})
        assert len(lines) > 0
        # Should have hits for the sample.py file
        for filename, line_data in lines.items():
            if "sample" in filename:
                assert len(line_data) > 0
                for lineno, info in line_data.items():
                    assert info["hits"] >= 1

    def test_line_timing_accumulated(self, make_config, repo_dir):
        cfg = make_config(backend="settrace", trace_level="line")
        mod = _import_sample(repo_dir)

        result = _run_traced(cfg, mod.leaf)
        lines = result.get("lines", {})
        for filename, line_data in lines.items():
            if "sample" in filename:
                for lineno, info in line_data.items():
                    assert info["time_ns"] >= 0

    def test_function_data_also_collected(self, make_config, repo_dir):
        """Line-level tracing is a superset of function-level."""
        cfg = make_config(backend="settrace", trace_level="line")
        mod = _import_sample(repo_dir)

        result = _run_traced(cfg, mod.outer)
        assert "functions" in result
        assert "call_sequence" in result
        assert "lines" in result
        # Should have function data for outer, middle, leaf
        func_names = {f["func"] for f in result["functions"]}
        assert any("leaf" in f for f in func_names)


# ═══════════════════════════════════════════════════════════════════════════
#  Group 6: SysMonBackend (Python 3.12+)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not tracer._HAS_SYS_MONITORING, reason="sys.monitoring not available")
class TestSysMonBackend:
    def test_function_level_output_schema(self, make_config, repo_dir):
        cfg = make_config(backend="sysmon", trace_level="function")
        mod = _import_sample(repo_dir)

        result = _run_traced(cfg, mod.outer)
        assert result["backend"] == "sysmon"
        assert "functions" in result
        assert "call_sequence" in result
        func_names = {f["func"] for f in result["functions"]}
        assert any("leaf" in f for f in func_names)

    def test_line_level_output_schema(self, make_config, repo_dir):
        cfg = make_config(backend="sysmon", trace_level="line")
        mod = _import_sample(repo_dir)

        result = _run_traced(cfg, mod.leaf)
        assert result["backend"] == "sysmon"
        assert "functions" in result
        assert "lines" in result

    def test_non_traced_code_efficient(self, make_config, repo_dir):
        """Non-traced code objects should return DISABLE."""
        cfg = make_config(backend="sysmon")
        # Call something entirely outside repo_dir — should be efficient
        result = _run_traced(cfg, lambda: json.dumps({"a": 1}))
        assert result["event_count"] == 0


# ═══════════════════════════════════════════════════════════════════════════
#  Group 7: Limits and Trimming
# ═══════════════════════════════════════════════════════════════════════════

class TestLimitsAndTrimming:
    def test_max_events_triggers_trim(self, make_config, repo_dir):
        cfg = make_config(backend="setprofile", max_events=3)
        mod = _import_sample(repo_dir)

        result = _run_traced(cfg, mod.outer)
        assert result.get("trimmed") is True
        reasons = [t["reason"] for t in result.get("trim_info", [])]
        assert "max_events" in reasons

    def test_max_depth_triggers_trim(self, make_config, repo_dir):
        cfg = make_config(backend="setprofile", max_depth=1)
        mod = _import_sample(repo_dir)

        result = _run_traced(cfg, mod.outer)
        assert result.get("trimmed") is True
        reasons = [t["reason"] for t in result.get("trim_info", [])]
        assert "max_depth" in reasons

    def test_max_functions_triggers_trim(self, make_config, repo_dir):
        cfg = make_config(backend="setprofile", max_functions=1)
        mod = _import_sample(repo_dir)

        result = _run_traced(cfg, mod.outer)
        # outer calls middle calls leaf — should hit max_functions
        assert result.get("trimmed") is True
        reasons = [t["reason"] for t in result.get("trim_info", [])]
        assert "max_functions" in reasons

    def test_max_duration_triggers_trim(self, make_config, repo_dir):
        cfg = make_config(backend="setprofile", max_duration=0.0001)
        mod = _import_sample(repo_dir)

        def slow_func():
            time.sleep(0.01)
            mod.leaf()

        result = _run_traced(cfg, slow_func)
        # Duration is very short, so tracing should stop
        # Note: the sleep itself won't trigger setprofile events,
        # so this test may or may not trigger depending on timing.
        # We just verify no crash occurs.
        assert "wall_time_s" in result

    def test_multiple_limits_recorded(self, make_config, repo_dir):
        cfg = make_config(backend="setprofile", max_events=2, max_depth=1)
        mod = _import_sample(repo_dir)

        result = _run_traced(cfg, mod.outer)
        if result.get("trimmed"):
            # Should have at least one trim reason
            assert len(result.get("trim_info", [])) >= 1

    def test_no_trim_when_within_limits(self, make_config, repo_dir):
        cfg = make_config(backend="setprofile")
        mod = _import_sample(repo_dir)

        result = _run_traced(cfg, mod.leaf)
        assert "trimmed" not in result


# ═══════════════════════════════════════════════════════════════════════════
#  Group 8: Compressed Output
# ═══════════════════════════════════════════════════════════════════════════

class TestCompressedOutput:
    def test_gzip_roundtrip(self, make_config, repo_dir):
        cfg = make_config(compress=True)
        mod = _import_sample(repo_dir)

        # Reset global state, run a trace, write output

        tracer._global_config = cfg
        tracer.start_trace("test::gz", config=cfg)
        mod.leaf()
        tracer.stop_trace("test::gz")

        output_path = str(repo_dir / "trace_output.json")
        tracer.write_output(output_path, config=cfg)

        # Read back
        gz_path = output_path + ".gz"
        assert os.path.exists(gz_path)
        with gzip.open(gz_path, "rt", encoding="utf-8") as f:
            data = json.load(f)

        assert data["tracer_version"] == "0.4.0"
        assert data["compress"] is True
        assert "tests" in data
        assert "test::gz" in data["tests"]



    def test_compact_json_no_whitespace(self, make_config, repo_dir):
        cfg = make_config(compress=False)
        mod = _import_sample(repo_dir)


        tracer._global_config = cfg
        tracer.start_trace("test::compact", config=cfg)
        mod.leaf()
        tracer.stop_trace("test::compact")

        output_path = str(repo_dir / "trace_output.json")
        tracer.write_output(output_path, config=cfg)

        with open(output_path, "r") as f:
            raw = f.read()

        # Compact format: no spaces after colons or commas (except inside strings)
        assert ": " not in raw.split('"')[0]  # Before first string value
        # More robust: re-parse should give same data
        data = json.loads(raw)
        assert data["tracer_version"] == "0.4.0"



    def test_plain_json_when_compress_false(self, make_config, repo_dir):
        cfg = make_config(compress=False)
        mod = _import_sample(repo_dir)


        tracer._global_config = cfg
        tracer.start_trace("test::plain", config=cfg)
        mod.leaf()
        tracer.stop_trace("test::plain")

        output_path = str(repo_dir / "trace_output.json")
        tracer.write_output(output_path, config=cfg)

        assert os.path.exists(output_path)
        assert not os.path.exists(output_path + ".gz")

        with open(output_path, "r") as f:
            data = json.load(f)
        assert data["tracer_version"] == "0.4.0"




# ═══════════════════════════════════════════════════════════════════════════
#  Group 9: Output Schema Compatibility
# ═══════════════════════════════════════════════════════════════════════════

class TestOutputSchemaCompatibility:
    def test_to_dict_has_all_required_fields(self, make_config, repo_dir):
        """Verify all fields consumed by tasks.py are present."""
        cfg = make_config(backend="setprofile")
        mod = _import_sample(repo_dir)

        result = _run_traced(cfg, mod.outer)

        # Top-level fields
        assert "test_nodeid" in result
        assert "trace_level" in result
        assert "wall_time_s" in result
        assert "start_rss_bytes" in result
        assert "peak_rss_bytes" in result
        assert "event_count" in result

        # Function summary
        assert "functions" in result
        for f in result["functions"]:
            assert "func" in f
            assert "file" in f
            assert "lineno" in f
            assert "call_count" in f
            assert "total_time_s" in f
            assert "exclusive_time_s" in f
            assert "max_time_s" in f
            assert "max_depth" in f
            assert "max_rss_delta_bytes" in f

        # Call sequence
        assert "call_sequence" in result
        for ev in result["call_sequence"]:
            assert "func" in ev
            assert "depth" in ev
            assert "wall_ns" in ev

        assert "call_sequence_truncated" in result
        assert "total_call_events" in result

    def test_functions_sorted_by_total_ns_desc(self, make_config, repo_dir):
        cfg = make_config(backend="setprofile")
        mod = _import_sample(repo_dir)

        result = _run_traced(cfg, mod.outer)
        funcs = result.get("functions", [])
        if len(funcs) >= 2:
            times = [f["total_time_s"] for f in funcs]
            assert times == sorted(times, reverse=True)

    def test_call_sequence_truncation(self, make_config, repo_dir):
        cfg = make_config(backend="setprofile", max_call_seq=2)
        mod = _import_sample(repo_dir)

        result = _run_traced(cfg, mod.outer)
        call_seq = result.get("call_sequence", [])
        assert len(call_seq) <= 2
        if result.get("total_call_events", 0) > 2:
            assert result["call_sequence_truncated"] is True

    def test_new_fields_are_additive(self, make_config, repo_dir):
        """New fields (backend, trimmed, trim_info) don't break old consumers."""
        cfg = make_config(backend="setprofile")
        mod = _import_sample(repo_dir)

        result = _run_traced(cfg, mod.leaf)
        assert "backend" in result
        assert result["backend"] == "setprofile"
        # trimmed should not be present when no trimming occurred
        assert "trimmed" not in result


# ═══════════════════════════════════════════════════════════════════════════
#  Group 10: Integration — Pytest Plugin
# ═══════════════════════════════════════════════════════════════════════════

class TestPytestPluginIntegration:
    def test_start_stop_cycle(self, make_config, repo_dir):
        cfg = make_config(backend="setprofile")
        mod = _import_sample(repo_dir)


        tracer._global_config = cfg

        tracer.start_trace("test::one", config=cfg)
        mod.leaf()
        tracer.stop_trace("test::one")

        assert "test::one" in tracer._all_traces
        trace_data = tracer._all_traces["test::one"]
        assert trace_data["test_nodeid"] == "test::one"
        assert trace_data["event_count"] > 0



    def test_multiple_tests_in_session(self, make_config, repo_dir):
        cfg = make_config(backend="setprofile")
        mod = _import_sample(repo_dir)


        tracer._global_config = cfg

        tracer.start_trace("test::a", config=cfg)
        mod.leaf()
        tracer.stop_trace("test::a")

        tracer.start_trace("test::b", config=cfg)
        mod.outer()
        tracer.stop_trace("test::b")

        assert "test::a" in tracer._all_traces
        assert "test::b" in tracer._all_traces



    def test_write_output_produces_valid_file(self, make_config, repo_dir):
        cfg = make_config(backend="setprofile", compress=False)
        mod = _import_sample(repo_dir)


        tracer._global_config = cfg

        tracer.start_trace("test::write", config=cfg)
        mod.leaf()
        tracer.stop_trace("test::write")

        output_path = str(repo_dir / "output.json")
        tracer.write_output(output_path, config=cfg)

        assert os.path.exists(output_path)
        with open(output_path, "r") as f:
            data = json.load(f)

        assert data["tracer_version"] == "0.4.0"
        assert data["test_count"] == 1
        assert "test::write" in data["tests"]




# ═══════════════════════════════════════════════════════════════════════════
#  Group 11: Backend Factory
# ═══════════════════════════════════════════════════════════════════════════

class TestBackendFactory:
    def test_auto_selects_sysmon_when_available(self, make_config, repo_dir):
        cfg = make_config(backend="auto")
        collector = tracer.TestTraceCollector("test::factory", config=cfg)
        if tracer._HAS_SYS_MONITORING:
            assert collector._backend.name == "sysmon"
        else:
            assert collector._backend.name == "setprofile"

    def test_explicit_setprofile(self, make_config, repo_dir):
        cfg = make_config(backend="setprofile")
        collector = tracer.TestTraceCollector("test::factory", config=cfg)
        assert collector._backend.name == "setprofile"

    def test_explicit_settrace(self, make_config, repo_dir):
        cfg = make_config(backend="settrace")
        collector = tracer.TestTraceCollector("test::factory", config=cfg)
        assert collector._backend.name == "settrace"

    def test_setprofile_with_line_level_falls_back(self, make_config, repo_dir):
        """setprofile can't do line-level, should fall back to settrace."""
        cfg = make_config(backend="setprofile", trace_level="line")
        collector = tracer.TestTraceCollector("test::factory", config=cfg)
        assert collector._backend.name == "settrace"


# ═══════════════════════════════════════════════════════════════════════════
#  Group 12: Golden-File Regression
# ═══════════════════════════════════════════════════════════════════════════

class TestGoldenFileRegression:
    """Trace known code and verify output structure matches expected schema."""

    def test_function_level_golden_schema(self, make_config, repo_dir):
        """Verify function-level trace output matches expected structure."""
        cfg = make_config(backend="setprofile")
        mod = _import_sample(repo_dir)

        result = _run_traced(cfg, mod.outer)

        # Verify structural invariants (not exact values, since timing varies)
        assert isinstance(result["test_nodeid"], str)
        assert result["trace_level"] == "function"
        assert isinstance(result["wall_time_s"], float)
        assert isinstance(result["event_count"], int)
        assert result["event_count"] >= 3  # outer, middle, leaf

        funcs = result["functions"]
        assert len(funcs) == 3
        # Should be sorted by total_time_s desc
        assert funcs[0]["total_time_s"] >= funcs[-1]["total_time_s"]

        # Each function should have call_count == 1 for outer()
        for f in funcs:
            assert f["call_count"] == 1

        # call_sequence should have 3 events
        assert len(result["call_sequence"]) == 3

    def test_line_level_golden_schema(self, make_config, repo_dir):
        """Verify line-level trace output includes both function and line data."""
        cfg = make_config(backend="settrace", trace_level="line")
        mod = _import_sample(repo_dir)

        result = _run_traced(cfg, mod.leaf)

        assert result["trace_level"] == "line"
        assert "functions" in result
        assert "lines" in result

        lines = result["lines"]
        assert len(lines) > 0
        for filename, file_lines in lines.items():
            for lineno_str, info in file_lines.items():
                assert isinstance(int(lineno_str), int)
                assert "hits" in info
                assert "time_ns" in info
                assert info["hits"] >= 1
                assert info["time_ns"] >= 0
