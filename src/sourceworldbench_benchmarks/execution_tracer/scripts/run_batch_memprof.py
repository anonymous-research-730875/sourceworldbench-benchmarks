"""Drive a memory-only mining pass over the target instances.

Function-level per-call `tracemalloc.reset_peak` profiling. No time tracking,
no per-line. Output goes to its own directory (memprof_results/) — does not
overlap with the time-only runs in `walltime_results/` or `cprofile_results/`.

Usage:
    # Smoke test:
    uv run python -m sourceworldbench_benchmarks.execution_tracer.scripts.run_batch_memprof \
        --samples_dir benchmark_samples \
        --run_id memprof_smoke \
        --instance_ids psf__requests-1142 django__django-10554 \
        --workers 1

    # Full pass over 142 instances:
    uv run python -m sourceworldbench_benchmarks.execution_tracer.scripts.run_batch_memprof \
        --samples_dir benchmark_samples \
        --run_id memprof_results \
        --workers 2
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sourceworldbench_benchmarks.execution_tracer.legacy_swebench_harness.run_memprof import main as run_memprof_main


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
    p.add_argument("--memprof_output_dir", default="./memprof_results")
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
    p.add_argument(
        "--only_f2p", action=argparse.BooleanOptionalAction, default=False,
        help=("Pytest-only: restrict the in-container pytest invocation to "
              "the dataset's FAIL_TO_PASS test ids. ~50-1000x speedup on "
              "heavy test files. The per-test profiler output for the F2P "
              "tests is unchanged."))
    args = p.parse_args()

    if args.instance_ids:
        ids = args.instance_ids
    else:
        ids = discover_instance_ids(Path(args.samples_dir))
    print(f"Resolved {len(ids)} instance_ids "
          f"(first 5: {ids[:5]}{'...' if len(ids) > 5 else ''})",
          flush=True)

    run_memprof_main(
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
        memprof_output_dir=args.memprof_output_dir,
        dual=args.dual,
        only_f2p=args.only_f2p,
    )


if __name__ == "__main__":
    sys.exit(main())
