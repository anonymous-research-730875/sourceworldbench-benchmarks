"""Regression tests for the memprof tracer (`memory_test_profiler.py`).

The tracer is loaded straight from its source file, the way the runner ships
it, and driven through `_start_test`/`_stop_test` with `sys.setprofile` (no
Docker, no pytest plugin). `SWEBENCH_REPO_DIR` points at this directory so
the in-project filter accepts the synthetic functions below.

The `rss` measure reads `/proc/self/statm`, so its tests run on Linux only.
"""

import importlib.util
import json
import os
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
TRACER_SOURCE = REPO_ROOT / "src/sourceworldbench_benchmarks/execution_tracer/tracers/memory_test_profiler.py"
MB = 1 << 20

linux_only = pytest.mark.skipif(not Path("/proc/self/statm").exists(), reason="needs Linux procfs")


def _load_profiler(tmp_out_path: Path, measure: str = "traced"):
    """Load a fresh copy of the tracer configured through env vars for this test."""
    here = Path(__file__).resolve().parent
    os.environ["SWEBENCH_MEM_OUTPUT"] = str(tmp_out_path)
    os.environ["SWEBENCH_MEM_MODE"] = "manual"  # don't activate pytest hook
    os.environ["SWEBENCH_MEM_MEASURE"] = measure
    os.environ["SWEBENCH_REPO_DIR"] = str(here)
    os.environ["SWEBENCH_MEM_PATHS"] = ""  # accept anything under repo dir
    spec = importlib.util.spec_from_file_location("memory_test_profiler_under_test", TRACER_SOURCE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _drive(mod, body, test_id="t"):
    """Run `body()` under the profiler and return the per-test record."""
    mod._start_test(test_id)
    try:
        body()
    finally:
        mod._stop_test(test_id, outcome="passed")
    return mod._RESULTS[test_id]


def _function(rec, name):
    return next(f for key, f in rec["functions"].items() if key.endswith(name))


# ── helpers we call from the body so they qualify as in-project frames ──

def _alloc_then_free_8mb():
    # ~8 MB Python list of small ints — definitely visible to tracemalloc.
    x = [0] * (1 << 21)
    del x


def _alloc_then_free_160mb():
    # One zero-filled buffer: touched pages, so RSS sees it too. Larger than
    # any other allocation in this module: `ru_maxrss` is process-wide and
    # never resets, so an exact RSS peak needs a new process maximum.
    x = bytearray(160 * MB)
    del x


def _alloc_hold_then_free_160mb():
    # Sampled RSS peaks resolve one sampling gap (~1 ms): a buffer freed the
    # instant its memset ends is only partly seen, one that stays resident
    # while it is used, like any real transient, is caught in full.
    x = bytearray(160 * MB)
    time.sleep(0.01)
    del x


def _small_work():
    # A trivial in-project frame that allocates very little — used to be
    # the trigger that wiped the prior peak via reset_peak() on entry.
    return sum(range(100))


def _parent_transient_then_small_call():
    x = bytearray(64 * MB)
    del x
    _small_work()


def _child_transient_32mb():
    x = bytearray(32 * MB)
    del x


def _parent_transient_then_two_children():
    x = bytearray(64 * MB)
    del x
    _child_transient_32mb()
    _child_transient_32mb()


def _wrap(*fns):
    def body():
        for f in fns:
            f()
    return body


# ── tests ───────────────────────────────────────────────────────────────


def test_finished_test_is_on_disk_before_process_exit(tmp_path):
    """A kill during a later test must not lose tests that already finished."""
    out_path = tmp_path / "memprof_output.json"
    mod = _load_profiler(out_path)
    _drive(mod, _wrap(_small_work), test_id="light")

    tests = json.loads(out_path.read_text())["tests"]
    assert tests["light"]["outcome"] == "passed"
    assert not list(tmp_path.glob("*.tmp"))


def test_transient_peak_survives_subsequent_small_call(tmp_path):
    """v0.1.0 bug: 8 MB transient is wiped by the next call's reset_peak.

    Sequence: project method A allocates+frees 8 MB; project method B
    runs trivial work; test ends. The test-level peak MUST reflect A's
    transient — i.e. `peak_traced_bytes - start_traced_bytes` must be
    >= 8 MB (with a small tolerance for Python list overhead).
    """
    mod = _load_profiler(tmp_path / "memprof_output.json")
    rec = _drive(mod, _wrap(_alloc_then_free_8mb, _small_work))

    delta_traced = rec["peak_traced_bytes"] - rec["start_traced_bytes"]
    # ~8 MB of Python ints. Conservative lower bound: 4 MB.
    assert delta_traced >= 4_000_000, (
        f"peak_traced delta only {delta_traced} bytes — "
        f"the 8 MB transient was lost. start={rec['start_traced_bytes']} "
        f"peak={rec['peak_traced_bytes']}"
    )


def test_test_level_peak_dominates_any_single_method(tmp_path):
    """For every recorded method, the test-level peak above start must
    be >= that method's inclusive_alloc_peak_max. The 0.1.0 build
    violated this on ~18% of real samples; the fix should make it hold
    in all cases."""
    mod = _load_profiler(tmp_path / "memprof_output.json")
    # Two methods, each peaks at >= 8 MB transient. Test-level peak
    # should be >= either one. The 0.1.0 implementation would record
    # ~0 because the last reset_peak (at the 2nd method's entry)
    # destroyed the high-water mark.
    rec = _drive(mod, _wrap(_alloc_then_free_8mb, _alloc_then_free_8mb))
    delta_traced = rec["peak_traced_bytes"] - rec["start_traced_bytes"]

    for name, f in rec["functions"].items():
        method_peak = f.get("inclusive_alloc_peak_max", 0)
        assert delta_traced >= method_peak, (
            f"test-level peak {delta_traced} < method {name!r} peak "
            f"{method_peak} — per-test counter is being reset under us"
        )


def test_per_method_peak_still_works(tmp_path):
    """The fix must NOT break the per-method peak accounting (that part
    was correct in 0.1.0)."""
    mod = _load_profiler(tmp_path / "memprof_output.json")
    rec = _drive(mod, _wrap(_alloc_then_free_8mb))

    f = _function(rec, "_alloc_then_free_8mb")
    assert f["call_count"] == 1
    assert f["inclusive_alloc_peak_max"] >= 4_000_000
    assert f["exclusive_alloc_peak_max"] >= 4_000_000
    # Delta should be ~0 because we freed before return.
    assert f["inclusive_alloc_delta_max"] < 1_000_000


def test_nested_call_peak_attribution(tmp_path):
    """Parent that calls a child which allocates+frees should:
      - parent inclusive_alloc_peak_max >= child's peak
      - parent exclusive_alloc_peak_max << child's peak (child credited)
      - test-level peak >= child's peak
    """
    mod = _load_profiler(tmp_path / "memprof_output.json")

    def _parent():
        _alloc_then_free_8mb()

    rec = _drive(mod, _parent)

    delta_traced = rec["peak_traced_bytes"] - rec["start_traced_bytes"]
    child = _function(rec, "_alloc_then_free_8mb")
    parent = _function(rec, "_parent")

    assert child["inclusive_alloc_peak_max"] >= 4_000_000
    assert parent["inclusive_alloc_peak_max"] >= child["inclusive_alloc_peak_max"]
    # Parent's own body did very little — its exclusive should be tiny.
    assert parent["exclusive_alloc_peak_max"] < 500_000, (
        f"parent exclusive_peak={parent['exclusive_alloc_peak_max']} — "
        f"child not credited correctly"
    )
    assert delta_traced >= child["inclusive_alloc_peak_max"]


def test_parent_transient_before_a_child_call_is_kept(tmp_path):
    """0.3.x lost a parent's own transient once a callee reset the peak counter."""
    mod = _load_profiler(tmp_path / "memprof_output.json")
    rec = _drive(mod, _parent_transient_then_small_call)

    parent = _function(rec, "_parent_transient_then_small_call")
    assert parent["inclusive_alloc_peak_max"] >= 60 * MB
    assert parent["exclusive_alloc_peak_max"] >= 60 * MB


def test_exclusive_peak_ignores_children_transients(tmp_path):
    """Two 32 MB callees must neither add up nor be subtracted from the parent's own 64 MB peak."""
    mod = _load_profiler(tmp_path / "memprof_output.json")
    rec = _drive(mod, _parent_transient_then_two_children)

    parent = _function(rec, "_parent_transient_then_two_children")
    child = _function(rec, "_child_transient_32mb")
    assert 60 * MB <= parent["inclusive_alloc_peak_max"] < 70 * MB
    assert 60 * MB <= parent["exclusive_alloc_peak_max"] < 70 * MB
    assert 30 * MB <= child["inclusive_alloc_peak_max"] < 40 * MB


@linux_only
def test_rss_peak_captures_transient_freed_before_return(tmp_path):
    mod = _load_profiler(tmp_path / "memrss_output.json", measure="rss")
    rec = _drive(mod, _wrap(_alloc_hold_then_free_160mb))

    assert rec["rss_peak_exact"] is True
    assert rec["peak_rss_bytes"] - rec["start_rss_bytes"] >= 150 * MB
    assert rec["rss_peak_sampled_bytes"] >= rec["start_rss_bytes"] + 150 * MB
    f = _function(rec, "_alloc_hold_then_free_160mb")
    assert f["inclusive_rss_delta_max"] < 8 * MB
    assert f["inclusive_rss_peak_max"] >= 150 * MB
    assert f["exclusive_rss_peak_max"] >= 150 * MB


def _parent_of_160mb_transient():
    _alloc_hold_then_free_160mb()


@linux_only
def test_rss_nested_transient_is_credited_to_the_callee(tmp_path):
    mod = _load_profiler(tmp_path / "memrss_output.json", measure="rss")
    rec = _drive(mod, _parent_of_160mb_transient)

    child = _function(rec, "_alloc_hold_then_free_160mb")
    parent = _function(rec, "_parent_of_160mb_transient")
    assert child["inclusive_rss_peak_max"] >= 150 * MB
    assert parent["inclusive_rss_peak_max"] >= child["inclusive_rss_peak_max"]
    assert parent["exclusive_rss_peak_max"] < 16 * MB


@linux_only
def test_rss_peak_below_an_earlier_maximum_is_flagged_inexact(tmp_path):
    """`ru_maxrss` never resets, so a smaller later test cannot get an exact peak."""
    mod = _load_profiler(tmp_path / "memrss_output.json", measure="rss")
    _drive(mod, _wrap(_alloc_then_free_160mb), test_id="first")
    rec = _drive(mod, _wrap(_alloc_then_free_8mb), test_id="second")

    assert rec["rss_peak_exact"] is False
    assert rec["peak_rss_bytes"] - rec["start_rss_bytes"] < 32 * MB


def _retain_64mb(sink):
    sink.append(bytearray(64 * MB))


@linux_only
def test_rss_delta_of_a_retaining_function_is_seen(tmp_path):
    """A stale statm read (same descriptor, no regeneration) would report a zero delta."""
    mod = _load_profiler(tmp_path / "memrss_output.json", measure="rss")
    sink = []
    rec = _drive(mod, lambda: _retain_64mb(sink))

    assert _function(rec, "_retain_64mb")["inclusive_rss_delta_max"] >= 60 * MB


@linux_only
def test_rss_read_in_a_forked_child_is_the_childs_own(tmp_path):
    """`/proc/self` is resolved at open time, so the child must reopen it after fork."""
    mod = _load_profiler(tmp_path / "memrss_output.json", measure="rss")
    read_end, write_end = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_end)
        _held = bytearray(256 * MB)
        os.write(write_end, str(mod._get_rss()).encode())
        os._exit(0)
    os.close(write_end)
    child_rss = int(os.read(read_end, 64))
    os.waitpid(pid, 0)

    assert child_rss - mod._get_rss() >= 200 * MB


def _keep_every_other_of_3000_64kb_blocks(keep):
    # 64 KB blocks live on the glibc heap (too big for pymalloc, below the
    # mmap threshold); keeping every other one stops the freed ones from
    # coalescing into a trimmable top chunk, so they stay resident.
    blocks = [bytes(64 * 1024) for _ in range(3000)]
    keep.extend(blocks[::2])
    del blocks


def _keep_1500_64kb_blocks(sink):
    sink.extend(bytes(64 * 1024) for _ in range(1500))


@linux_only
def test_rss_baseline_excludes_memory_freed_by_an_earlier_test(tmp_path):
    """Blocks freed by the first test would be reused by the second without moving RSS."""
    mod = _load_profiler(tmp_path / "memrss_output.json", measure="rss")
    keep, sink = [], []
    _drive(mod, lambda: _keep_every_other_of_3000_64kb_blocks(keep), test_id="first")
    rec = _drive(mod, lambda: _keep_1500_64kb_blocks(sink), test_id="second")

    assert rec["peak_rss_bytes"] - rec["start_rss_bytes"] >= 70 * MB
