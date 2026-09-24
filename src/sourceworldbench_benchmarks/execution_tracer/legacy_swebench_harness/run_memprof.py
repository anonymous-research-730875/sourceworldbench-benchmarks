"""run_memprof.py -- Inject per-call tracemalloc reset_peak memory profiler.

No time tracking, no line-level tracking. Function-level memory only.
Mirrors run_cprofile.py / run_walltime.py.

Output (per instance):
    memprof_results/<run_id>/<instance_id>/memprof_output.json[.gz]
    memprof_results/<run_id>/<instance_id>/memprof_output_pre.json[.gz]
"""
from __future__ import annotations

import gzip
import os
import platform
import threading
import traceback
from argparse import ArgumentParser, BooleanOptionalAction
from pathlib import Path, PurePosixPath

import docker

if platform.system() == "Linux":
    import resource

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
from swebench.harness.test_spec.test_spec import TestSpec, make_test_spec
from swebench.harness.utils import (
    EvaluationError,
    get_predictions_from_file,
    load_swebench_dataset,
    run_threadpool,
)
from tqdm.auto import tqdm

from sourceworldbench_benchmarks.execution_tracer.legacy_swebench_harness.run_traced import (
    GIT_APPLY_CMDS,
    _copy_string_to_container,
    _extract_binary_from_container,
    _extract_file_from_container,
    _extract_patch_dirs,
    _extract_test_patch_from_eval_script,
    _is_django_instance,
    _is_session_trace_instance,
    _is_sympy_instance,
)

MEMPROF_SOURCE = (
    Path(__file__).parent.parent / "tracers" / "memory_test_profiler.py")


def _get_memprof_source() -> str:
    return MEMPROF_SOURCE.read_text(encoding="utf-8")


def inject_memprof(container, test_spec: TestSpec, repo_dir: str = "/testbed"):
    """Drop the memory-only profiler into the container.

    pytest repos: as `conftest.py`.
    Django/sympy: as `_swebench_memprof.py` + a wrapper that imports it
    BEFORE running the real test command (otherwise the unittest patch
    doesn't apply in time).
    """
    code = _get_memprof_source()
    if _is_session_trace_instance(test_spec):
        _copy_string_to_container(
            container, code, f"{repo_dir}/_swebench_memprof.py")
        wrapper = (
            "import sys\n"
            "import os\n"
            "import runpy\n"
            "import _swebench_memprof  # installs unittest.TestCase.run patch\n"
            "sys.argv = sys.argv[1:]\n"
            "script_dir = os.path.dirname(os.path.abspath(sys.argv[0]))\n"
            "if script_dir not in sys.path:\n"
            "    sys.path.insert(0, script_dir)\n"
            "runpy.run_path(sys.argv[0], run_name='__main__')\n"
        )
        _copy_string_to_container(
            container, wrapper, f"{repo_dir}/_memprof_wrapper.py")
    else:
        _copy_string_to_container(container, code, f"{repo_dir}/conftest.py")


def _filter_pytest_line_to_f2p(line: str, f2p: list[str]) -> str:
    """Restrict a `pytest …` invocation to only the FAIL_TO_PASS tests.

    SWE-bench eval scripts run `pytest -rA path/to/file.py [path2.py …]`
    on the full test file(s). The benchmark only needs the
    `FAIL_TO_PASS` tests, so for heavy files (matplotlib's
    `test_axes.py` has 600+ tests; xarray has 1800+) we waste enormous
    time profiling tests we'll never read.

    This rewrite tokenizes the pytest line, drops any positional
    arguments that look like test paths (anything ending in `.py` or
    containing `.py::`), then appends the F2P test ids verbatim. Flags
    (`-rA`, `--no-header`, …) are preserved. Quoted shell-safe via
    `shlex.join` so parametric ids like `test_x[case-1]` survive
    bash glob expansion. Returns the input unchanged if the line isn't
    a `pytest` invocation or `f2p` is empty.
    """
    import shlex
    stripped = line.lstrip()
    if not stripped.startswith("pytest") or not f2p:
        return line
    indent = line[: len(line) - len(stripped)]
    try:
        tokens = shlex.split(stripped)
    except ValueError:
        return line
    kept: list[str] = []
    found_path = False
    for tok in tokens:
        if tok.endswith(".py") or ".py::" in tok:
            found_path = True
            continue
        kept.append(tok)
    if not found_path:
        return line
    kept.extend(f2p)
    return indent + shlex.join(kept)


def patch_eval_script_for_memprof(
    test_spec: TestSpec,
    pred: dict,
    output_path: str = "/testbed/memprof_output.json",
    only_f2p: bool = False,
) -> str:
    """Inject SWEBENCH_MEM_* env vars and re-route the test runner (Django/sympy).

    Scope filter is set from the gold-patch dirs so the per-method buckets
    only cover in-project frames, matching the existing tracer's scope.

    If `only_f2p` is set and the instance is pytest-based, the eval
    script's pytest invocation is narrowed to just the FAIL_TO_PASS
    test ids — typically a 50–1000× reduction in tests run, since
    SWE-bench eval scripts default to running every test in the
    target file (e.g. matplotlib's `test_axes.py`, ~600 tests).
    """
    original_script = test_spec.eval_script
    is_django = _is_django_instance(test_spec)
    is_sympy = _is_sympy_instance(test_spec)
    is_session = is_django or is_sympy

    patch_text = pred.get(KEY_PREDICTION, "") or ""
    paths = _extract_patch_dirs(patch_text)
    if is_session:
        test_patch_text = _extract_test_patch_from_eval_script(
            test_spec.eval_script)
        for d in _extract_patch_dirs(test_patch_text):
            if d not in paths:
                paths.append(d)

    mode = "unittest" if is_session else "pytest"
    env_lines = [
        "",
        "# SWE-bench memory profiler configuration",
        f"export SWEBENCH_MEM_OUTPUT={output_path}",
        f"export SWEBENCH_MEM_MODE={mode}",
        "export SWEBENCH_REPO_DIR=/testbed",
    ]
    if paths:
        env_lines.append("export SWEBENCH_MEM_PATHS=" + ",".join(paths))

    # FAIL_TO_PASS filter — same trick the original tracer uses.
    # Without it the profiler instruments every test in the pytest run,
    # which on matplotlib-class instances (~600 tests in test_axes.py)
    # multiplies overhead 600x and blows past any reasonable timeout.
    fail_to_pass = list(getattr(test_spec, "FAIL_TO_PASS", None) or [])
    f2p_arg = (",".join(t.replace(",", "%2C") for t in fail_to_pass)
               if fail_to_pass else "")
    if f2p_arg:
        env_lines.append(f"export SWEBENCH_MEM_FAIL_TO_PASS='{f2p_arg}'")
    if is_session:
        env_lines.append("export PYTHONPATH=/testbed:${PYTHONPATH:-}")
    env_lines.append("")

    f2p_list = list(getattr(test_spec, "FAIL_TO_PASS", None) or [])
    apply_filter = only_f2p and not is_session and f2p_list

    lines = original_script.split("\n")
    new_lines: list[str] = []
    for line in lines:
        if is_django and "./tests/runtests.py" in line:
            line = line.replace(
                "./tests/runtests.py",
                "python /testbed/_memprof_wrapper.py ./tests/runtests.py",
            )
        if is_sympy and "bin/test" in line:
            line = line.replace(
                "bin/test",
                "python /testbed/_memprof_wrapper.py bin/test",
            )
        if apply_filter:
            line = _filter_pytest_line_to_f2p(line, f2p_list)
        new_lines.append(line)
        if line.strip() == "set -uxo pipefail":
            new_lines.extend(env_lines)
    return "\n".join(new_lines)


def _extract_memprof_from_container(
    container, base_path: str, output_dir: Path, logger=None,
):
    gz_path = base_path + ".gz" if not base_path.endswith(".gz") else base_path
    raw = _extract_binary_from_container(container, gz_path)
    if raw:
        f = output_dir / os.path.basename(gz_path)
        f.write_bytes(raw)
        return gzip.decompress(raw).decode("utf-8"), f
    plain = base_path.removesuffix(".gz")
    txt = _extract_file_from_container(container, plain)
    if txt:
        f = output_dir / os.path.basename(plain)
        f.write_text(txt)
        return txt, f
    return None, None


def _apply_patch_to_container(container, patch_file, instance_id, logger):
    copy_to_container(container, patch_file, PurePosixPath(DOCKER_PATCH))
    for cmd in GIT_APPLY_CMDS:
        val = container.exec_run(
            f"{cmd} {DOCKER_PATCH}",
            workdir=DOCKER_WORKDIR, user=DOCKER_USER,
        )
        if val.exit_code == 0:
            logger.info(f"{APPLY_PATCH_PASS}:\n{val.output.decode(UTF8)}")
            return
        logger.info(f"Failed: {cmd}")
    logger.info(f"{APPLY_PATCH_FAIL}:\n{val.output.decode(UTF8)}")
    raise EvaluationError(
        instance_id,
        f"{APPLY_PATCH_FAIL}:\n{val.output.decode(UTF8)}",
        logger,
    )


def _run_eval_script(container, eval_script: str, eval_file: Path, timeout, logger):
    eval_file.write_text(eval_script)
    copy_to_container(container, eval_file, PurePosixPath("/eval.sh"))
    return exec_run_with_timeout(container, "/bin/bash /eval.sh", timeout)


def _find_existing(out_dir: Path, basename: str):
    gz = out_dir / f"{basename}.json.gz"
    if gz.exists():
        return gz
    plain = out_dir / f"{basename}.json"
    if plain.exists():
        return plain
    return None


def run_instance_memprof(
    test_spec: TestSpec,
    pred: dict,
    rm_image: bool,
    force_rebuild: bool,
    client: docker.DockerClient,
    run_id: str,
    timeout=None,
    memprof_output_dir: str = "./memprof_results",
    dual: bool = True,
    only_f2p: bool = False,
) -> dict:
    instance_id = test_spec.instance_id
    model_name_or_path = pred.get(KEY_MODEL, "None").replace("/", "__")
    log_dir = RUN_EVALUATION_LOG_DIR / run_id / model_name_or_path / instance_id

    out_dir = Path(memprof_output_dir) / run_id / instance_id
    existing_post = _find_existing(out_dir, "memprof_output")
    existing_pre = _find_existing(out_dir, "memprof_output_pre")
    if existing_post and (not dual or existing_pre):
        return {
            "completed": True,
            "memprof_file": str(existing_post),
            "pre_memprof_file": str(existing_pre) if existing_pre else None,
            "skipped": True,
        }

    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / LOG_INSTANCE
    logger = setup_logger(instance_id, log_file)

    container = None
    post_file = None
    pre_file = None
    completed = False

    try:
        container = build_container(
            test_spec, client, run_id, logger, rm_image, force_rebuild)
        container.start()
        logger.info(f"Container for {instance_id} started: {container.id}")

        patch_file = Path(log_dir / "patch.diff")
        patch_file.write_text(pred[KEY_PREDICTION] or "")

        inject_memprof(container, test_spec=test_spec)

        if dual:
            logger.info("=== PRE-PATCH (memprof) ===")
            try:
                pre_script = patch_eval_script_for_memprof(
                    test_spec, pred,
                    output_path="/testbed/memprof_output_pre.json",
                    only_f2p=only_f2p,
                )
                pre_eval = Path(log_dir / "eval_pre.sh")
                _out, timed_out_pre, rt_pre = _run_eval_script(
                    container, pre_script, pre_eval, timeout, logger)
                logger.info(f"Pre-patch runtime: {rt_pre:_.2f}s")
                (log_dir / "test_output_pre.txt").write_text(_out or "")
                out_dir.mkdir(parents=True, exist_ok=True)
                _pre_json, pre_file = _extract_memprof_from_container(
                    container, "/testbed/memprof_output_pre.json",
                    out_dir, logger=logger,
                )
            except Exception as e:
                logger.warning(f"Pre-patch memprof failed (non-blocking): {e}")

        _apply_patch_to_container(container, patch_file, instance_id, logger)

        logger.info("=== POST-PATCH (memprof) ===")
        post_script = patch_eval_script_for_memprof(
            test_spec, pred, output_path="/testbed/memprof_output.json",
            only_f2p=only_f2p)
        eval_file = Path(log_dir / "eval.sh")
        _out, timed_out, total_rt = _run_eval_script(
            container, post_script, eval_file, timeout, logger)
        logger.info(f"Post-patch runtime: {total_rt:_.2f}s")
        (log_dir / "test_output.txt").write_text(_out or "")
        if timed_out:
            raise EvaluationError(
                instance_id,
                f"Tests timed out after {timeout} seconds.", logger,
            )

        out_dir.mkdir(parents=True, exist_ok=True)
        _post_json, post_file = _extract_memprof_from_container(
            container, "/testbed/memprof_output.json",
            out_dir, logger=logger,
        )
        completed = True

    except (EvaluationError, BuildImageError) as e:
        logger.info(traceback.format_exc())
        print(e)
    except Exception as e:
        logger.error(
            f"Error in {instance_id}: {e}\n{traceback.format_exc()}")
    finally:
        cleanup_container(client, container, logger)
        if rm_image:
            remove_image(client, test_spec.instance_image_key, logger)
        close_logger(logger)
        return {
            "completed": completed,
            "memprof_file": str(post_file) if post_file else None,
            "pre_memprof_file": str(pre_file) if pre_file else None,
            "skipped": False,
        }


def run_instances_memprof(
    predictions: dict,
    instances: list,
    cache_level: str,
    clean: bool,
    force_rebuild: bool,
    max_workers: int,
    run_id: str,
    timeout: int,
    memprof_output_dir: str = "./memprof_results",
    dual: bool = True,
    namespace=None,
    instance_image_tag: str = "latest",
    env_image_tag: str = "latest",
    only_f2p: bool = False,
):
    client = docker.from_env()
    test_specs = [
        make_test_spec(
            inst, namespace=namespace,
            instance_image_tag=instance_image_tag,
            env_image_tag=env_image_tag,
        )
        for inst in instances
    ]
    instance_image_ids = {x.instance_image_key for x in test_specs}
    existing_images = {
        tag for i in client.images.list(all=True)
        for tag in i.tags if tag in instance_image_ids
    }
    if not force_rebuild and existing_images:
        print(f"Reusing {len(existing_images)} existing instance images.")

    payloads = []
    for ts in test_specs:
        payloads.append((
            ts, predictions[ts.instance_id],
            should_remove(ts.instance_image_key, cache_level, clean,
                          existing_images),
            force_rebuild, client, run_id, timeout, memprof_output_dir,
            dual, only_f2p,
        ))

    print(f"Running {len(instances)} instances (memprof)...")
    stats = {"✓": 0, "skip": 0, "err": 0}
    pbar = tqdm(total=len(payloads), desc="Memprof mining", postfix=stats)
    lock = threading.Lock()

    def run_one(*args):
        r = run_instance_memprof(*args)
        with lock:
            if r.get("skipped"):
                stats["skip"] += 1
            elif r.get("completed") and r.get("memprof_file"):
                stats["✓"] += 1
            else:
                stats["err"] += 1
            pbar.set_postfix(stats)
            pbar.update()
        return r

    run_threadpool(run_one, payloads, max_workers)
    print(f"Done. ok={stats['✓']} skip={stats['skip']} err={stats['err']}")


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
    namespace,
    memprof_output_dir: str,
    dual: bool,
    instance_image_tag: str = "latest",
    env_image_tag: str = "latest",
    only_f2p: bool = False,
):
    assert len(run_id) > 0, "run_id required"
    predictions = get_predictions_from_file(predictions_path, dataset_name, split)
    predictions = {p[KEY_INSTANCE_ID]: p for p in predictions}
    dataset = load_swebench_dataset(dataset_name, split, instance_ids)
    dataset = [row for row in dataset if row[KEY_INSTANCE_ID] in predictions]

    if platform.system() == "Linux":
        resource.setrlimit(resource.RLIMIT_NOFILE,
                           (open_file_limit, open_file_limit))
    client = docker.from_env()
    existing_images = list_images(client)
    if not dataset:
        print("No instances to run.")
        return

    if namespace is None:
        build_env_images(
            client, dataset, force_rebuild, max_workers,
            namespace, instance_image_tag, env_image_tag,
        )
    run_instances_memprof(
        predictions, dataset, cache_level, clean, force_rebuild,
        max_workers, run_id, timeout,
        memprof_output_dir=memprof_output_dir,
        dual=dual, namespace=namespace,
        instance_image_tag=instance_image_tag,
        env_image_tag=env_image_tag,
        only_f2p=only_f2p,
    )
    clean_images(client, existing_images, cache_level, clean)


if __name__ == "__main__":
    p = ArgumentParser()
    p.add_argument("--dataset_name", default="SWE-bench/SWE-bench_Verified")
    p.add_argument("--split", default="test")
    p.add_argument("--instance_ids", nargs="+", default=None)
    p.add_argument("--predictions_path", default="gold")
    p.add_argument("--max_workers", type=int, default=1)
    p.add_argument("--force_rebuild", action=BooleanOptionalAction, default=False)
    p.add_argument("--cache_level", default="env",
                   choices=["none", "base", "env", "instance"])
    p.add_argument("--clean", action=BooleanOptionalAction, default=False)
    p.add_argument("--open_file_limit", type=int, default=4096)
    p.add_argument("--run_id", required=True)
    p.add_argument("--timeout", type=int, default=900)
    p.add_argument("--namespace", default="swebench")
    p.add_argument("--memprof_output_dir", default="./memprof_results")
    p.add_argument("--dual", action=BooleanOptionalAction, default=True)
    p.add_argument("--instance_image_tag", default="latest")
    p.add_argument("--env_image_tag", default="latest")
    p.add_argument(
        "--only_f2p", action=BooleanOptionalAction, default=False,
        help=("Pytest-only: rewrite the eval script's pytest invocation to "
              "run only FAIL_TO_PASS test ids instead of the whole test "
              "file. Huge speedup on heavy files (matplotlib's test_axes "
              "has 600+ tests; xarray has 1800+); we only need 1-3."))
    args = p.parse_args()
    main(**vars(args))
