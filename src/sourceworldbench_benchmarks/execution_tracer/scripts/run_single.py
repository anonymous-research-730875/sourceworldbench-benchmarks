"""
run_single.py — Quick test script to run tracing on a single SWE-bench instance.

Usage:
    uv run python -m sourceworldbench_benchmarks.execution_tracer.scripts.run_single \
        --instance_id django__django-16379 \
        --trace_level function
"""

from __future__ import annotations

import argparse
import json

from swebench.harness.utils import load_swebench_dataset


def pick_instance(dataset_name: str = "SWE-bench/SWE-bench_Verified", split: str = "test"):
    """Show some instances to help pick one for testing."""
    dataset = load_swebench_dataset(dataset_name, split)
    print(f"Dataset: {dataset_name}, split: {split}, total: {len(dataset)} instances\n")

    # Group by repo
    repos = {}
    for item in dataset:
        repo = item["repo"]
        repos.setdefault(repo, []).append(item["instance_id"])

    print("Repos and instance counts:")
    for repo, ids in sorted(repos.items(), key=lambda x: -len(x[1])):
        print(f"  {repo}: {len(ids)} instances")
        # Show first 3 instance IDs
        for iid in ids[:3]:
            print(f"    - {iid}")

    return dataset


def show_instance_detail(dataset_name: str, split: str, instance_id: str):
    """Show details for a specific instance."""
    dataset = load_swebench_dataset(dataset_name, split, [instance_id])
    if not dataset:
        print(f"Instance {instance_id} not found!")
        return None

    inst = dataset[0]
    print(f"\n{'='*80}")
    print(f"Instance: {inst['instance_id']}")
    print(f"Repo: {inst['repo']}")
    print(f"Version: {inst.get('version', 'N/A')}")
    print(f"Base commit: {inst['base_commit'][:12]}")
    print("\nProblem statement (first 500 chars):")
    print(inst.get("problem_statement", "N/A")[:500])
    print(f"\nFAIL_TO_PASS: {inst.get('FAIL_TO_PASS', '[]')}")
    print(f"PASS_TO_PASS count: {len(json.loads(inst.get('PASS_TO_PASS', '[]')))}")
    print("\nTest patch (first 500 chars):")
    print(inst.get("test_patch", "N/A")[:500])
    print("\nGold patch (first 500 chars):")
    print(inst.get("patch", "N/A")[:500])
    print(f"{'='*80}")

    return inst


def run_traced_single(
    instance_id: str,
    dataset_name: str = "SWE-bench/SWE-bench_Verified",
    split: str = "test",
    trace_level: str = "line",
    memory_tracking: str = "both",
    trace_output_dir: str = "./trace_results",
    timeout: int = 600,
    compress: bool = True,
    dual_trace: bool = False,
    pre_only: bool = False,
):
    """Run traced evaluation on a single instance using the gold patch."""
    from sourceworldbench_benchmarks.execution_tracer.legacy_swebench_harness.run_traced import main as run_traced_main

    run_traced_main(
        dataset_name=dataset_name,
        split=split,
        instance_ids=[instance_id],
        predictions_path="gold",
        max_workers=1,
        force_rebuild=False,
        cache_level="env",
        clean=False,
        open_file_limit=4096,
        run_id=f"trace_single_{instance_id}",
        timeout=timeout,
        namespace="swebench",
        trace_level=trace_level,
        memory_tracking=memory_tracking,
        trace_output_dir=trace_output_dir,
        compress=compress,
        dual_trace=dual_trace,
        pre_only=pre_only,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true", help="List available instances")
    parser.add_argument("--instance_id", type=str, help="Instance ID to run")
    parser.add_argument("--show", type=str, help="Show details for an instance ID")
    parser.add_argument("--dataset_name", type=str, default="SWE-bench/SWE-bench_Verified")
    parser.add_argument("--split", type=str, default="test")
    parser.add_argument("--trace_level", type=str, default="line", choices=["function", "line"])
    parser.add_argument("--memory_tracking", type=str, default="both",
                        choices=["rss", "tracemalloc", "both"])
    parser.add_argument("--trace_output_dir", type=str, default="./trace_results")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--compress", action=argparse.BooleanOptionalAction, default=True,
                       help="Enable gzip compression for trace output")
    parser.add_argument("--dual_trace", action=argparse.BooleanOptionalAction, default=False,
                       help="Collect pre-patch trace in addition to post-patch")
    parser.add_argument("--pre_only", action=argparse.BooleanOptionalAction, default=False,
                       help="Run only pre-patch trace, skip post-patch + grading")

    args = parser.parse_args()

    if args.list:
        pick_instance(args.dataset_name, args.split)
    elif args.show:
        show_instance_detail(args.dataset_name, args.split, args.show)
    elif args.instance_id:
        run_traced_single(
            args.instance_id,
            dataset_name=args.dataset_name,
            split=args.split,
            trace_level=args.trace_level,
            memory_tracking=args.memory_tracking,
            trace_output_dir=args.trace_output_dir,
            timeout=args.timeout,
            compress=args.compress,
            dual_trace=args.dual_trace,
            pre_only=args.pre_only,
        )
    else:
        parser.print_help()
