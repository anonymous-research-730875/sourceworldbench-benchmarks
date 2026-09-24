"""
run_batch_traced.py — Pull Docker images and run tracing on SWE-bench instances.

Handles the pull → trace → cleanup cycle one instance at a time to manage disk space.
Skips instances that already have trace data + repo snapshots.

Usage:
    # Run all remaining pytest-based instances
    uv run python -m sourceworldbench_benchmarks.execution_tracer.scripts.run_batch_traced \
        --mode pytest --max_instances 50

    # Run all remaining Django instances
    uv run python -m sourceworldbench_benchmarks.execution_tracer.scripts.run_batch_traced \
        --mode django --max_instances 50

    # Run specific instances
    uv run python -m sourceworldbench_benchmarks.execution_tracer.scripts.run_batch_traced \
        --instance_ids sphinx-doc__sphinx-10323 pytest-dev__pytest-10051

    # Dry run: show what would be run
    uv run python -m sourceworldbench_benchmarks.execution_tracer.scripts.run_batch_traced --dry_run
"""

from __future__ import annotations

import argparse
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from swebench.harness.test_spec.test_spec import make_test_spec
from swebench.harness.utils import load_swebench_dataset

# Force unbuffered output
os.environ["PYTHONUNBUFFERED"] = "1"


def _print(*args, **kwargs):
    """Print with flush."""
    kwargs.setdefault("flush", True)
    print(*args, **kwargs)

BROKEN_REPOS: set[str] = set()  # all 12 repos now supported
TRACE_OUTPUT_DIR = "./trace_results"


def get_traced_instances(trace_output_dir: str, pre_only: bool = False) -> set[str]:
    """Find instances that already have trace data.

    In normal mode: requires trace_output.* + repo_snapshot/.
    In pre_only mode: requires trace_output_pre.* (snapshot reuse is OK).
    """
    traced = set()
    trace_path = Path(trace_output_dir)
    if not trace_path.exists():
        return traced
    for d in trace_path.iterdir():
        if not d.is_dir():
            continue
        for inst_dir in d.iterdir():
            if not inst_dir.is_dir():
                continue
            if pre_only:
                if (inst_dir / "trace_output_pre.json").exists() \
                        or (inst_dir / "trace_output_pre.json.gz").exists():
                    traced.add(inst_dir.name)
            else:
                has_trace = (
                    (inst_dir / "trace_output.json").exists()
                    or (inst_dir / "trace_output.json.gz").exists()
                )
                snapshot_dir = inst_dir / "repo_snapshot"
                if has_trace and snapshot_dir.is_dir():
                    traced.add(inst_dir.name)
    return traced


def get_remaining_instances(
    dataset_name: str,
    split: str,
    trace_output_dir: str,
    mode: str = "all",
    instance_ids: list[str] | None = None,
    pre_only: bool = False,
) -> list[dict]:
    """Get instances that still need tracing."""
    ds = load_swebench_dataset(dataset_name, split)
    already_traced = get_traced_instances(trace_output_dir, pre_only=pre_only)

    instances = []
    for item in ds:
        iid = item["instance_id"]
        repo = item["repo"]

        if repo in BROKEN_REPOS:
            continue
        if iid in already_traced:
            continue
        if instance_ids and iid not in instance_ids:
            continue

        is_django = repo == "django/django"
        is_sympy = repo == "sympy/sympy"
        is_session_trace = is_django or is_sympy

        if mode == "pytest" and is_session_trace:
            continue
        if mode == "django" and not is_django:
            continue
        if mode == "sympy" and not is_sympy:
            continue
        if mode == "session" and not is_session_trace:
            continue

        spec = make_test_spec(item, namespace="swebench")
        instances.append({
            "instance_id": iid,
            "repo": repo,
            "image_key": spec.instance_image_key,
            "is_session_trace": is_session_trace,
        })

    instances.sort(key=lambda x: (x["is_session_trace"], x["repo"], x["instance_id"]))
    return instances


_RATE_LIMITED = threading.Event()  # set when Docker Hub returns rate-limit error


def pull_image(image_key: str, timeout: int = 300) -> bool:
    """Pull a Docker image. Returns True on success.

    If Docker Hub responds with "pull rate limit" the global _RATE_LIMITED
    event is set so the main loop can short-circuit further attempts; the
    user can wait ~6h for the limit to reset and resume (the runner's
    resume logic will skip already-traced instances).
    """
    if _RATE_LIMITED.is_set():
        return False
    try:
        result = subprocess.run(
            ["docker", "pull", image_key],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode == 0:
            return True
        err = result.stderr.strip()
        if "pull rate limit" in err.lower():
            _RATE_LIMITED.set()
        _print(f"  Pull failed: {err[-200:]}")
        return False
    except subprocess.TimeoutExpired:
        _print(f"  Pull timed out after {timeout}s")
        return False
    except Exception as e:
        _print(f"  Pull error: {e}")
        return False


def _cleanup_gold_reports(run_dir: Path, instance_id: str):
    """Move gold.*.json reports from cwd into run_dir to keep root clean."""
    run_dir.mkdir(parents=True, exist_ok=True)
    for f in Path(".").glob(f"gold.*{instance_id}*.json"):
        f.rename(run_dir / f.name)


def run_trace(
    instance_id: str, trace_output_dir: str, timeout: int = 600,
    compress: bool = True, dual_trace: bool = False,
    trace_level: str = "line", memory_tracking: str = "both",
    pre_only: bool = False,
) -> bool:
    """Run tracing on a single instance. Returns True on success."""
    try:
        abs_trace_dir = str(Path(trace_output_dir).resolve())
        cmd = [
            "uv", "run", "python", "-m", "sourceworldbench_benchmarks.execution_tracer.scripts.run_single",
            "--instance_id", instance_id,
            "--trace_level", trace_level,
            "--memory_tracking", memory_tracking,
            "--trace_output_dir", abs_trace_dir,
            "--timeout", str(timeout),
        ]
        if not compress:
            cmd.append("--no-compress")
        if dual_trace:
            cmd.append("--dual_trace")
        if pre_only:
            cmd.append("--pre_only")
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=timeout + 120,
        )
        # Check for success indicators in stdout
        if "Traced: 1/1" in result.stdout or "traced=1" in result.stdout:
            return True
        # Check if trace file was created on disk (authoritative check).
        # SWE-bench's make_run_report() may print "Instances with errors: 1"
        # due to container cleanup issues even when tracing succeeded.
        trace_dir = Path(trace_output_dir)
        target_files = (
            ("trace_output_pre.json", "trace_output_pre.json.gz")
            if pre_only
            else ("trace_output.json", "trace_output.json.gz")
        )
        for d in trace_dir.iterdir():
            inst_dir = d / instance_id
            if inst_dir.is_dir() and any(
                (inst_dir / name).exists() for name in target_files
            ):
                return True
        # No trace file found — report error details if available
        if "error=1" in result.stdout or "Instances with errors: 1" in result.stdout:
            _print("  Trace error in output")
            for line in result.stdout.strip().split("\n")[-5:]:
                _print(f"    {line}")
            return False
        _print("  No trace output found")
        return False
    except subprocess.TimeoutExpired:
        _print(f"  Trace timed out after {timeout + 120}s")
        return False
    except Exception as e:
        _print(f"  Trace error: {e}")
        return False


def cleanup_image(image_key: str):
    """Remove a Docker image to free space."""
    try:
        subprocess.run(
            ["docker", "rmi", "-f", image_key],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        pass


def _prune_docker_dangling():
    """Reclaim Docker VM disk by pruning dangling images, stopped containers, build cache.

    Safe to call concurrently — only removes resources not in use. Cheap (~1s)
    when nothing to clean. Critical for keeping the Docker VM disk from filling
    up when running matplotlib's 2-3 GB images.
    """
    try:
        subprocess.run(
            ["docker", "container", "prune", "-f"],
            capture_output=True, text=True, timeout=20,
        )
        subprocess.run(
            ["docker", "image", "prune", "-f"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description="Batch trace SWE-bench instances")
    parser.add_argument("--mode", choices=["pytest", "django", "sympy", "session", "all"], default="all")
    parser.add_argument("--dataset_name", default="SWE-bench/SWE-bench_Verified")
    parser.add_argument("--split", default="test")
    parser.add_argument("--trace_output_dir", default=TRACE_OUTPUT_DIR)
    parser.add_argument("--instance_ids", nargs="+")
    parser.add_argument("--max_instances", type=int, default=None)
    parser.add_argument("--max_workers", type=int, default=1,
                        help="Number of parallel mining workers")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--pull_timeout", type=int, default=300)
    parser.add_argument("--dry_run", action="store_true")
    parser.add_argument("--keep_images", action="store_true",
                       help="Don't remove Docker images after tracing")
    parser.add_argument("--compress", action=argparse.BooleanOptionalAction, default=True,
                       help="Enable gzip compression for trace output")
    parser.add_argument("--dual_trace", action=argparse.BooleanOptionalAction, default=False,
                       help="Collect pre-patch trace in addition to post-patch")
    parser.add_argument("--pre_only", action=argparse.BooleanOptionalAction, default=False,
                       help="Run only pre-patch trace; skip patch+post-run+grading. "
                            "Resume detection looks for trace_output_pre.* — instances "
                            "without pre-patch traces will be processed; instances with "
                            "them will be skipped.")
    parser.add_argument("--trace_level", type=str, default="line",
                       choices=["function", "line"],
                       help="Trace level (line is required for line-level hotspot tasks)")
    parser.add_argument("--memory_tracking", type=str, default="both",
                       choices=["rss", "tracemalloc", "both"],
                       help="Memory tracking mode (tracemalloc/both required for allocation tasks)")
    args = parser.parse_args()

    _print("Finding remaining instances...")
    instances = get_remaining_instances(
        args.dataset_name, args.split, args.trace_output_dir,
        mode=args.mode, instance_ids=args.instance_ids,
        pre_only=args.pre_only,
    )

    if args.max_instances:
        instances = instances[:args.max_instances]

    _print(f"Instances to process: {len(instances)}")
    pytest_count = sum(1 for i in instances if not i["is_session_trace"])
    session_count = sum(1 for i in instances if i["is_session_trace"])
    _print(f"  Pytest-based: {pytest_count}")
    _print(f"  Session-trace (Django/sympy): {session_count}")

    if args.dry_run:
        _print("\nDry run — instances that would be processed:")
        for inst in instances:
            _print(f"  {inst['instance_id']} ({inst['repo']})")
        return

    stats = {"success": 0, "pull_fail": 0, "trace_fail": 0, "total": len(instances)}
    stats_lock = threading.Lock()
    print_lock = threading.Lock()
    pull_lock = threading.Lock()  # serialize pulls so concurrent extracts of
                                  # shared base layers don't race in containerd
                                  # (observed as "lchown: no such file" errors
                                  # when 3+ matplotlib images pulled in parallel)
    start_time = time.time()
    completed = [0]

    def _process_one(idx, inst):
        iid = inst["instance_id"]
        image = inst["image_key"]
        if _RATE_LIMITED.is_set():
            with stats_lock:
                stats["pull_fail"] += 1
            return
        with print_lock:
            _print(f"[start {idx}/{len(instances)}] {iid}")

        # NOTE: pre-pull pruning was removed because concurrent pruning while
        # other workers were mid-pull triggered containerd "lease does not
        # exist" errors. Post-trace cleanup still calls _prune_docker_dangling().

        # Step 1: pull — held under pull_lock so only one image extracts at a
        # time. This sidesteps a containerd race when concurrent pulls share
        # base layers (matplotlib images: ~3 GB each, common conda layers).
        with pull_lock:
            ok = pull_image(image, timeout=args.pull_timeout)
        if not ok:
            with stats_lock:
                stats["pull_fail"] += 1
            with print_lock:
                _print(f"  [{iid}] SKIP: image pull failed")
            return

        # Step 2: trace
        trace_start = time.time()
        success = run_trace(
            iid, args.trace_output_dir, timeout=args.timeout,
            compress=args.compress, dual_trace=args.dual_trace,
            trace_level=args.trace_level, memory_tracking=args.memory_tracking,
            pre_only=args.pre_only,
        )
        trace_time = time.time() - trace_start

        # Move gold.*.json out of root
        _cleanup_gold_reports(Path(args.trace_output_dir) / "_swebench_reports", iid)

        with stats_lock:
            if success:
                stats["success"] += 1
                tag = "OK"
            else:
                stats["trace_fail"] += 1
                tag = "FAIL"
            completed[0] += 1
            done = completed[0]
            elapsed = time.time() - start_time
            rate = done / elapsed * 60 if elapsed > 0 else 0
            remaining = len(instances) - done
            eta_min = (elapsed / done * remaining) / 60 if done > 0 else 0

        with print_lock:
            _print(f"  [{iid}] {tag} ({trace_time:.0f}s) "
                   f"-- {done}/{len(instances)} done, "
                   f"{stats['success']} ok / {stats['trace_fail']} fail / "
                   f"{stats['pull_fail']} pullfail, "
                   f"{rate:.2f}/min, ETA {eta_min:.0f}min")

        # Cleanup image + dangling resources to free Docker VM disk
        if not args.keep_images:
            cleanup_image(image)
            _prune_docker_dangling()

    with ThreadPoolExecutor(max_workers=args.max_workers) as ex:
        futures = [ex.submit(_process_one, i + 1, inst)
                   for i, inst in enumerate(instances)]
        for f in as_completed(futures):
            try:
                f.result()
            except Exception as e:
                _print(f"  Worker exception: {e}")

    total_time = time.time() - start_time
    _print(f"\n{'='*60}")
    _print(f"BATCH COMPLETE ({total_time/60:.1f} min, {args.max_workers} workers)")
    _print(f"  Success: {stats['success']}/{stats['total']}")
    _print(f"  Pull failures: {stats['pull_fail']}")
    _print(f"  Trace failures: {stats['trace_fail']}")
    _print(f"{'='*60}")


if __name__ == "__main__":
    main()
