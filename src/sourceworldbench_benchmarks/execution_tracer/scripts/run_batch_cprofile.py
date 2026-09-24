"""Drive a cProfile mining pass over the target instances.

Walks `benchmark_samples/combined/*.json`, derives the 142
unique instance_ids, then runs the cProfile-based harness on each.
Dual mode (pre + post in one container) is on by default.

Output:
    cprofile_results/<run_id>/<instance_id>/cprofile_output{,_pre}.json

Resumable: instances whose outputs already exist are skipped.

Usage:
    # Smoke test:
    uv run python -m sourceworldbench_benchmarks.execution_tracer.scripts.run_batch_cprofile \
        --samples_dir benchmark_samples \
        --run_id cprof_smoke \
        --instance_ids psf__requests-1142 django__django-10554 \
        --workers 1

    # Full pass:
    uv run python -m sourceworldbench_benchmarks.execution_tracer.scripts.run_batch_cprofile \
        --samples_dir benchmark_samples \
        --run_id cprofile_results \
        --workers 2
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sourceworldbench_benchmarks.execution_tracer.legacy_swebench_harness.run_cprofile import main as run_cprofile_main


def discover_instance_ids(samples_dir: Path) -> list[str]:
    combined = samples_dir / "combined"
    if not combined.is_dir():
        raise SystemExit(f"No combined/ subdir under {samples_dir}")
    ids = set()
    for f in combined.glob("*.json"):
        ids.add(f.name.split("::", 1)[0])
    return sorted(ids)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--samples_dir", default="benchmark_samples")
    p.add_argument("--instance_ids", nargs="+", default=None)
    p.add_argument("--run_id", required=True)
    p.add_argument("--profile_output_dir", default="./cprofile_results")
    p.add_argument("--dataset_name", default="SWE-bench/SWE-bench_Verified")
    p.add_argument("--split", default="test")
    p.add_argument("--predictions_path", default="gold")
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--timeout", type=int, default=900)
    p.add_argument("--dual", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--namespace", default="swebench")
    p.add_argument("--cache_level", default="env",
                   choices=["none", "base", "env", "instance"])
    p.add_argument("--force_rebuild", action=argparse.BooleanOptionalAction,
                   default=False)
    p.add_argument("--clean", action=argparse.BooleanOptionalAction, default=False)
    args = p.parse_args()

    if args.instance_ids:
        ids = args.instance_ids
    else:
        ids = discover_instance_ids(Path(args.samples_dir))
    print(f"Resolved {len(ids)} instance_ids "
          f"(first 5: {ids[:5]}{'...' if len(ids) > 5 else ''})",
          flush=True)

    run_cprofile_main(
        dataset_name=args.dataset_name,
        split=args.split,
        instance_ids=ids,
        predictions_path=args.predictions_path,
        max_workers=args.workers,
        force_rebuild=args.force_rebuild,
        cache_level=args.cache_level,
        clean=args.clean,
        open_file_limit=4096,
        run_id=args.run_id,
        timeout=args.timeout,
        namespace=args.namespace,
        profile_output_dir=args.profile_output_dir,
        dual=args.dual,
    )


if __name__ == "__main__":
    sys.exit(main())
