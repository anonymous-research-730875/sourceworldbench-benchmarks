"""Merge an `{test_id: outcome}` map into a trace JSON in-place.

The benchmark builder reads `trace["tests"][nodeid]["outcome"]` to
populate `BenchmarkSample.metadata.outcome` and to filter eligibility.
The legacy harness merged outcomes (extracted from swebench's
`report.json`) into the trace JSON immediately after the run; this
module replicates that step over the unified `OutcomeProvider` shape.

Trace JSONs may be plain `.json` or gzip-compressed `.json.gz`; both
are handled here.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path


def _read_trace(path: Path) -> dict:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return json.load(handle)
    return json.loads(path.read_text(encoding="utf-8"))


def _write_trace(path: Path, trace: dict) -> None:
    data = json.dumps(trace, separators=(",", ":"))
    if path.suffix == ".gz":
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write(data)
    else:
        path.write_text(data, encoding="utf-8")


def merge_outcomes_into_trace(
    trace_path: Path,
    outcomes: dict[str, str],
    *,
    write_back: bool = True,
) -> dict:
    """Set `trace["tests"][nodeid]["outcome"]` for each matching id.

    Returns the (possibly modified) trace dict. When `write_back=True`
    (the default), the merged trace is written back to `trace_path` in
    the same on-disk format. Test ids not present in the trace are
    ignored; tests present in the trace but not in `outcomes` are left
    untouched.
    """
    trace = _read_trace(trace_path)
    tests = trace.get("tests")
    if not isinstance(tests, dict):
        return trace

    for nodeid, outcome in outcomes.items():
        entry = tests.get(nodeid)
        if isinstance(entry, dict):
            entry["outcome"] = outcome

    if write_back:
        _write_trace(trace_path, trace)
    return trace
