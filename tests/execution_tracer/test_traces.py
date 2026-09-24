"""Tests for trace-merging utilities."""

from __future__ import annotations

import json
from pathlib import Path

from sourceworldbench_benchmarks.execution_tracer.traces import merge_trace_outputs


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _side_dir(tmp_path: Path) -> Path:
    d = tmp_path / "inst" / "base"
    d.mkdir(parents=True)
    return d


def test_merge_copies_tracemalloc_test_fields(tmp_path: Path) -> None:
    d = _side_dir(tmp_path)
    _write(d / "trace_output.json", {
        "tests": {"t::test_a": {"outcome": "passed", "functions": []}}
    })
    _write(d / "memprof_output.json", {
        "tests": {"t::test_a": {
            "start_traced_bytes": 100, "peak_traced_bytes": 500,
            "start_rss_bytes": 1000, "peak_rss_bytes": 2000,
            "outcome": "passed", "functions": {},
        }}
    })
    merge_trace_outputs(d)
    t = _read(d / "trace_output.json")["tests"]["t::test_a"]
    assert t["start_traced_bytes"] == 100
    assert t["peak_traced_bytes"] == 500
    # start_rss_bytes already present in trace — should NOT be overwritten
    # (only the tracemalloc fields missing from trace are added)


def test_merge_adds_exclusive_alloc_bytes_to_functions(tmp_path: Path) -> None:
    d = _side_dir(tmp_path)
    _write(d / "trace_output.json", {
        "tests": {"t::test_a": {
            "outcome": "passed",
            "functions": [
                {"func": "mymod.foo", "exclusive_time_s": 0.01},
                {"func": "mymod.bar", "exclusive_time_s": 0.005},
            ],
        }}
    })
    _write(d / "memprof_output.json", {
        "tests": {"t::test_a": {
            "start_traced_bytes": 0, "peak_traced_bytes": 0,
            "start_rss_bytes": 0, "peak_rss_bytes": 0,
            "outcome": "passed",
            "functions": {
                "mymod.foo": {"exclusive_alloc_peak_sum": 4096, "exclusive_rss_delta_sum": 0},
                "mymod.bar": {"exclusive_alloc_peak_sum": 0,    "exclusive_rss_delta_sum": 8192},
            },
        }}
    })
    merge_trace_outputs(d)
    funcs = {f["func"]: f for f in
             _read(d / "trace_output.json")["tests"]["t::test_a"]["functions"]}
    assert funcs["mymod.foo"]["exclusive_alloc_bytes"] == 4096
    assert funcs["mymod.bar"]["exclusive_alloc_bytes"] == 0


def test_merge_overrides_wall_time_from_walltime_output(tmp_path: Path) -> None:
    d = _side_dir(tmp_path)
    _write(d / "trace_output.json", {
        "tests": {"t::test_a": {"wall_time_s": 0.1, "functions": []}}
    })
    _write(d / "walltime_output.json", {
        "tests": {"t::test_a": {"wall_time_s": 0.08, "outcome": "passed"}}
    })
    merge_trace_outputs(d)
    t = _read(d / "trace_output.json")["tests"]["t::test_a"]
    assert t["wall_time_s"] == 0.08


def test_merge_tolerates_missing_auxiliary_files(tmp_path: Path) -> None:
    d = _side_dir(tmp_path)
    _write(d / "trace_output.json", {
        "tests": {"t::test_a": {"outcome": "passed", "functions": []}}
    })
    # No walltime or memprof files — should not raise.
    n = merge_trace_outputs(d)
    assert n == 0


def test_merge_overwrites_exclusive_time_from_cprofile(tmp_path: Path) -> None:
    d = _side_dir(tmp_path)
    _write(d / "trace_output.json", {
        "tests": {"t::test_a": {
            "wall_time_s": 0.1,
            "functions": [
                {"func": "mymod.foo", "exclusive_time_s": 0.05},
                {"func": "mymod.bar", "exclusive_time_s": 0.03},
            ],
        }}
    })
    _write(d / "cprofile_output.json", {
        "tests": {"t::test_a": {
            "wall_time_s": 0.09,
            "outcome": "passed",
            "functions": {
                "mymod.foo": {"call_count": 1, "exclusive_time_s": 0.042, "inclusive_time_s": 0.042},
                # mymod.bar intentionally absent — trace value should be kept
            },
        }}
    })
    merge_trace_outputs(d)
    funcs = {f["func"]: f for f in
             _read(d / "trace_output.json")["tests"]["t::test_a"]["functions"]}
    assert funcs["mymod.foo"]["exclusive_time_s"] == 0.042   # overwritten by cprofile
    assert funcs["mymod.bar"]["exclusive_time_s"] == 0.03    # kept from trace


def test_merge_raises_when_trace_missing(tmp_path: Path) -> None:
    import pytest
    d = _side_dir(tmp_path)
    with pytest.raises(FileNotFoundError):
        merge_trace_outputs(d)
