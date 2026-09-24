"""Build three HuggingFace datasets from the dl4c trace data.

Datasets produced
-----------------
1. sourceworldbench-benchmarks-dl4c-environments  (~217 rows, one per instance × side)
2. sourceworldbench-benchmarks-dl4c-benchmark     (435 rows, one per benchmark sample)
3. sourceworldbench-benchmarks-dl4c-traces        (435 rows, one per benchmark sample)

Usage
-----
    uv run python sourceworldbench_benchmarks.execution_tracer/scripts/build_hf_datasets.py \\
        --data_dir data/dl4c \\
        --out_dir data/hf_datasets

To also push to the Hub:
    uv run python sourceworldbench_benchmarks.execution_tracer/scripts/build_hf_datasets.py \\
        --data_dir data/dl4c \\
        --out_dir data/hf_datasets \\
        --push_to_hub \\
        --hub_org anonymous-research-730875
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path

from datasets import Dataset
from datasets import load_dataset as hf_load_dataset
from swebench.harness.test_spec.test_spec import make_test_spec

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> dict:
    if path.suffix == ".gz":
        with gzip.open(path) as f:
            return json.load(f)
    return json.loads(path.read_text())


def _extract_pytest_command(eval_script: str) -> str:
    """Pull the test invocation out of the SWEbench eval shell script.

    The eval script marks the test section with:
        : '>>>>> Start Test Output'
        <command>
        : '>>>>> End Test Output'

    We return all non-empty, non-marker lines between those sentinels joined
    with ' && ', which covers both single-command (pytest) and multi-command
    (Django runtests.py) cases.
    """
    lines = eval_script.splitlines()
    in_test_section = False
    commands: list[str] = []
    for line in lines:
        stripped = line.strip()
        if ">>>>> Start Test Output" in stripped:
            in_test_section = True
            continue
        if ">>>>> End Test Output" in stripped:
            in_test_section = False
            break
        if in_test_section and stripped:
            commands.append(stripped)
    return " && ".join(commands)


def _load_swebench_index(instance_ids: list[str]) -> dict[str, dict]:
    """Load SWEbench Verified rows for the given instances, keyed by instance_id."""
    print("Loading SWE-bench/SWE-bench_Verified …", flush=True)
    ds = hf_load_dataset("SWE-bench/SWE-bench_Verified", split="test")
    keep = set(instance_ids)
    return {row["instance_id"]: dict(row) for row in ds if row["instance_id"] in keep}


# ---------------------------------------------------------------------------
# Dataset 1 – environments
# ---------------------------------------------------------------------------

def build_environments(
    samples_dir: Path,
    traces_dir: Path,
    swebench: dict[str, dict],
) -> list[dict]:
    """One row per (instance_id, side) that appears in the sample set."""

    # ---- collect sample metadata ----------------------------------------
    # env_key -> {tests_pass: [], tests_fail: []}
    env_tests: dict[str, dict[str, list[str]]] = {}
    instance_meta: dict[str, dict] = {}  # instance_id -> first sample metadata

    for fpath in sorted(samples_dir.glob("*.json")):
        s = _load_json(fpath)
        iid = s["instance_id"]
        side = s["metadata"]["side"]
        env_key = f"{iid}::{side}"

        if iid not in instance_meta:
            instance_meta[iid] = {
                "repo": s["repo"],
                "base_commit": s["base_commit"],
            }
        env_tests.setdefault(env_key, {"tests_pass": [], "tests_fail": []})

        # Outcome from ground truth (the single traced test)
        outcome = s["ground_truth"]["outcome"]["outcome"]
        test_nodeid = s["test_nodeid"]
        bucket = "tests_pass" if outcome == "passed" else "tests_fail"
        if test_nodeid not in env_tests[env_key][bucket]:
            env_tests[env_key][bucket].append(test_nodeid)

    # ---- build rows -------------------------------------------------------
    rows: list[dict] = []
    for env_key, test_buckets in sorted(env_tests.items()):
        iid, side = env_key.rsplit("::", 1)
        sw = swebench.get(iid, {})
        meta = instance_meta[iid]

        # Container + command via make_test_spec (needs the SWEbench row dict)
        container = ""
        command = ""
        if sw:
            try:
                spec = make_test_spec(sw)
                container = spec.instance_image_key
                command = _extract_pytest_command(spec.eval_script)
            except Exception as e:
                print(f"  WARN: make_test_spec failed for {iid}: {e}")

        rows.append(
            {
                "env_id": env_key,
                "instance_id": iid,
                "side": side,
                "repo": meta["repo"],
                "base_commit": meta["base_commit"],
                # patch is the fixing diff; only present in post environment
                "patch": sw.get("patch", "") if side == "post" else "",
                "test_patch": sw.get("test_patch", ""),
                "container": container,
                "command": command,
                "tests_pass": sorted(test_buckets["tests_pass"]),
                "tests_fail": sorted(test_buckets["tests_fail"]),
            }
        )

    return rows


# ---------------------------------------------------------------------------
# Dataset 2 – benchmark
# ---------------------------------------------------------------------------

def _read_prompt(prompts_dir: Path, sample_id: str) -> str:
    """Return the user prompt text for the given sample_id."""
    # manifest maps sample_id -> relative path like prompts/foo.txt
    manifest_path = prompts_dir / "manifest.tsv"
    if not manifest_path.exists():
        return ""
    # Build lookup on first call (lazy cache via attribute)
    if not hasattr(_read_prompt, "_cache"):
        _read_prompt._cache = {}  # type: ignore[attr-defined]
        for line in manifest_path.read_text().splitlines()[1:]:
            parts = line.split("\t")
            if len(parts) >= 2:
                _read_prompt._cache[parts[0]] = parts[1]  # type: ignore[attr-defined]

    rel = _read_prompt._cache.get(sample_id, "")  # type: ignore[attr-defined]
    if not rel:
        return ""
    prompt_file = prompts_dir / rel
    return prompt_file.read_text() if prompt_file.exists() else ""


def build_benchmark(
    samples_dir: Path,
    prompts_dir: Path,
) -> list[dict]:
    """One row per benchmark sample (435 rows)."""
    rows: list[dict] = []

    for fpath in sorted(samples_dir.glob("*.json")):
        s = _load_json(fpath)
        iid = s["instance_id"]
        side = s["metadata"]["side"]
        gt = s["ground_truth"]
        outcome_block = gt.get("outcome", {})

        user_prompt = _read_prompt(prompts_dir, s["sample_id"])
        # Fall back to embedded prompt if the file isn't available
        if not user_prompt:
            user_prompt = s.get("user_prompt", "")

        rows.append(
            {
                "sample_id": s["sample_id"],
                "env_id": f"{iid}::{side}",
                "instance_id": iid,
                "test_nodeid": s["test_nodeid"],
                "repo": s["repo"],
                "base_commit": s["base_commit"],
                "side": side,
                "task": s["task"],
                # Prompts
                "system_prompt": s.get("system_prompt", ""),
                "user_prompt": user_prompt,
                # Ground truth – flattened for ergonomics
                "ground_truth_outcome": outcome_block.get("outcome", ""),
                "ground_truth_failure_line": outcome_block.get("failure_line"),
                "ground_truth_exception_type": outcome_block.get("exception_type"),
                "ground_truth_peak_bytes": int(gt.get("peak_bytes", 0)),
                "ground_truth_wall_ms": float(gt.get("wall_ms", 0.0)),
                "ground_truth_hot_methods_time": gt.get("hot_methods_time", []),
                "ground_truth_hot_methods_alloc": gt.get("hot_methods_alloc", []),
                "ground_truth_hot_lines_time": gt.get("hot_lines_time", []),
                "ground_truth_hot_lines_alloc": gt.get("hot_lines_alloc", []),
                # Raw per-item measurements used to derive the ranked lists above.
                # Each is a JSON object mapping name → numeric value.
                # hot_methods_*: method qualified name → time_s or alloc_bytes
                # hot_lines_*:   "file:line" → time_ns or alloc_bytes
                "values_hot_methods_time": json.dumps(
                    s.get("metric_args", {}).get("values_by_subtask", {}).get("hot_methods_time", {})
                ),
                "values_hot_methods_alloc": json.dumps(
                    s.get("metric_args", {}).get("values_by_subtask", {}).get("hot_methods_alloc", {})
                ),
                "values_hot_lines_time": json.dumps(
                    s.get("metric_args", {}).get("values_by_subtask", {}).get("hot_lines_time", {})
                ),
                "values_hot_lines_alloc": json.dumps(
                    s.get("metric_args", {}).get("values_by_subtask", {}).get("hot_lines_alloc", {})
                ),
                # Metadata
                "wall_time_s": float(s["metadata"].get("wall_time_s", 0.0)),
                "peak_rss_bytes": int(s["metadata"].get("peak_rss_bytes", 0)),
                "peak_traced_bytes": int(s["metadata"].get("peak_traced_bytes", 0)),
            }
        )

    return rows


# ---------------------------------------------------------------------------
# Dataset 3 – traces
# ---------------------------------------------------------------------------

def _find_trace_file(trace_instance_dir: Path, side: str) -> Path | None:
    """Return trace_output[_pre].json[.gz] for the given side."""
    suffix = "_pre" if side == "pre" else ""
    for ext in (".json.gz", ".json"):
        p = trace_instance_dir / f"trace_output{suffix}{ext}"
        if p.exists():
            return p
    return None


def _get_test_data(trace_data: dict, test_nodeid: str) -> dict | None:
    """Extract the per-test dict from trace output.

    Trace outputs keyed by exact test_nodeid OR by the sentinel 'session' (for
    some Django/sympy traces that bundle results under a single key).
    """
    tests = trace_data.get("tests", {})
    if test_nodeid in tests:
        return tests[test_nodeid]
    if "session" in tests:
        return tests["session"]
    # Try prefix match (parametrized test IDs can differ slightly)
    for key, val in tests.items():
        if key != "session" and test_nodeid.startswith(key.split("[")[0]):
            return val
    # Return first available test
    if tests:
        return next(iter(tests.values()))
    return None


def build_traces(
    samples_dir: Path,
    traces_dir: Path,
) -> list[dict]:
    """One row per benchmark sample (435 rows), with raw trace data."""
    rows: list[dict] = []
    missing_traces = 0

    for fpath in sorted(samples_dir.glob("*.json")):
        s = _load_json(fpath)
        iid = s["instance_id"]
        side = s["metadata"]["side"]
        test_nodeid = s["test_nodeid"]

        # Locate trace directory for this instance
        trace_inst_dir = traces_dir / f"trace_single_{iid}" / iid
        trace_file = _find_trace_file(trace_inst_dir, side) if trace_inst_dir.exists() else None

        # Load snapshot manifest
        manifest: dict = {}
        manifest_path = trace_inst_dir / "snapshot_manifest.json" if trace_inst_dir.exists() else None
        if manifest_path and manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())

        # Defaults for when trace file is missing
        test_data: dict = {}
        global_meta: dict = {}

        if trace_file:
            try:
                trace_data = _load_json(trace_file)
                global_meta = {k: trace_data[k] for k in (
                    "tracer_version", "trace_level", "memory_tracking",
                    "backend", "python_version",
                ) if k in trace_data}
                td = _get_test_data(trace_data, test_nodeid)
                if td:
                    test_data = td
            except Exception as e:
                print(f"  WARN: could not load trace for {iid}/{side}: {e}")
                missing_traces += 1
        else:
            missing_traces += 1

        rows.append(
            {
                "sample_id": s["sample_id"],
                "env_id": f"{iid}::{side}",
                "instance_id": iid,
                "test_nodeid": test_nodeid,
                "side": side,
                # Tracer global metadata
                "tracer_version": global_meta.get("tracer_version", ""),
                "trace_level": global_meta.get("trace_level", ""),
                "memory_tracking": global_meta.get("memory_tracking", ""),
                "backend": global_meta.get("backend", ""),
                "python_version": global_meta.get("python_version", ""),
                # Per-test execution stats
                "outcome": test_data.get("outcome", ""),
                "wall_time_s": float(test_data.get("wall_time_s", 0.0)),
                "setup_time_s": float(test_data.get("setup_time_s", 0.0)),
                "call_time_s": float(test_data.get("call_time_s", 0.0)),
                "teardown_time_s": float(test_data.get("teardown_time_s", 0.0)),
                "start_rss_bytes": int(test_data.get("start_rss_bytes", 0)),
                "peak_rss_bytes": int(test_data.get("peak_rss_bytes", 0)),
                "start_traced_bytes": int(test_data.get("start_traced_bytes", 0)),
                "peak_traced_bytes": int(test_data.get("peak_traced_bytes", 0)),
                "event_count": int(test_data.get("event_count", 0)),
                "total_call_events": int(test_data.get("total_call_events", 0)),
                "call_sequence_truncated": bool(test_data.get("call_sequence_truncated", False)),
                # Profiling data as JSON strings
                "functions": json.dumps(test_data.get("functions", [])),
                "lines": json.dumps(test_data.get("lines", {})),
                "call_sequence": json.dumps(test_data.get("call_sequence", [])),
                "session_outcomes": json.dumps(test_data.get("session_outcomes", {})),
                # Snapshot metadata
                "traced_files": manifest.get("traced_files", []),
                "test_files": manifest.get("test_files", []),
                "neighbor_files": manifest.get("neighbor_files", []),
                "test_patch_applied": bool(manifest.get("test_patch_applied", False)),
            }
        )

    if missing_traces:
        print(f"  NOTE: {missing_traces} samples had no trace file available")
    return rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data_dir", default="data/dl4c",
                        help="Root data directory containing samples/, prompts/, traces/")
    parser.add_argument("--out_dir", default="data/hf_datasets",
                        help="Output directory for saved HF datasets")
    parser.add_argument("--push_to_hub", action="store_true",
                        help="Push datasets to HuggingFace Hub after building")
    parser.add_argument("--hub_org", default="anonymous-research-730875",
                        help="HuggingFace organization for push_to_hub")
    parser.add_argument("--skip_swebench", action="store_true",
                        help="Skip SWE-bench API calls (leaves patch/container/command empty)")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    samples_dir = data_dir / "samples"
    prompts_dir = data_dir / "prompts"
    traces_dir = data_dir / "traces"
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Collect instance IDs from samples
    sample_files = sorted(samples_dir.glob("*.json"))
    if not sample_files:
        sys.exit(f"No sample files found in {samples_dir}")
    print(f"Found {len(sample_files)} sample files")

    instance_ids: list[str] = []
    seen: set[str] = set()
    for fpath in sample_files:
        iid = fpath.stem.split("::")[0]
        if iid not in seen:
            instance_ids.append(iid)
            seen.add(iid)
    print(f"Unique instance IDs: {len(instance_ids)}")

    # Load SWEbench data
    swebench: dict[str, dict] = {}
    if not args.skip_swebench:
        swebench = _load_swebench_index(instance_ids)
        print(f"Loaded SWEbench data for {len(swebench)} instances")
    else:
        print("Skipping SWEbench API calls")

    # -----------------------------------------------------------------------
    # Dataset 1: environments
    # -----------------------------------------------------------------------
    print("\n[1/3] Building environments dataset …")
    env_rows = build_environments(samples_dir, traces_dir, swebench)
    print(f"  → {len(env_rows)} environment rows")

    env_ds = Dataset.from_list(env_rows)
    env_path = out_dir / "environments"
    env_ds.save_to_disk(str(env_path))
    print(f"  → Saved to {env_path}")

    # -----------------------------------------------------------------------
    # Dataset 2: benchmark
    # -----------------------------------------------------------------------
    print("\n[2/3] Building benchmark dataset …")
    bench_rows = build_benchmark(samples_dir, prompts_dir)
    print(f"  → {len(bench_rows)} benchmark rows")

    bench_ds = Dataset.from_list(bench_rows)
    bench_path = out_dir / "benchmark"
    bench_ds.save_to_disk(str(bench_path))
    print(f"  → Saved to {bench_path}")

    # -----------------------------------------------------------------------
    # Dataset 3: traces
    # -----------------------------------------------------------------------
    print("\n[3/3] Building traces dataset …")
    trace_rows = build_traces(samples_dir, traces_dir)
    print(f"  → {len(trace_rows)} trace rows")

    traces_ds = Dataset.from_list(trace_rows)
    traces_path = out_dir / "traces"
    traces_ds.save_to_disk(str(traces_path))
    print(f"  → Saved to {traces_path}")

    # -----------------------------------------------------------------------
    # Push to Hub
    # -----------------------------------------------------------------------
    if args.push_to_hub:
        org = args.hub_org
        print(f"\nPushing to HuggingFace Hub (org: {org}) …")
        env_ds.push_to_hub(f"{org}/sourceworldbench-benchmarks-dl4c-environments")
        bench_ds.push_to_hub(f"{org}/sourceworldbench-benchmarks-dl4c-benchmark")
        traces_ds.push_to_hub(f"{org}/sourceworldbench-benchmarks-dl4c-traces")
        print("Done.")

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    print("\n=== Summary ===")
    print(f"environments : {len(env_ds)} rows, {env_ds.column_names}")
    print(f"benchmark    : {len(bench_ds)} rows, {bench_ds.column_names}")
    print(f"traces       : {len(traces_ds)} rows, {traces_ds.column_names}")
    print(f"\nDatasets saved to: {out_dir.resolve()}")


if __name__ == "__main__":
    main()
