"""Utilities for merging per-tracer output files into a single trace JSON.

Each tracer pass writes its own output file to `<traces>/<instance>/<side>/`:

  trace_output.json    — outcome, per-function timing + RSS, line-level events
  walltime_output.json — clean per-test wall_time_s (no settrace overhead)
  memprof_output.json  — per-test tracemalloc peaks + per-function alloc data
  cprofile_output.json — per-function exclusive_time_s via cProfile (more
                         reliable than sys.settrace timing: setprofile fires
                         only at function entry/exit, far lower overhead)

`merge_trace_outputs` enriches `trace_output.json` in-place:

  • `wall_time_s` per test          ← walltime (cleanest; no profiler)
  • `exclusive_time_s` per function ← cprofile (overwrites trace value;
                                      lower measurement overhead)
  • `start_traced_bytes` /
    `peak_traced_bytes` per test    ← memprof
  • `exclusive_alloc_bytes`
    per function                    ← memprof's exclusive_alloc_peak_sum

After merging, `trace_output.json` is the only file `build-samples` needs.
"""

from __future__ import annotations

import gzip
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_MEMPROF_TEST_FIELDS = (
    "start_traced_bytes",
    "peak_traced_bytes",
)


def _read(path: Path) -> dict:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, data: dict) -> None:
    if path.suffix == ".gz":
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            fh.write(json.dumps(data))
    else:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _find(side_dir: Path, stem: str) -> Path | None:
    for name in (f"{stem}.json", f"{stem}.json.gz"):
        p = side_dir / name
        if p.exists():
            return p
    return None


def merge_trace_outputs(side_dir: Path) -> int:
    """Merge walltime + cprofile + memprof data into trace_output.json.

    Returns the number of tests enriched. Raises `FileNotFoundError` if
    `trace_output.json` is absent; silently skips any missing auxiliary file.
    """
    trace_path = _find(side_dir, "trace_output")
    if trace_path is None:
        raise FileNotFoundError(f"trace_output.json not found in {side_dir}")

    trace = _read(trace_path)
    tests: dict = trace.get("tests") or {}
    enriched = 0

    # ── walltime ──────────────────────────────────────────────────────────────
    # Clean per-test wall_time_s: perf_counter with no profiler attached.
    # Overrides the trace's own wall_time_s which carries settrace overhead.
    wt_path = _find(side_dir, "walltime_output")
    if wt_path is not None:
        wt = _read(wt_path)
        for nodeid, wt_test in (wt.get("tests") or {}).items():
            if nodeid in tests and wt_test.get("wall_time_s") is not None:
                tests[nodeid]["wall_time_s"] = wt_test["wall_time_s"]

    # ── cprofile ──────────────────────────────────────────────────────────────
    # Per-function exclusive_time_s via cProfile (sys.setprofile). Lower
    # per-event overhead than sys.settrace, so timing is more reliable.
    # Overwrites the trace's own exclusive_time_s for matched functions.
    cp_path = _find(side_dir, "cprofile_output")
    if cp_path is not None:
        cp = _read(cp_path)
        for nodeid, cp_test in (cp.get("tests") or {}).items():
            if nodeid not in tests:
                continue
            cp_funcs: dict = cp_test.get("functions") or {}
            for f in tests[nodeid].get("functions") or []:
                fname = f.get("func")
                if fname and fname in cp_funcs:
                    cf = cp_funcs[fname]
                    if cf.get("exclusive_time_s") is not None:
                        f["exclusive_time_s"] = cf["exclusive_time_s"]

    # ── memprof ───────────────────────────────────────────────────────────────
    mp_path = _find(side_dir, "memprof_output")
    if mp_path is not None:
        mp = _read(mp_path)
        for nodeid, mp_test in (mp.get("tests") or {}).items():
            if nodeid not in tests:
                continue
            t = tests[nodeid]

            # Test-level tracemalloc baseline + peak (RSS already in trace).
            for field in _MEMPROF_TEST_FIELDS:
                if mp_test.get(field) is not None:
                    t[field] = mp_test[field]

            # Per-function alloc: add exclusive_alloc_bytes to each entry in
            # the trace's functions list, keyed by the func name string.
            # exclusive_alloc_peak_sum catches transient alloc-then-free patterns.
            mp_funcs: dict = mp_test.get("functions") or {}
            for f in t.get("functions") or []:
                fname = f.get("func")
                if fname and fname in mp_funcs:
                    mf = mp_funcs[fname]
                    f["exclusive_alloc_bytes"] = mf.get("exclusive_alloc_peak_sum") or 0

            enriched += 1

    _write(trace_path, trace)
    return enriched
