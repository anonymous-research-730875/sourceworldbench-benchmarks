"""
run_traced.py — Run SWE-bench evaluation with tracing instrumentation.

This module wraps the standard SWE-bench evaluation flow to:
1. Inject a tracer (conftest.py or sitecustomize.py) into the Docker container
2. Run tests as normal (grading still works)
3. Extract trace data from the container before cleanup

Usage:
    uv run python -m sourceworldbench_benchmarks.execution_tracer.legacy_swebench_harness.run_traced \
        --dataset_name SWE-bench/SWE-bench_Verified \
        --predictions_path gold \
        --instance_ids django__django-14238 \
        --run_id test_trace_01 \
        --trace_level function \
        --trace_output_dir ./trace_results
"""

from __future__ import annotations

import gzip
import io
import json
import os
import platform
import re
import subprocess
import tarfile
import threading
import traceback

import docker

if platform.system() == "Linux":
    import resource

from argparse import ArgumentDefaultsHelpFormatter, ArgumentParser, BooleanOptionalAction
from pathlib import Path, PurePosixPath

from swebench.harness.constants import (
    APPLY_PATCH_FAIL,
    APPLY_PATCH_PASS,
    DOCKER_PATCH,
    DOCKER_USER,
    DOCKER_WORKDIR,
    KEY_INSTANCE_ID,
    KEY_MODEL,
    KEY_PREDICTION,
    LOG_INSTANCE,
    LOG_REPORT,
    LOG_TEST_OUTPUT,
    RUN_EVALUATION_LOG_DIR,
    UTF8,
)
from swebench.harness.docker_build import (
    BuildImageError,
    build_container,
    build_env_images,
    close_logger,
    setup_logger,
)
from swebench.harness.docker_utils import (
    clean_images,
    cleanup_container,
    copy_to_container,
    exec_run_with_timeout,
    list_images,
    remove_image,
    should_remove,
)
from swebench.harness.grading import get_eval_report
from swebench.harness.reporting import make_run_report
from swebench.harness.test_spec.test_spec import TestSpec, make_test_spec
from swebench.harness.utils import (
    EvaluationError,
    get_predictions_from_file,
    load_swebench_dataset,
    optional_str,
    run_threadpool,
    str2bool,
)
from tqdm.auto import tqdm

# Path to the injectable tracer module
TRACER_SOURCE = Path(__file__).parent.parent / "tracers" / "injectable_tracer.py"

GIT_APPLY_CMDS = [
    "git apply --verbose",
    "git apply --verbose --reject",
    "patch --batch --fuzz=5 -p1 -i",
]


def _get_tracer_source():
    """Read the injectable tracer source code."""
    return TRACER_SOURCE.read_text(encoding="utf-8")


def _copy_string_to_container(container, content: str, dst_path: str):
    """Write a string as a file inside a Docker container using tar."""
    # Create an in-memory tar archive with the file
    tar_buffer = io.BytesIO()
    file_data = content.encode("utf-8")
    with tarfile.open(fileobj=tar_buffer, mode="w") as tar:
        info = tarfile.TarInfo(name=os.path.basename(dst_path))
        info.size = len(file_data)
        tar.addfile(info, io.BytesIO(file_data))
    tar_buffer.seek(0)

    dst_dir = os.path.dirname(dst_path)
    container.exec_run(f"mkdir -p {dst_dir}")
    container.put_archive(dst_dir, tar_buffer.read())


def _extract_binary_from_container(container, src_path: str) -> bytes | None:
    """Extract a file from a Docker container as raw bytes. Returns bytes or None."""
    try:
        bits, stat = container.get_archive(src_path)
        # bits is a generator of tar chunks
        tar_buffer = io.BytesIO()
        for chunk in bits:
            tar_buffer.write(chunk)
        tar_buffer.seek(0)

        with tarfile.open(fileobj=tar_buffer, mode="r") as tar:
            for member in tar.getmembers():
                f = tar.extractfile(member)
                if f:
                    return f.read()
    except Exception:
        return None
    return None


def _extract_file_from_container(container, src_path: str) -> str | None:
    """Extract a text file from a Docker container. Returns content or None."""
    data = _extract_binary_from_container(container, src_path)
    if data is None:
        return None
    return data.decode("utf-8")


def _is_django_instance(test_spec: TestSpec) -> bool:
    """Check if this is a Django instance (uses runtests.py, not pytest)."""
    return test_spec.repo == "django/django"


def _is_sympy_instance(test_spec: TestSpec) -> bool:
    """Check if this is a sympy instance (uses bin/test, not pytest)."""
    return test_spec.repo == "sympy/sympy"


def _is_session_trace_instance(test_spec: TestSpec) -> bool:
    """Check if this instance needs session-level tracing (wrapper script).

    Repos with custom (non-pytest) test runners need the _trace_wrapper.py
    approach: import tracer for whole-session profiling + atexit dump, then
    run the original test command via runpy.run_path.
    """
    return _is_django_instance(test_spec) or _is_sympy_instance(test_spec)


def _extract_patch_dirs(patch_text: str) -> list[str]:
    """Extract directory prefixes of files modified by a unified diff patch.

    Returns deduplicated directory prefixes (e.g. ["src/_pytest/mark", "django/db/models"])
    so the tracer can focus only on the code being changed.
    """
    dirs = set()
    for m in re.finditer(r"^(?:---|\+\+\+) [ab]/(.+)$", patch_text, re.MULTILINE):
        filepath = m.group(1)
        if filepath == "/dev/null":
            continue
        parent = os.path.dirname(filepath)
        if parent:
            dirs.add(parent)
    return sorted(dirs)


def _extract_directory_listing(container, dir_path: str) -> list[str]:
    """List .py files in a directory inside the container."""
    result = container.exec_run(
        f"find {dir_path} -maxdepth 1 -name '*.py' -type f",
        workdir="/testbed",
    )
    if result.exit_code != 0:
        return []
    return [
        line.strip()
        for line in result.output.decode("utf-8", errors="replace").strip().split("\n")
        if line.strip()
    ]


_TEST_PATCH_HEREDOC_RE = re.compile(
    r"git apply[^\n]*<<'(EOF_\d+)'\n(.*?)\n\1",
    flags=re.DOTALL,
)


def _extract_test_patch_from_eval_script(eval_script: str) -> str:
    """Pull the test_patch unified diff out of SWE-bench's eval_script.

    The eval_script applies the test_patch via a `git apply <<'EOF_xxx'`
    heredoc; we just snag the body of that heredoc. Returns "" if no
    such heredoc is present (some Django/sympy modes generate scripts
    without one).
    """
    m = _TEST_PATCH_HEREDOC_RE.search(eval_script or "")
    return m.group(2) if m else ""


def _apply_test_patch_to_snapshot(snapshot_dir: Path, test_patch: str,
                                  logger=None) -> bool:
    """Apply the unified diff `test_patch` to files under `snapshot_dir`.

    SWE-bench's eval_script applies test_patch at the start of the run
    and reverts it at the end (`git checkout <base_commit> <files>`).
    Our snapshot is grabbed *after* that revert, so the test_patch
    additions are missing. Re-applying it here gives us the same source
    state the test actually ran against.

    Uses `patch -p1 --forward`. Treats "all hunks already applied" as
    success (idempotent), so the same snapshot can be repaired more
    than once without spurious failures.
    """
    if not test_patch.strip():
        return True
    snapshot_dir = snapshot_dir.resolve()
    patch_file = snapshot_dir / ".test_patch.diff"
    patch_file.write_text(test_patch)
    try:
        result = subprocess.run(
            ["patch", "-p1", "--forward", "--input", str(patch_file)],
            cwd=str(snapshot_dir), capture_output=True, text=True,
        )
        if result.returncode == 0:
            return True
        # rc==1 means "some hunks didn't apply". That happens when
        # every hunk was already applied — patch prints
        # "Ignoring previously applied (or reversed) patch" per hunk.
        # As long as there's no actual "FAILED" / "rejected" hunk, treat
        # as success.
        out = (result.stdout or "") + "\n" + (result.stderr or "")
        if result.returncode == 1 and "Ignoring previously applied" in out \
                and "FAILED" not in out and "rejected" not in out.lower():
            return True
        if logger:
            logger.warning(
                "patch returncode=%s output=%s",
                result.returncode, out[-400:],
            )
        return False
    finally:
        try:
            patch_file.unlink()
        except OSError:
            pass


def extract_repo_snapshot(
    container,
    trace_jsons: list[str],
    test_spec: TestSpec,
    output_dir: Path,
    repo_dir: str = "/testbed",
    max_file_size: int = 200_000,
    logger=None,
    snapshot_subdir: str = "repo_snapshot",
    manifest_name: str = "snapshot_manifest.json",
):
    """Extract source files from the container based on trace data.

    Pulls:
    1. All files referenced in trace function entries (where execution happened)
    2. Other .py files in the same directories (neighboring context)
    3. The test file(s) referenced by test node IDs

    Accepts multiple trace JSON strings (e.g. pre-patch + post-patch) and merges
    their file sets.

    Files are saved to output_dir/<snapshot_subdir>/<relative_path>.
    """
    snapshot_dir = output_dir / snapshot_subdir
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    # Merge tests from all traces
    tests = {}
    for trace_json in trace_jsons:
        trace = json.loads(trace_json)
        tests.update(trace.get("tests", {}))

    # Collect all file paths from trace function entries
    traced_files: set[str] = set()
    for test_data in tests.values():
        for fn in test_data.get("functions", []):
            fp = fn.get("file", "")
            if fp:
                traced_files.add(fp)

    # Collect test file paths from test node IDs
    test_files: set[str] = set()
    for test_nodeid in tests:
        # "tests/test_foo.py::test_bar" -> "tests/test_foo.py"
        test_file = test_nodeid.split("::")[0]
        if test_file:
            test_files.add(test_file)

    # Collect directories to also grab neighboring .py files
    dirs_to_scan: set[str] = set()
    for fp in traced_files | test_files:
        parent = os.path.dirname(fp)
        if parent:
            dirs_to_scan.add(parent)

    # List neighboring .py files in those directories
    neighbor_files: set[str] = set()
    for d in sorted(dirs_to_scan):
        abs_dir = f"{repo_dir}/{d}"
        listings = _extract_directory_listing(container, abs_dir)
        for abs_path in listings:
            # Convert /testbed/src/flask/app.py -> src/flask/app.py
            if abs_path.startswith(repo_dir):
                rel = abs_path[len(repo_dir):].lstrip("/")
            else:
                rel = abs_path
            neighbor_files.add(rel)

    # Merge all file sets
    all_files = sorted(traced_files | test_files | neighbor_files)

    extracted = 0
    skipped = 0
    for rel_path in all_files:
        abs_path = f"{repo_dir}/{rel_path}"
        content = _extract_file_from_container(container, abs_path)
        if content is None:
            skipped += 1
            continue
        # Test files are NEVER truncated — the benchmark prompt needs to
        # show the FAIL_TO_PASS test function in full, and that function
        # might live past byte `max_file_size`. The builder applies its
        # own smart-window when the prompt budget can't fit the whole
        # test file. Other source files use the size cap.
        is_test = rel_path in test_files
        if not is_test and len(content) > max_file_size:
            content = content[:max_file_size] + "\n# ... [truncated, file too large]\n"

        dest = snapshot_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        extracted += 1

    # Re-apply test_patch to the snapshot.
    # SWE-bench's eval_script applies the test_patch at the start, then
    # reverts it (`git checkout <base_commit> <test_files>`) at the end so
    # the container can be reused. Our snapshot is captured AFTER that
    # revert, so test_patch additions (e.g. FAIL_TO_PASS tests that the
    # gold patch adds) are missing from the on-disk test file even though
    # they were present when the test ran. Re-applying gives us the source
    # state pytest actually saw.
    test_patch = _extract_test_patch_from_eval_script(test_spec.eval_script)
    test_patch_applied = False
    if test_patch:
        test_patch_applied = _apply_test_patch_to_snapshot(
            snapshot_dir, test_patch, logger=logger,
        )

    # Write a manifest of what was extracted
    manifest = {
        "traced_files": sorted(traced_files),
        "test_files": sorted(test_files),
        "neighbor_files": sorted(neighbor_files - traced_files - test_files),
        "total_extracted": extracted,
        "total_skipped": skipped,
        "test_patch_applied": test_patch_applied,
    }
    (output_dir / manifest_name).write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    if logger:
        logger.info(
            f"Repo snapshot: extracted {extracted} files, skipped {skipped} "
            f"({len(traced_files)} traced, {len(test_files)} test, "
            f"{len(neighbor_files - traced_files - test_files)} neighbor); "
            f"test_patch applied={test_patch_applied}"
        )

    return manifest


def inject_tracer(container, test_spec: TestSpec, trace_level: str = "function", repo_dir: str = "/testbed"):
    """Inject the tracer into a running container."""
    tracer_code = _get_tracer_source()

    if _is_session_trace_instance(test_spec):
        # For non-pytest repos (Django, sympy): write tracer as a module,
        # then use a wrapper script that imports it before running the
        # original test command.
        _copy_string_to_container(container, tracer_code, f"{repo_dir}/_swebench_tracer.py")
        wrapper = (
            "import sys\n"
            "import os\n"
            "import runpy\n"
            "# Import tracer — session mode activates whole-session profiling + atexit\n"
            "import _swebench_tracer\n"
            "# Remove wrapper from argv, keep the target script + its args\n"
            "sys.argv = sys.argv[1:]\n"
            "# Add the script's directory to sys.path (matching Python's direct execution behavior)\n"
            "script_dir = os.path.dirname(os.path.abspath(sys.argv[0]))\n"
            "if script_dir not in sys.path:\n"
            "    sys.path.insert(0, script_dir)\n"
            "runpy.run_path(sys.argv[0], run_name='__main__')\n"
        )
        _copy_string_to_container(container, wrapper, f"{repo_dir}/_trace_wrapper.py")
    else:
        # For pytest-based repos: write tracer as conftest.py
        _copy_string_to_container(container, tracer_code, f"{repo_dir}/conftest.py")


def patch_eval_script(
    test_spec: TestSpec,
    pred: dict,
    trace_level: str = "line",
    memory_tracking: str = "both",
    compress: bool = True,
    output_path: str = "/testbed/trace_output.json",
) -> str:
    """
    Patch the eval script to:
    1. Set tracer environment variables (including TRACE_PATHS from the patch)
    2. For Django/sympy: replace the test-runner call with a wrapper invocation
    3. Keep the rest of the eval script as-is
    """
    original_script = test_spec.eval_script

    is_django = _is_django_instance(test_spec)
    is_sympy = _is_sympy_instance(test_spec)
    is_session = is_django or is_sympy

    # Extract directories touched by the gold patch so the tracer focuses
    # only on the code being changed (not the entire framework).
    patch_text = pred.get(KEY_PREDICTION, "") or ""
    trace_paths = _extract_patch_dirs(patch_text)

    # In session mode (Django/sympy) we also need the tracer to fire on
    # the test files themselves so it can detect test-method entries and
    # open per-test buckets. Without this, test_* functions in tests/...
    # are invisible to the tracer (SWEBENCH_TRACE_PATHS filters them out)
    # and the trace collapses back to one session-wide record.
    if _is_session_trace_instance(test_spec):
        test_patch_text = _extract_test_patch_from_eval_script(
            test_spec.eval_script,
        )
        test_dirs = _extract_patch_dirs(test_patch_text)
        for d in test_dirs:
            if d not in trace_paths:
                trace_paths.append(d)

    # FAIL_TO_PASS set so the tracer only attaches to those tests.
    fail_to_pass = list(getattr(test_spec, "FAIL_TO_PASS", None) or [])
    f2p_arg = ",".join(t.replace(",", "%2C") for t in fail_to_pass) if fail_to_pass else ""

    tracer_env_lines = [
        "",
        "# SWE-bench tracer configuration",
        "export SWEBENCH_TRACE_ENABLED=1",
        f"export SWEBENCH_TRACE_LEVEL={trace_level}",
        f"export SWEBENCH_TRACE_MEMORY={memory_tracking}",
        f"export SWEBENCH_TRACE_OUTPUT={output_path}",
        "export SWEBENCH_REPO_DIR=/testbed",
        f"export SWEBENCH_TRACE_INSTANCE_ID={test_spec.instance_id}",
        f"export SWEBENCH_TRACE_COMPRESS={'1' if compress else '0'}",
    ]
    if f2p_arg:
        # quoted to preserve special chars in test names like '[' or '<'
        tracer_env_lines.append(
            f"export SWEBENCH_TRACE_FAIL_TO_PASS='{f2p_arg}'"
        )
    if trace_paths:
        tracer_env_lines.append(
            f"export SWEBENCH_TRACE_PATHS={','.join(trace_paths)}"
        )

    if is_session:
        tracer_env_lines += [
            "export SWEBENCH_TRACE_DJANGO=1",
            "export PYTHONPATH=/testbed:${PYTHONPATH:-}",
            # v0.4+: enable test-method boundary detection so the session
            # trace produces one record per test (matching pytest-mode shape).
            "export SWEBENCH_TRACE_SESSION_TEST_DETECT=1",
            # FAIL_TO_PASS dictates the test_id format we need to match:
            # - Django uses "test_method (module.ClassName)"
            # - Sympy uses just the bare function name (top-level test_*
            #   functions, no path or class prefix in the dataset)
            f"export SWEBENCH_TRACE_TEST_ID_FORMAT="
            f"{'django' if is_django else 'bare'}",
        ]

    tracer_env_lines.append("")

    # Insert after "set -uxo pipefail"
    lines = original_script.split("\n")
    new_lines = []
    for line in lines:
        # For Django: replace ./tests/runtests.py with wrapper invocation
        if is_django and "./tests/runtests.py" in line:
            line = line.replace(
                "./tests/runtests.py",
                "python /testbed/_trace_wrapper.py ./tests/runtests.py",
            )
        # For sympy: replace bin/test with wrapper invocation
        if is_sympy and "bin/test" in line:
            line = line.replace(
                "bin/test",
                "python /testbed/_trace_wrapper.py bin/test",
            )
        new_lines.append(line)
        if line.strip() == "set -uxo pipefail":
            new_lines.extend(tracer_env_lines)

    return "\n".join(new_lines)


def _extract_trace_from_container(
    container, base_path: str, output_dir: Path, logger=None,
) -> tuple[str | None, Path | None]:
    """Extract a trace file from container, trying .json.gz first then .json.

    Returns (trace_json_str, trace_file_path) or (None, None) if not found.
    """
    # Try gzip first
    gz_path = base_path + ".gz" if not base_path.endswith(".gz") else base_path
    trace_raw = _extract_binary_from_container(container, gz_path)
    if trace_raw:
        trace_file = output_dir / os.path.basename(gz_path)
        trace_file.write_bytes(trace_raw)
        trace_json_str = gzip.decompress(trace_raw).decode("utf-8")
        if logger:
            logger.info(f"Extracted gzip trace ({len(trace_raw)} bytes) to {trace_file}")
        return trace_json_str, trace_file

    # Fall back to plain JSON
    plain_path = base_path.removesuffix(".gz")
    trace_json_str = _extract_file_from_container(container, plain_path)
    if trace_json_str:
        trace_file = output_dir / os.path.basename(plain_path)
        trace_file.write_text(trace_json_str)
        if logger:
            logger.info(f"Extracted plain trace ({len(trace_json_str)} bytes) to {trace_file}")
        return trace_json_str, trace_file

    return None, None


def _pre_outcomes_from_log(
    test_spec: TestSpec,
    log_text: str,
    fail_to_pass: list[str],
    logger=None,
) -> dict[str, str]:
    """Build a {test_id: outcome} dict for a PRE-patch run.

    SWE-bench only grades the post-patch run, so for the pre side we have
    to parse the raw test output ourselves. We use SWE-bench's repo-specific
    parser when available, normalizing the returned statuses to the same
    lowercase labels used elsewhere in the trace ("passed" / "failed" /
    "error").

    Tests the parser didn't surface (because they didn't run, or because
    of a known parser quirk like Django's multi-test-per-line verbose
    output) are filled in with the FAIL_TO_PASS contract: every
    FAIL_TO_PASS test does NOT pass pre-patch by definition, so we
    default it to "failed". Tests that DID surface keep whatever the
    parser said (could be "passed" — e.g. tests added by the test_patch
    that happen to pass already in pre-state).
    """
    from swebench.harness.log_parsers import MAP_REPO_TO_PARSER

    out: dict[str, str] = {}
    parser = MAP_REPO_TO_PARSER.get(test_spec.repo) if test_spec else None
    if parser is not None and log_text:
        try:
            parsed_raw = parser(log_text, test_spec) or {}
        except Exception as e:
            if logger:
                logger.warning(f"pre-log parser raised: {e}")
            parsed_raw = {}
        for tid, status in parsed_raw.items():
            label = str(status).strip().lower()
            if label in ("passed", "failed", "error", "skipped", "xfail"):
                out[tid] = label
            else:
                out[tid] = "error"

    # Fallback: every FAIL_TO_PASS test the parser missed → "failed".
    for tid in fail_to_pass or []:
        out.setdefault(tid, "failed")
    return out


def _merge_outcomes_dict_into_trace(
    trace_file: Path,
    outcomes: dict[str, str],
    instance_resolved: bool | None = None,
    test_output_path: Path | None = None,
    logger=None,
) -> bool:
    """Core merge: write a {test_id: outcome} dict into an existing trace.

    Used by both the post-patch flow (where outcomes come from SWE-bench's
    report.json) and the pre-patch flow (where outcomes come from
    `_pre_outcomes_from_log`).
    """
    if not trace_file.exists():
        return False

    is_gz = str(trace_file).endswith(".gz")
    if is_gz:
        with gzip.open(trace_file, "rt", encoding="utf-8") as f:
            trace = json.load(f)
    else:
        trace = json.loads(trace_file.read_text())

    # Capture sample of test output so failure reasons can be reconstructed
    output_excerpt = None
    if test_output_path and test_output_path.exists():
        try:
            txt = test_output_path.read_text(errors="replace")
            output_excerpt = txt[-4096:] if len(txt) > 4096 else txt
        except Exception:
            output_excerpt = None

    tests = trace.get("tests", {})
    matched = 0
    for nodeid, rec in tests.items():
        # Attach known outcome if we have one
        if nodeid in outcomes and rec.get("outcome") in (None, ""):
            rec["outcome"] = outcomes[nodeid]
            matched += 1
        # For session-mode entries, attach aggregate outcomes + excerpt
        if nodeid == "session" or rec.get("backend") in ("setprofile", "settrace", "sysmon"):
            if outcomes:
                rec["session_outcomes"] = outcomes
            # Aggregate outcome: passed only if ALL FAIL_TO_PASS tests passed
            if outcomes and rec.get("outcome") in (None, ""):
                vals = list(outcomes.values())
                if all(v == "passed" for v in vals):
                    rec["outcome"] = "passed"
                else:
                    rec["outcome"] = "failed"
        if output_excerpt and rec.get("outcome_details") is None and rec.get("outcome") in ("failed", "error"):
            rec["outcome_details"] = {
                "exception_type": None,
                "exception_message": None,
                "is_assertion": False,
                "failure_file": None,
                "failure_line": None,
                "failure_phase": None,
                "traceback_text": output_excerpt,
            }

    if instance_resolved is not None:
        trace["instance_resolved"] = bool(instance_resolved)

    if is_gz:
        with gzip.open(trace_file, "wt", encoding="utf-8") as f:
            json.dump(trace, f, separators=(",", ":"))
    else:
        trace_file.write_text(json.dumps(trace, separators=(",", ":")))

    if logger:
        logger.info(
            f"Merged outcomes into trace: {matched} per-test matches, "
            f"{len(outcomes)} total outcomes"
        )
    return True


def _merge_outcomes_into_trace(
    trace_file: Path,
    report: dict,
    instance_id: str,
    test_output_path: Path | None = None,
    logger=None,
) -> bool:
    """Merge SWE-bench's report.json outcomes into the trace (post-patch flow).

    SWE-bench's `get_eval_report` produces:
      report[instance_id]["tests_status"][category]["success"|"failure"] -> [test_ids]

    Builds the outcome dict from that shape, then dispatches to
    `_merge_outcomes_dict_into_trace` for the actual write.
    """
    inst_report = report.get(instance_id, {}) if report else {}
    outcomes: dict[str, str] = {}
    tests_status = inst_report.get("tests_status") or {}
    for category, parts in tests_status.items():
        if not isinstance(parts, dict):
            continue
        for test_id in parts.get("success", []) or []:
            outcomes[test_id] = "passed"
        for test_id in parts.get("failure", []) or []:
            outcomes[test_id] = outcomes.get(test_id, "failed")
    return _merge_outcomes_dict_into_trace(
        trace_file, outcomes,
        instance_resolved=inst_report.get("resolved", False),
        test_output_path=test_output_path, logger=logger,
    )


def _find_existing_trace(trace_dir: Path, basename: str = "trace_output") -> Path | None:
    """Find an existing trace file, checking .json.gz first, then .json."""
    gz = trace_dir / f"{basename}.json.gz"
    if gz.exists():
        return gz
    plain = trace_dir / f"{basename}.json"
    if plain.exists():
        return plain
    return None


def _apply_patch_to_container(container, patch_file, instance_id, logger):
    """Apply a patch file to the container. Raises EvaluationError on failure."""
    copy_to_container(container, patch_file, PurePosixPath(DOCKER_PATCH))
    for git_apply_cmd in GIT_APPLY_CMDS:
        val = container.exec_run(
            f"{git_apply_cmd} {DOCKER_PATCH}",
            workdir=DOCKER_WORKDIR,
            user=DOCKER_USER,
        )
        if val.exit_code == 0:
            logger.info(f"{APPLY_PATCH_PASS}:\n{val.output.decode(UTF8)}")
            return
        else:
            logger.info(f"Failed: {git_apply_cmd}")
    logger.info(f"{APPLY_PATCH_FAIL}:\n{val.output.decode(UTF8)}")
    raise EvaluationError(
        instance_id,
        f"{APPLY_PATCH_FAIL}:\n{val.output.decode(UTF8)}",
        logger,
    )


def _run_eval_script(container, eval_script: str, eval_file: Path, timeout, logger):
    """Copy eval script into container, run it, return (test_output, timed_out, runtime)."""
    eval_file.write_text(eval_script)
    copy_to_container(container, eval_file, PurePosixPath("/eval.sh"))
    return exec_run_with_timeout(container, "/bin/bash /eval.sh", timeout)


def run_instance_traced(
    test_spec: TestSpec,
    pred: dict,
    rm_image: bool,
    force_rebuild: bool,
    client: docker.DockerClient,
    run_id: str,
    timeout: int | None = None,
    trace_level: str = "line",
    memory_tracking: str = "both",
    trace_output_dir: str = "./trace_results",
    compress: bool = True,
    dual_trace: bool = False,
    pre_only: bool = False,
) -> dict:
    """
    Run a single instance with tracing instrumentation.

    Same as swebench's run_instance but with tracer injection and extraction.

    When dual_trace=True, runs tests twice: once before applying the gold patch
    (pre-patch trace) and once after (post-patch trace). Pre-patch failures are
    non-blocking.

    When pre_only=True, runs only the pre-patch trace (no post-patch, no
    grading). Used to backfill pre-patch traces for instances that already
    have post-patch traces. Skips the instance if `trace_output_pre.*` is
    already present.
    """
    instance_id = test_spec.instance_id
    model_name_or_path = pred.get(KEY_MODEL, "None").replace("/", "__")
    log_dir = RUN_EVALUATION_LOG_DIR / run_id / model_name_or_path / instance_id

    # Check if already completed (supports both .json and .json.gz)
    report_path = log_dir / LOG_REPORT
    trace_dir = Path(trace_output_dir) / run_id / instance_id
    existing_trace = _find_existing_trace(trace_dir)
    existing_pre = _find_existing_trace(trace_dir, "trace_output_pre")

    if pre_only:
        # Skip if pre-patch trace already exists.
        if existing_pre:
            return {
                "completed": True,
                "resolved": None,
                "trace_file": str(existing_trace) if existing_trace else None,
                "pre_trace_file": str(existing_pre),
            }
    elif report_path.exists() and existing_trace:
        report = json.loads(report_path.read_text())
        return {
            "completed": True,
            "resolved": report[instance_id]["resolved"],
            "trace_file": str(existing_trace),
            "pre_trace_file": str(existing_pre) if existing_pre else None,
        }

    # Set up logger
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / LOG_INSTANCE
    logger = setup_logger(instance_id, log_file)

    container = None
    eval_completed = False
    report = {}
    trace_json_str = None
    pre_trace_json_str = None
    trace_file_path = None
    pre_trace_file_path = None

    try:
        # Build + start container
        container = build_container(
            test_spec, client, run_id, logger, rm_image, force_rebuild
        )
        container.start()
        logger.info(f"Container for {instance_id} started: {container.id}")

        # Copy model prediction as patch (needed for patch_eval_script and later apply)
        patch_file = Path(log_dir / "patch.diff")
        patch_file.write_text(pred[KEY_PREDICTION] or "")
        logger.info(f"Patch for {instance_id} written to {patch_file}")

        # --- TRACING: Inject tracer (before any patch application) ---
        logger.info(f"Injecting tracer (level={trace_level}) into container...")
        inject_tracer(container, test_spec=test_spec, trace_level=trace_level)

        # --- PRE-PATCH TRACE (optional) ---
        if dual_trace or pre_only:
            logger.info("=== PRE-PATCH TRACE ===")
            try:
                pre_script = patch_eval_script(
                    test_spec, pred, trace_level=trace_level,
                    memory_tracking=memory_tracking,
                    compress=compress,
                    output_path="/testbed/trace_output_pre.json",
                )
                pre_eval_file = Path(log_dir / "eval_pre.sh")
                test_output_pre, timed_out_pre, runtime_pre = _run_eval_script(
                    container, pre_script, pre_eval_file, timeout, logger,
                )
                logger.info(f"Pre-patch test runtime: {runtime_pre:_.2f} seconds")

                # Save pre-patch test output
                pre_output_path = log_dir / "test_output_pre.txt"
                with open(pre_output_path, "w") as f:
                    f.write(test_output_pre)
                if timed_out_pre:
                    logger.warning(f"Pre-patch tests timed out after {timeout}s")

                # Extract pre-patch trace
                trace_dir.mkdir(parents=True, exist_ok=True)
                pre_trace_json_str, pre_trace_file_path = _extract_trace_from_container(
                    container, "/testbed/trace_output_pre.json",
                    trace_dir, logger=logger,
                )
                if pre_trace_json_str:
                    logger.info("Pre-patch trace extracted successfully")
                    # SWE-bench only grades the post-patch run, so for the
                    # pre-side we have to parse the raw test output
                    # ourselves to populate session_outcomes / per-test
                    # outcomes. Without this, the benchmark builder skips
                    # all session-mode pre records (Django/sympy).
                    try:
                        f2p = list(getattr(test_spec, "FAIL_TO_PASS", None) or [])
                        pre_outcomes = _pre_outcomes_from_log(
                            test_spec, test_output_pre, f2p, logger=logger,
                        )
                        if pre_outcomes and pre_trace_file_path:
                            _merge_outcomes_dict_into_trace(
                                pre_trace_file_path, pre_outcomes,
                                test_output_path=pre_output_path,
                                logger=logger,
                            )
                            # Refresh the in-memory string so downstream
                            # consumers (snapshot extraction etc.) see the
                            # merged outcomes.
                            if str(pre_trace_file_path).endswith(".gz"):
                                with gzip.open(pre_trace_file_path, "rt",
                                               encoding="utf-8") as f:
                                    pre_trace_json_str = f.read()
                            else:
                                pre_trace_json_str = pre_trace_file_path.read_text()
                    except Exception as e:
                        logger.warning(f"Pre-outcome merge failed (non-blocking): {e}")
                else:
                    logger.info("No pre-patch trace found (expected for FAIL_TO_PASS)")
            except Exception as e:
                logger.warning(f"Pre-patch trace failed (non-blocking): {e}")

        if pre_only:
            # In pre-only mode: extract a pre-patch repo snapshot if we have
            # the trace data, then short-circuit. We do NOT apply the gold
            # patch, run post-patch, or grade — those are already done.
            if pre_trace_json_str:
                logger.info("Extracting pre-patch repo snapshot...")
                extract_repo_snapshot(
                    container, [pre_trace_json_str], test_spec,
                    trace_dir, logger=logger,
                    snapshot_subdir="repo_snapshot_pre",
                    manifest_name="snapshot_manifest_pre.json",
                )
            eval_completed = True
            return  # finally returns the result dict

        # --- APPLY GOLD PATCH ---
        _apply_patch_to_container(container, patch_file, instance_id, logger)

        # Git diff after patch
        git_diff_output = (
            container.exec_run(
                "git -c core.fileMode=false diff", workdir=DOCKER_WORKDIR
            )
            .output.decode(UTF8)
            .strip()
        )
        logger.info(f"Git diff after patch:\n{git_diff_output}")

        # --- POST-PATCH TRACE ---
        logger.info("=== POST-PATCH TRACE ===")
        patched_script = patch_eval_script(
            test_spec, pred, trace_level=trace_level,
            memory_tracking=memory_tracking,
            compress=compress,
            output_path="/testbed/trace_output.json",
        )
        eval_file = Path(log_dir / "eval.sh")
        test_output, timed_out, total_runtime = _run_eval_script(
            container, patched_script, eval_file, timeout, logger,
        )
        test_output_path = log_dir / LOG_TEST_OUTPUT
        logger.info(f"Post-patch test runtime: {total_runtime:_.2f} seconds")
        with open(test_output_path, "w") as f:
            f.write(test_output)
            logger.info(f"Test output written to {test_output_path}")
            if timed_out:
                f.write(f"\n\nTimeout error: {timeout} seconds exceeded.")
                raise EvaluationError(
                    instance_id,
                    f"Test timed out after {timeout} seconds.",
                    logger,
                )

        # --- TRACING: Extract post-patch trace data ---
        logger.info("Extracting post-patch trace data from container...")
        trace_dir.mkdir(parents=True, exist_ok=True)
        trace_json_str, trace_file_path = _extract_trace_from_container(
            container, "/testbed/trace_output.json",
            trace_dir, logger=logger,
        )

        if trace_json_str:
            # --- TRACING: Extract repo snapshot (from post-patch state) ---
            logger.info("Extracting repo snapshot from container...")
            trace_jsons = [trace_json_str]
            if pre_trace_json_str:
                trace_jsons.append(pre_trace_json_str)
            extract_repo_snapshot(
                container, trace_jsons, test_spec, trace_dir, logger=logger,
            )
        else:
            logger.info("No post-patch trace data found in container")

        # Git diff after tests
        git_diff_output_after = (
            container.exec_run(
                "git -c core.fileMode=false diff", workdir=DOCKER_WORKDIR
            )
            .output.decode(UTF8)
            .strip()
        )
        logger.info(f"Git diff after tests:\n{git_diff_output_after}")

        # Grade
        logger.info(f"Grading answer for {instance_id}...")
        report = get_eval_report(
            test_spec=test_spec,
            prediction=pred,
            test_log_path=test_output_path,
            include_tests_status=True,
        )
        logger.info(
            f"report: {report}\n"
            f"Result for {instance_id}: resolved: {report[instance_id]['resolved']}"
        )
        with open(report_path, "w") as f:
            f.write(json.dumps(report, indent=4))
        eval_completed = True

        # --- TRACING: merge outcomes into the trace ---
        if trace_file_path:
            try:
                _merge_outcomes_into_trace(
                    trace_file_path, report, instance_id,
                    test_output_path=test_output_path, logger=logger,
                )
            except Exception as merge_err:
                logger.warning(f"Outcome merge failed (non-blocking): {merge_err}")

    except (EvaluationError, BuildImageError) as e:
        error_msg = traceback.format_exc()
        logger.info(error_msg)
        print(e)
    except Exception as e:
        error_msg = (
            f"Error in evaluating {instance_id}: {e}\n"
            f"{traceback.format_exc()}\n"
            f"Check ({logger.log_file}) for more information."
        )
        logger.error(error_msg)
    finally:
        cleanup_container(client, container, logger)
        if rm_image:
            remove_image(client, test_spec.instance_image_key, logger)
        close_logger(logger)
        return {
            "completed": eval_completed,
            "resolved": report.get(instance_id, {}).get("resolved", False),
            "trace_file": str(trace_file_path) if trace_json_str else None,
            "pre_trace_file": str(pre_trace_file_path) if pre_trace_json_str else None,
        }


def run_instances_traced(
    predictions: dict,
    instances: list,
    cache_level: str,
    clean: bool,
    force_rebuild: bool,
    max_workers: int,
    run_id: str,
    timeout: int,
    trace_level: str = "line",
    memory_tracking: str = "both",
    trace_output_dir: str = "./trace_results",
    compress: bool = True,
    dual_trace: bool = False,
    pre_only: bool = False,
    namespace: str | None = "swebench",
    instance_image_tag: str = "latest",
    env_image_tag: str = "latest",
):
    """Run all instances with tracing in parallel."""
    client = docker.from_env()
    test_specs = list(
        map(
            lambda instance: make_test_spec(
                instance,
                namespace=namespace,
                instance_image_tag=instance_image_tag,
                env_image_tag=env_image_tag,
            ),
            instances,
        )
    )

    instance_image_ids = {x.instance_image_key for x in test_specs}
    existing_images = {
        tag
        for i in client.images.list(all=True)
        for tag in i.tags
        if tag in instance_image_ids
    }
    if not force_rebuild and len(existing_images):
        print(f"Found {len(existing_images)} existing instance images. Will reuse them.")

    payloads = []
    for test_spec in test_specs:
        payloads.append(
            (
                test_spec,
                predictions[test_spec.instance_id],
                should_remove(
                    test_spec.instance_image_key, cache_level, clean, existing_images,
                ),
                force_rebuild,
                client,
                run_id,
                timeout,
                trace_level,
                memory_tracking,
                trace_output_dir,
                compress,
                dual_trace,
                pre_only,
            )
        )

    print(f"Running {len(instances)} instances with tracing (level={trace_level})...")
    stats = {"✓": 0, "✖": 0, "error": 0, "traced": 0}
    pbar = tqdm(total=len(payloads), desc="Traced Evaluation", postfix=stats)
    lock = threading.Lock()

    def run_with_progress(*args):
        result = run_instance_traced(*args)
        with lock:
            # Count trace extraction separately from eval completion.
            # Trace can succeed even when later steps (grading) fail.
            if result.get("trace_file"):
                stats["traced"] += 1
            if result["completed"]:
                if result["resolved"]:
                    stats["✓"] += 1
                else:
                    stats["✖"] += 1
            else:
                stats["error"] += 1
            pbar.set_postfix(stats)
            pbar.update()
        return result

    run_threadpool(run_with_progress, payloads, max_workers)
    print(f"All instances run. Traced: {stats['traced']}/{len(instances)}")


def main(
    dataset_name: str,
    split: str,
    instance_ids: list,
    predictions_path: str,
    max_workers: int,
    force_rebuild: bool,
    cache_level: str,
    clean: bool,
    open_file_limit: int,
    run_id: str,
    timeout: int,
    namespace: str | None,
    trace_level: str,
    trace_output_dir: str,
    memory_tracking: str = "both",
    compress: bool = True,
    dual_trace: bool = False,
    pre_only: bool = False,
    instance_image_tag: str = "latest",
    env_image_tag: str = "latest",
):
    """Run traced evaluation harness."""
    assert len(run_id) > 0, "Run ID must be provided"

    predictions = get_predictions_from_file(predictions_path, dataset_name, split)
    predictions = {pred[KEY_INSTANCE_ID]: pred for pred in predictions}

    # Load dataset, filter to predictions.
    # In pre_only mode we bypass SWE-bench's report.json-based "already done"
    # filter so we can backfill pre-patch traces for instances whose post-patch
    # run + grading have already completed.
    full_dataset = load_swebench_dataset(dataset_name, split, instance_ids)
    if pre_only:
        dataset = [
            row for row in full_dataset
            if row[KEY_INSTANCE_ID] in predictions
        ]
    else:
        from swebench.harness.run_evaluation import get_dataset_from_preds
        dataset = get_dataset_from_preds(
            dataset_name, split, instance_ids, predictions, run_id, rewrite_reports=False
        )

    if platform.system() == "Linux":
        resource.setrlimit(resource.RLIMIT_NOFILE, (open_file_limit, open_file_limit))
    client = docker.from_env()

    existing_images = list_images(client)
    if not dataset:
        print("No instances to run.")
    else:
        if namespace is None:
            build_env_images(
                client, dataset, force_rebuild, max_workers,
                namespace, instance_image_tag, env_image_tag,
            )
        run_instances_traced(
            predictions, dataset, cache_level, clean, force_rebuild,
            max_workers, run_id, timeout, trace_level,
            memory_tracking=memory_tracking,
            trace_output_dir=trace_output_dir,
            compress=compress, dual_trace=dual_trace, pre_only=pre_only,
            namespace=namespace, instance_image_tag=instance_image_tag,
            env_image_tag=env_image_tag,
        )

    clean_images(client, existing_images, cache_level, clean)
    return make_run_report(
        predictions, full_dataset, run_id, client,
        namespace, instance_image_tag, env_image_tag,
    )


if __name__ == "__main__":
    parser = ArgumentParser(
        description="Run SWE-bench evaluation with tracing.",
        formatter_class=ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("-d", "--dataset_name", default="SWE-bench/SWE-bench_Verified", type=str)
    parser.add_argument("-s", "--split", type=str, default="test")
    parser.add_argument("-i", "--instance_ids", nargs="+", type=str)
    parser.add_argument("-p", "--predictions_path", type=str, required=True)
    parser.add_argument("--max_workers", type=int, default=4)
    parser.add_argument("--open_file_limit", type=int, default=4096)
    parser.add_argument("-t", "--timeout", type=int, default=1800)
    parser.add_argument("--force_rebuild", type=str2bool, default=False)
    parser.add_argument("--cache_level", type=str, choices=["none", "base", "env", "instance"], default="env")
    parser.add_argument("--clean", type=str2bool, default=False)
    parser.add_argument("-id", "--run_id", type=str, required=True)
    parser.add_argument("-n", "--namespace", type=optional_str, default="swebench")
    parser.add_argument("--instance_image_tag", type=str, default="latest")
    parser.add_argument("--env_image_tag", type=str, default="latest")

    # Tracing-specific args
    parser.add_argument("--trace_level", type=str, choices=["function", "line"], default="line",
                       help="Trace level: function (low overhead) or line (detailed)")
    parser.add_argument("--memory_tracking", type=str,
                       choices=["rss", "tracemalloc", "both"], default="both",
                       help="Memory tracking mode: rss only, tracemalloc only, or both")
    parser.add_argument("--trace_output_dir", type=str, default="./trace_results",
                       help="Directory to store trace output files")
    parser.add_argument("--compress", action=BooleanOptionalAction, default=True,
                       help="Enable gzip compression for trace output")
    parser.add_argument("--dual_trace", action=BooleanOptionalAction, default=False,
                       help="Collect pre-patch trace in addition to post-patch trace")
    parser.add_argument("--pre_only", action=BooleanOptionalAction, default=False,
                       help="Run only the pre-patch trace; skip patch apply, "
                            "post-patch run, and grading. Skips instances that "
                            "already have a pre-patch trace.")

    args = parser.parse_args()
    main(**vars(args))
