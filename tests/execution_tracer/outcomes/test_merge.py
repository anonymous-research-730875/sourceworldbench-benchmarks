"""Tests for `merge_outcomes_into_trace` (plain + gzip)."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

from sourceworldbench_benchmarks.execution_tracer.outcomes import merge_outcomes_into_trace


def _write_trace(path: Path, trace: dict) -> None:
    if path.suffix == ".gz":
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write(json.dumps(trace))
    else:
        path.write_text(json.dumps(trace), encoding="utf-8")


def _read_trace(path: Path) -> dict:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return json.load(handle)
    return json.loads(path.read_text())


def _sample_trace() -> dict:
    return {
        "tracer_version": "0.4.0",
        "tests": {
            "tests/t.py::test_a": {"functions": [], "outcome": None},
            "tests/t.py::test_b": {"functions": []},
            "tests/t.py::test_c": {"functions": []},
        },
    }


def test_merge_sets_outcome_field_per_test(tmp_path: Path) -> None:
    p = tmp_path / "trace.json"
    _write_trace(p, _sample_trace())

    merge_outcomes_into_trace(
        p, {"tests/t.py::test_a": "PASSED", "tests/t.py::test_b": "FAILED"}
    )

    trace = _read_trace(p)
    assert trace["tests"]["tests/t.py::test_a"]["outcome"] == "PASSED"
    assert trace["tests"]["tests/t.py::test_b"]["outcome"] == "FAILED"
    # Tests not in `outcomes` keep whatever they had (no implicit reset).
    assert "outcome" not in trace["tests"]["tests/t.py::test_c"]


def test_merge_handles_gzip_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "trace.json.gz"
    _write_trace(p, _sample_trace())

    merge_outcomes_into_trace(p, {"tests/t.py::test_a": "PASSED"})

    trace = _read_trace(p)
    assert trace["tests"]["tests/t.py::test_a"]["outcome"] == "PASSED"


def test_merge_no_write_back_keeps_disk_unchanged(tmp_path: Path) -> None:
    p = tmp_path / "trace.json"
    _write_trace(p, _sample_trace())
    original = p.read_text()

    merge_outcomes_into_trace(
        p, {"tests/t.py::test_a": "PASSED"}, write_back=False
    )

    assert p.read_text() == original


def test_merge_ignores_unknown_test_ids(tmp_path: Path) -> None:
    p = tmp_path / "trace.json"
    _write_trace(p, _sample_trace())

    merge_outcomes_into_trace(p, {"does/not/exist::test_x": "PASSED"})

    trace = _read_trace(p)
    assert trace["tests"].keys() == _sample_trace()["tests"].keys()
