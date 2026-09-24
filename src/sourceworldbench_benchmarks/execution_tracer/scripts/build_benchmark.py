"""CLI: build benchmark samples from collected traces.

Usage:
    uv run python -m sourceworldbench_benchmarks.execution_tracer.scripts.build_benchmark \
        --trace_dir ./trace_results \
        --out_dir ./benchmark_samples \
        [--instance_ids ... ] [--tasks outcome_pass_fail hot_methods_time]
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from datasets import load_dataset

from sourceworldbench_benchmarks.execution_tracer.benchmark.builder import (
    build_samples_for_trace,
)
from sourceworldbench_benchmarks.execution_tracer.benchmark.samples import TASKS, write_sample


def _find_trace_dirs(trace_root: Path) -> list[Path]:
    """Each instance dir is .../<run_id>/<instance_id>/. We accept any such layout."""
    out = []
    for first in sorted(trace_root.iterdir()):
        if not first.is_dir():
            continue
        # If first looks like an instance dir (contains trace_output*) pick it
        if any(p.name.startswith("trace_output") for p in first.iterdir() if p.is_file()):
            out.append(first)
            continue
        for second in sorted(first.iterdir()):
            if not second.is_dir():
                continue
            if any(p.name.startswith("trace_output")
                   for p in second.iterdir() if p.is_file()):
                out.append(second)
    return out


def _load_dataset_index(dataset_name: str, split: str) -> dict:
    print(f"Loading dataset {dataset_name} (split={split})...")
    ds = load_dataset(dataset_name, split=split)
    return {row["instance_id"]: row for row in ds}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace_dir", required=True, type=str,
                        help="Directory containing traced instances")
    parser.add_argument("--out_dir", required=True, type=str,
                        help="Output directory for benchmark samples")
    parser.add_argument("--instance_ids", nargs="+",
                        help="Restrict to these instance ids")
    parser.add_argument("--tasks", nargs="+", choices=TASKS,
                        help="Subset of tasks to build (default: all)")
    parser.add_argument("--dataset_name", default="SWE-bench/SWE-bench_Verified")
    parser.add_argument("--split", default="test")
    parser.add_argument("--max_instances", type=int, default=None)
    parser.add_argument("--context_strategy", default="smart",
                        choices=["smart", "oracle"],
                        help="How to slice source files. 'smart' (default) "
                             "AST-guides essentials + fills with siblings; "
                             "'oracle' legacy per-file truncate.")
    args = parser.parse_args()

    trace_root = Path(args.trace_dir)
    out_root = Path(args.out_dir)
    out_root.mkdir(parents=True, exist_ok=True)

    instance_dirs = _find_trace_dirs(trace_root)
    if args.instance_ids:
        keep = set(args.instance_ids)
        instance_dirs = [d for d in instance_dirs if d.name in keep]
    if args.max_instances:
        instance_dirs = instance_dirs[:args.max_instances]
    print(f"Found {len(instance_dirs)} instance trace directories")

    # Index the dataset for instance metadata
    ds_index = _load_dataset_index(args.dataset_name, args.split)

    counters = Counter()
    instances_with_samples = set()
    total_written = 0

    def _build_side(d: Path, meta: dict, side: str):
        """Build samples for one side (post / pre). Returns (count, source_kind).

        For pre samples we pass [pre_dir, post_dir] so the builder reads the
        gold-patch reverse-applied (or GitHub-fetched) file from pre_dir for
        files the patch touched, and falls back to post_dir for everything
        else (unchanged between pre and post). For post we pass just the
        post snapshot. NEVER serve post-patch code as pre — the model would
        otherwise see the fixed source while predicting buggy runtime
        behavior.
        """
        post_snap = d / "repo_snapshot"
        pre_snap = d / "repo_snapshot_pre"
        if side == "post":
            for ext in (".json.gz", ".json"):
                trace_path = d / f"trace_output{ext}"
                if trace_path.exists():
                    break
            else:
                return 0, "no_trace"
            snap: Path | list[Path] = post_snap
            if not post_snap.exists():
                return 0, "no_post_snap"
        else:  # pre
            for ext in (".json.gz", ".json"):
                trace_path = d / f"trace_output_pre{ext}"
                if trace_path.exists():
                    break
            else:
                return 0, "no_trace"
            if not pre_snap.exists():
                # No pre-patch snapshot at all — refusing to build pre
                # samples from a post snapshot would yield wrong data.
                return 0, "no_pre_snap"
            snap = [pre_snap, post_snap] if post_snap.exists() else [pre_snap]
        if not trace_path.exists():
            return 0, "no_trace"
        try:
            samples = build_samples_for_trace(
                trace_path, snap, meta, tasks=args.tasks, side=side,
                context_strategy=args.context_strategy,
            )
        except Exception as e:
            print(f"  ERROR building {side} samples for {meta.get('instance_id', d.name)}: {e}")
            return 0, "exception"
        for s in samples:
            write_sample(s, out_root)
            counters[s.task] += 1
        return len(samples), "ok"

    skipped_no_pre_snap = []
    for d in instance_dirs:
        instance_id = d.name
        meta = ds_index.get(instance_id)
        if meta is None:
            print(f"  WARN: {instance_id} not in dataset, skipping")
            continue
        n_post, post_status = _build_side(d, meta, "post")
        n_pre, pre_status = _build_side(d, meta, "pre")
        if pre_status == "no_pre_snap":
            skipped_no_pre_snap.append(instance_id)
        n = n_post + n_pre
        total_written += n
        if n:
            instances_with_samples.add(instance_id)
        print(f"  {instance_id}: post={n_post}, pre={n_pre}")

    # Manifest
    manifest = {
        "dataset_name": args.dataset_name,
        "split": args.split,
        "context_strategy": args.context_strategy,
        "tasks": {t: {"count": counters[t]} for t in TASKS if counters[t]},
        "total_samples": total_written,
        "instances_covered": len(instances_with_samples),
        "instances_skipped_no_pre_snap": skipped_no_pre_snap,
    }
    (out_root / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print()
    if skipped_no_pre_snap:
        print(f"NOTE: {len(skipped_no_pre_snap)} instance(s) had no "
              f"repo_snapshot_pre/ — their pre samples were SKIPPED rather "
              f"than served stale post-patch source. Re-run "
              f"reconstruct_pre_snapshots.py to populate them.")
    print(f"Total samples written: {total_written}")
    for t, c in counters.most_common():
        print(f"  {t}: {c}")


if __name__ == "__main__":
    main()
