"""run_cprofile.py -- Inject cProfile per-test profiling, no other instrumentation.

C-level cProfile gives accurate per-function exclusive/inclusive time with
~3-10% overhead, an order of magnitude better than the line-mode `sys.settrace`
based tracer. Output is per-method ONLY (no per-line, no memory).

Mirrors `run_walltime.py`'s dual-side flow (pre + post in one container).
Reuses helpers from `run_traced.py` and the same patch-dir extraction so the
profiler scope matches the existing tracer's "in-project" set.

Output (per instance):

    cprofile_results/<run_id>/<instance_id>/cprofile_output.json[.gz]
    cprofile_results/<run_id>/<instance_id>/cprofile_output_pre.json[.gz]
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

PROFILER_SOURCE = (
    Path(__file__).parent.parent / "tracers" / "cprofile_test_profiler.py")


def _get_profiler_source() -> str:
    return PROFILER_SOURCE.read_text(encoding="utf-8")


def inject_profiler(container, test_spec: TestSpec, repo_dir: str = "/testbed"):
    """Drop the cProfile module into the container.

    pytest repos: as `conftest.py`.
    Django/sympy: as `_swebench_profiler.py` + a wrapper that imports it
    before running the real test command.
    """
    code = _get_profiler_source()
    if _is_session_trace_instance(test_spec):
        _copy_string_to_container(
            container, code, f"{repo_dir}/_swebench_profiler.py")
        wrapper = (
            "import sys\n"
            "import os\n"
            "import runpy\n"
            "import _swebench_profiler  # installs unittest.TestCase.run patch\n"
            "sys.argv = sys.argv[1:]\n"
            "script_dir = os.path.dirname(os.path.abspath(sys.argv[0]))\n"
            "if script_dir not in sys.path:\n"
            "    sys.path.insert(0, script_dir)\n"
            "runpy.run_path(sys.argv[0], run_name='__main__')\n"
        )
        _copy_string_to_container(
            container, wrapper, f"{repo_dir}/_profiler_wrapper.py")
    else:
        _copy_string_to_container(container, code, f"{repo_dir}/conftest.py")


def patch_eval_script_for_profiler(
    test_spec: TestSpec,
    pred: dict,
    output_path: str = "/testbed/cprofile_output.json",
) -> str:
    """Inject SWEBENCH_PROF_* env vars and (for Django/sympy) route the test
    runner through the profiler wrapper.

    The patch-dirs filter is set so the cProfile output only contains
    in-project functions, matching the existing tracer's scope. Without
    this filter, cProfile would dump tens of thousands of entries from
    pytest internals, stdlib, and third-party packages.
    """
    original_script = test_spec.eval_script
    is_django = _is_django_instance(test_spec)
    is_sympy = _is_sympy_instance(test_spec)
    is_session = is_django or is_sympy

    patch_text = pred.get(KEY_PREDICTION, "") or ""
    prof_paths = _extract_patch_dirs(patch_text)
    # Session-mode repos need the test files visible too so that test_*
    # functions show up under their FQN; otherwise pure-test-only filters
    # would hide most of the profile.
    if is_session:
        test_patch_text = _extract_test_patch_from_eval_script(
            test_spec.eval_script)
        for d in _extract_patch_dirs(test_patch_text):
            if d not in prof_paths:
                prof_paths.append(d)

    mode = "unittest" if is_session else "pytest"
    env_lines = [
        "",
        "# SWE-bench cProfile profiler configuration",
        f"export SWEBENCH_PROF_OUTPUT={output_path}",
        f"export SWEBENCH_PROF_MODE={mode}",
        "export SWEBENCH_REPO_DIR=/testbed",
    ]
    if prof_paths:
        env_lines.append("export SWEBENCH_PROF_PATHS=" + ",".join(prof_paths))
    if is_session:
        env_lines.append("export PYTHONPATH=/testbed:${PYTHONPATH:-}")
    env_lines.append("")

    lines = original_script.split("\n")
    new_lines: list[str] = []
    for line in lines:
        if is_django and "./tests/runtests.py" in line:
            line = line.replace(
                "./tests/runtests.py",
                "python /testbed/_profiler_wrapper.py ./tests/runtests.py",
            )
        if is_sympy and "bin/test" in line:
            line = line.replace(
                "bin/test",
                "python /testbed/_profiler_wrapper.py bin/test",
            )
        new_lines.append(line)
        if line.strip() == "set -uxo pipefail":
            new_lines.extend(env_lines)
    return "\n".join(new_lines)


def _extract_profile_from_container(
    container, base_path: str, output_dir: Path, logger=None,
):
    """Pull <base_path>(.gz) out of the container."""
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


def run_instance_cprofile(
    test_spec: TestSpec,
    pred: dict,
    rm_image: bool,
    force_rebuild: bool,
    client: docker.DockerClient,
    run_id: str,
    timeout=None,
    profile_output_dir: str = "./cprofile_results",
    dual: bool = True,
) -> dict:
    """Run one instance through the cProfile pipeline.

    Resumable: if both expected output files already exist, skips the
    container build.
    """
    instance_id = test_spec.instance_id
    model_name_or_path = pred.get(KEY_MODEL, "None").replace("/", "__")
    log_dir = RUN_EVALUATION_LOG_DIR / run_id / model_name_or_path / instance_id

    out_dir = Path(profile_output_dir) / run_id / instance_id
    existing_post = _find_existing(out_dir, "cprofile_output")
    existing_pre = _find_existing(out_dir, "cprofile_output_pre")
    if existing_post and (not dual or existing_pre):
        return {
            "completed": True,
            "profile_file": str(existing_post),
            "pre_profile_file": str(existing_pre) if existing_pre else None,
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

        inject_profiler(container, test_spec=test_spec)

        if dual:
            logger.info("=== PRE-PATCH (cProfile) ===")
            try:
                pre_script = patch_eval_script_for_profiler(
                    test_spec, pred,
                    output_path="/testbed/cprofile_output_pre.json",
                )
                pre_eval = Path(log_dir / "eval_pre.sh")
                _out, timed_out_pre, rt_pre = _run_eval_script(
                    container, pre_script, pre_eval, timeout, logger)
                logger.info(f"Pre-patch runtime: {rt_pre:_.2f}s")
                (log_dir / "test_output_pre.txt").write_text(_out or "")
                out_dir.mkdir(parents=True, exist_ok=True)
                _pre_json, pre_file = _extract_profile_from_container(
                    container, "/testbed/cprofile_output_pre.json",
                    out_dir, logger=logger,
                )
                if not _pre_json:
                    logger.info(
                        "No pre-patch profile output found (expected when "
                        "FAIL_TO_PASS pre-runs crash before tests complete)"
                    )
            except Exception as e:
                logger.warning(f"Pre-patch profile failed (non-blocking): {e}")

        _apply_patch_to_container(container, patch_file, instance_id, logger)

        logger.info("=== POST-PATCH (cProfile) ===")
        post_script = patch_eval_script_for_profiler(
            test_spec, pred, output_path="/testbed/cprofile_output.json")
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
        _post_json, post_file = _extract_profile_from_container(
            container, "/testbed/cprofile_output.json",
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
            "profile_file": str(post_file) if post_file else None,
            "pre_profile_file": str(pre_file) if pre_file else None,
            "skipped": False,
        }


def run_instances_cprofile(
    predictions: dict,
    instances: list,
    cache_level: str,
    clean: bool,
    force_rebuild: bool,
    max_workers: int,
    run_id: str,
    timeout: int,
    profile_output_dir: str = "./cprofile_results",
    dual: bool = True,
    namespace=None,
    instance_image_tag: str = "latest",
    env_image_tag: str = "latest",
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
            force_rebuild, client, run_id, timeout, profile_output_dir,
            dual,
        ))

    print(f"Running {len(instances)} instances (cProfile)...")
    stats = {"✓": 0, "skip": 0, "err": 0}
    pbar = tqdm(total=len(payloads), desc="cProfile mining", postfix=stats)
    lock = threading.Lock()

    def run_one(*args):
        r = run_instance_cprofile(*args)
        with lock:
            if r.get("skipped"):
                stats["skip"] += 1
            elif r.get("completed") and r.get("profile_file"):
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
    profile_output_dir: str,
    dual: bool,
    instance_image_tag: str = "latest",
    env_image_tag: str = "latest",
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
    run_instances_cprofile(
        predictions, dataset, cache_level, clean, force_rebuild,
        max_workers, run_id, timeout,
        profile_output_dir=profile_output_dir,
        dual=dual, namespace=namespace,
        instance_image_tag=instance_image_tag,
        env_image_tag=env_image_tag,
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
    p.add_argument("--profile_output_dir", default="./cprofile_results")
    p.add_argument("--dual", action=BooleanOptionalAction, default=True)
    p.add_argument("--instance_image_tag", default="latest")
    p.add_argument("--env_image_tag", default="latest")
    args = p.parse_args()
    main(**vars(args))
