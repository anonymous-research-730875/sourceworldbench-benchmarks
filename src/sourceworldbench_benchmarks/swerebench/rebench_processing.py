"""Orchestrate the full SWE-rebench pipeline for one or more rebench instances.

Importable API: :func:`process_rebench_instance` (rebench_base/golden_base build + validation, then
trajectory augmentations), plus its building blocks :func:`create_base_datapoints`
and :func:`create_trajectory_augmentations`.

Runnable as a CLI (``uv run src/sourceworldbench_benchmarks/swerebench/rebench_processing.py``) — see
``--help`` for instance selection (``--rebench-id`` / ``--index`` / whole split),
concurrency, and output options.
"""

import argparse
import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from datasets import load_dataset

from sourceworldbench_benchmarks.collector import collect_states
from sourceworldbench_benchmarks.image_builder import build_image
from sourceworldbench_benchmarks.parsers.junit import parse_junit_xml
from sourceworldbench_benchmarks.parsers.registry import PARSERS
from sourceworldbench_benchmarks.schema import StateDatapoint, base_key
from sourceworldbench_benchmarks.swerebench.annotate_outcomes import check_outcomes
from sourceworldbench_benchmarks.swerebench.create_docker import create_dockerimage_from_rebench
from sourceworldbench_benchmarks.swerebench.environment_patch import EnvironmentPatchError, detect_environment_patch
from sourceworldbench_benchmarks.swerebench.from_trajectory import augment_from_trajectory, iter_trajectories
from sourceworldbench_benchmarks.swerebench.golden_commit import GoldenCommitError, identify_golden_commit
from sourceworldbench_benchmarks.swerebench.to_datapoint import (
    DEFAULT_REBENCH_DATASET,
    DEFAULT_REBENCH_SPLIT,
    load_rebench_datapoint,
    rebench_base_stem,
    rebench_datapoint_row,
)
from sourceworldbench_benchmarks.swerebench.validate_outcomes import collect_validation_outcomes


class GoldenBaseMode(str, Enum):
    """How the golden_base state is materialised (chosen by :func:`process_rebench_instance`)."""

    CHECKOUT = "checkout"  # main path: `git checkout` the golden commit
    PATCHES = "patches"  # fallback: apply the fix + test_patch without a checkout
    SKIP = "skip"  # golden_base cannot be built safely and is not produced


def create_base_datapoints(
    rebench_dp: dict[str, Any],
    *,
    golden_commit: str | None,
    golden_mode: GoldenBaseMode,
    integration_failures: list[str],
    timeout: int = 600,
) -> tuple[dict[str, Any], dict[str, StateDatapoint]]:
    """Build and collect the rebench_base and golden_base standalone datapoints.

    Steps: create environments → build images → collect → check f2p/p2p.

    Two standalone single-state base datapoints (both bare, ``patch=None``, both taking
    the reserved ``__base`` suffix, distinguished by the base-id in their stem):

    - The **rebench_base** state, the eval image as it ships (already at the rebench base
      commit) with only the ``ln -s /testbed /app`` adjustment needed by ``collect`` —
      no test_patch, no checkout.  Its ``base_commit`` is the rebench base commit.  It is
      **always** produced: no checkout means the eval image's environment patch (if any)
      is never disturbed, so no edge case can break it.
    - The **golden_base** state, built according to ``golden_mode``
      (:class:`GoldenBaseMode`, chosen upstream by :func:`process_rebench_instance`):
      - ``CHECKOUT`` (main): ``git checkout`` the verified ``golden_commit`` (fix + tests
        already landed, no patch applied); ``base_commit = golden_commit``.
      - ``PATCHES`` (fallback): apply the fix + ``test_patch`` on the eval image **without a
        checkout**, so edge-case datapoints a checkout cannot handle can still be included.
        ``base_commit`` may be ``None`` (a ``none_<timestamp>`` base-id) when the golden
        commit is unknown.
      - ``SKIP``: golden_base is not built at all.

    f2p/p2p validation does **not** use the rebench_base datapoint (it has no test_patch).
    Instead the **rebench_base_with_golden_tests** state (the base commit *with*
    ``test_patch``) is collected from the eval image via :func:`collect_validation_outcomes`
    and compared against the golden_base outcomes.

    ``integration_failures`` is recorded in every produced row's metadata.  Skips f2p/p2p
    (leaving them ``None``) when golden_base was skipped/failed or validation produced no
    results.

    Returns ``(result, filled_by_id)`` so the caller can access the collected rows.
    """
    rebench_id = str(rebench_dp["instance_id"])
    repo = str(rebench_dp["repo"])
    rebench_base_commit = str(rebench_dp["base_commit"])
    test_cmd = str(rebench_dp["install_config"]["test_cmd"])
    base_stem_rebench = rebench_base_stem(repo, rebench_base_commit)
    instance_id_rebench_base = f"{base_stem_rebench}__base"
    build_golden = golden_mode != GoldenBaseMode.SKIP
    # Compute the golden_base id once (its `none_<timestamp>` base-id must be stable).
    base_stem_golden = rebench_base_stem(repo, golden_commit) if build_golden else None
    instance_id_golden_base = f"{base_stem_golden}__base" if build_golden else None
    repo_root = Path(".")

    result: dict[str, Any] = {
        "instance_id": rebench_id,
        "golden_commit": golden_commit,
        "golden_mode": golden_mode.value,
        "rebench_base_commit": rebench_base_commit,
        "integration_failures": list(integration_failures),
        "rebench_base_instance_id": instance_id_rebench_base,
        "golden_base_instance_id": instance_id_golden_base,
        "rebench_base_build_status": None,
        "golden_base_build_status": None if build_golden else "skipped",
        "rebench_base_test_run_status": None,
        "golden_base_test_run_status": None,
        "outcome_validation_status": None,
        "f2p_match": None,
        "p2p_match": None,
    }

    # 1. Write Dockerfiles.
    #    rebench_base: eval image as it ships (no test_patch, no checkout) — just the
    #    `ln -s /testbed /app` adjustment create_dockerimage_from_rebench always adds.
    create_dockerimage_from_rebench(rebench_dp, patches={}, base_stem=base_stem_rebench)
    if build_golden:
        assert base_stem_golden is not None
        if golden_mode == GoldenBaseMode.CHECKOUT:
            # main path: check out the golden commit (fix + tests already landed).
            create_dockerimage_from_rebench(
                rebench_dp, patches={}, base_stem=base_stem_golden,
                checkout=golden_commit, skip_pull=True,
            )
        else:
            # fallback: apply fix + test_patch on the eval image, no checkout.
            create_dockerimage_from_rebench(
                rebench_dp,
                patches={"patch": rebench_dp["patch"], "test_patch": rebench_dp["test_patch"]},
                base_stem=base_stem_golden,
                skip_pull=True,
            )

    # 2. Build images. rebench_base is always attempted; golden_base only when not skipped.
    try:
        build_image(
            repo_root=repo_root, instance_id=base_stem_rebench, push=False, tag="dev", build_args={}, ssh=None
        )
        result["rebench_base_build_status"] = "ok"
    except Exception as exc:
        result["rebench_base_build_status"] = f"failed: {exc}"

    if build_golden:
        assert base_stem_golden is not None
        try:
            build_image(
                repo_root=repo_root, instance_id=base_stem_golden, push=False, tag="dev", build_args={}, ssh=None
            )
            result["golden_base_build_status"] = "ok"
        except Exception as exc:
            result["golden_base_build_status"] = f"failed: {exc}"

    # 3. Collect whichever standalone datapoints built successfully.
    rows: dict[str, dict[str, Any]] = {
        instance_id_rebench_base: rebench_datapoint_row(
            rebench_dp,
            instance_id=instance_id_rebench_base,
            base_commit=rebench_base_commit,
            golden_commit=golden_commit,
            integration_failures=integration_failures,
        ),
    }

    golden_built_ok = result["golden_base_build_status"] == "ok"
    if golden_built_ok:
        assert instance_id_golden_base is not None
        rows[instance_id_golden_base] = rebench_datapoint_row(
            rebench_dp,
            instance_id=instance_id_golden_base,
            base_commit=golden_commit,
            golden_commit=golden_commit,
            integration_failures=integration_failures,
        )

    states = [StateDatapoint.model_validate(r) for r in rows.values()]
    # Register a parser per base state, keyed the same way get_parser looks it up — so the
    # unidentified-commit fallback (base_commit=None) keys on its base-id, not a shared None.
    for state in states:
        PARSERS[base_key(state)] = parse_junit_xml

    filled_by_id: dict[str, StateDatapoint] = {}
    collect_states(
        states,
        timeout=timeout,
        workers=1,
        failures_dir=Path("failures"),
        on_filled=lambda row: filled_by_id.update({row.instance_id: row}),
    )

    rebench_base_row = filled_by_id.get(instance_id_rebench_base)
    result["rebench_base_test_run_status"] = (
        "ok" if (rebench_base_row and rebench_base_row.passed_tests is not None) else "failed"
    )
    if golden_built_ok:
        assert instance_id_golden_base is not None
        golden_base_row = filled_by_id.get(instance_id_golden_base)
        result["golden_base_test_run_status"] = (
            "ok" if (golden_base_row and golden_base_row.passed_tests is not None) else "failed"
        )

    if result["golden_base_test_run_status"] != "ok":
        return result, filled_by_id

    # 4. Validate f2p / p2p against the rebench_base_with_golden_tests state (rebench base
    #    commit + test_patch), collected from the eval image itself — no checkout, so any
    #    environment patch in its working tree is preserved.
    assert instance_id_golden_base is not None
    unfixed_outcomes = collect_validation_outcomes(
        eval_image=str(rebench_dp["docker_image"]),
        test_patch=str(rebench_dp["test_patch"]),
        test_cmd=test_cmd,
        timeout=timeout,
    )
    result["outcome_validation_status"] = "ok" if unfixed_outcomes is not None else "failed"
    if unfixed_outcomes is None:
        return result, filled_by_id

    match = check_outcomes(
        unfixed_outcomes,
        filled_by_id[instance_id_golden_base].model_dump(),
        rebench_dp,
    )
    result["f2p_match"] = match.f2p_match
    result["p2p_match"] = match.p2p_match
    return result, filled_by_id


def create_trajectory_augmentations(
    rebench_dp: dict[str, Any],
    golden_base_row: dict[str, Any],
    *,
    use_reverse_patch: bool,
    workers: int = 1,
    timeout: int = 600,
    on_filled: Callable[[StateDatapoint], None] | None = None,
) -> list[dict[str, Any]]:
    """Compute and collect trajectory augmentation rows for one rebench datapoint.

    For each trajectory in the dataset that matches ``rebench_dp["instance_id"]``:
    - Computes the patch from golden_base state to trajectory state
    - Skips trajectories that produce an empty diff
    - Collects test outcomes locally

    ``use_reverse_patch`` selects the restore strategy, matching how the golden_base state
    was built (see :func:`create_base_datapoints`): ``False`` (main path — golden_base
    built by checkout) restores the pre-fix tree by checking out the rebench base commit
    and re-applying ``test_patch``; ``True`` (fallback path — golden_base built by
    applying patches) reverse-applies the fix ``patch`` with no checkout, preserving any
    environment patch.

    ``on_filled`` is invoked with each filled augmentation ``StateDatapoint`` as the
    collector completes it, letting the caller persist the resulting states.

    Returns a list of result dicts, one per trajectory, each with keys:
    ``trajectory_id``, ``instance_id``, ``patch_status``, ``test_run_status``,
    ``passed``, ``failed``.
    """
    rebench_id = str(rebench_dp["instance_id"])

    # 1. Compute augmentation rows
    aug_rows: dict[str, dict] = {}
    results: list[dict[str, Any]] = []

    for traj in iter_trajectories(rebench_id):
        trajectory_id = str(traj.get("trajectory_id", ""))
        entry: dict[str, Any] = {
            "trajectory_id": trajectory_id,
            "instance_id": None,
            "patch_status": None,
            "test_run_status": None,
            "passed": None,
            "failed": None,
        }
        try:
            if use_reverse_patch:
                # fallback: golden_base was built by applying the fix on the eval image;
                # reverse the fix to restore the base state (preserves the env patch).
                row = augment_from_trajectory(
                    golden_base_row,
                    traj,
                    fix_patch=str(rebench_dp["patch"]),
                    timeout=timeout,
                )
            else:
                # main path: golden_base was built by checkout; check out the base commit
                # and re-apply test_patch to restore the pre-fix state + shared suite.
                row = augment_from_trajectory(
                    golden_base_row,
                    traj,
                    base_commit=str(rebench_dp["base_commit"]),
                    test_patch=str(rebench_dp["test_patch"]),
                    timeout=timeout,
                )
        except Exception as exc:
            entry["patch_status"] = f"error: {exc}"
            results.append(entry)
            continue
        if row is None:
            entry["patch_status"] = "empty_diff"
            results.append(entry)
            continue
        entry["patch_status"] = "ok"
        entry["instance_id"] = row["instance_id"]
        aug_rows[row["instance_id"]] = row
        results.append(entry)

    if not aug_rows:
        return results

    # 2. Collect locally
    filled_by_id: dict[str, StateDatapoint] = {}

    def _on_filled(row: StateDatapoint) -> None:
        filled_by_id[row.instance_id] = row
        if on_filled is not None:
            on_filled(row)

    states = [StateDatapoint.model_validate(r) for r in aug_rows.values()]
    # Register a parser per augmentation, keyed the same way get_parser looks it up (all
    # augmentations of a base resolve to the base's key — see sourceworldbench_benchmarks.schema.base_key).
    for state in states:
        PARSERS[base_key(state)] = parse_junit_xml

    collect_states(
        states,
        timeout=timeout,
        workers=workers,
        failures_dir=Path("failures"),
        on_filled=_on_filled,
    )

    for entry in results:
        iid = entry.get("instance_id")
        if iid is None:
            continue
        filled = filled_by_id.get(iid)
        if filled is None:
            entry["test_run_status"] = "failed"
        else:
            entry["test_run_status"] = "ok" if filled.passed_tests is not None else "no_results"
            entry["passed"] = len(filled.passed_tests or [])
            entry["failed"] = len(filled.failed_tests or [])

    return results


def process_rebench_instance(
    rebench_id: str,
    *,
    workers: int = 1,
    timeout: int = 600,
    on_state: Callable[[StateDatapoint], None] | None = None,
) -> dict[str, Any]:
    """Full pipeline for one rebench instance: rebench_base/golden_base then trajectories.

    1. Load the rebench datapoint; verify its golden commit against GitHub and inspect the
       eval image for an environment patch.  Neither is fatal: instead of skipping the
       instance, any detected failure is recorded in ``integration_failures`` and the
       golden_base build mode is chosen accordingly (see :func:`create_base_datapoints`):

       - golden commit unidentified (any :class:`GoldenCommitError`) ⇒ ``PATCHES``
         with ``base_commit=None`` (a ``none_<timestamp>`` base-id).
       - environment patch present ⇒ ``PATCHES`` (keeps the normal id/base_commit).
       - environment-patch inspection failed ⇒ ``SKIP`` (golden_base not built).
       - otherwise ⇒ ``CHECKOUT`` (main path).

       The precedence: an unidentified golden commit wins the *build* decision (its
       patch build already covers a present environment patch), but **all** detected
       failures are recorded.  ``rebench_base`` is always produced regardless.
    2. Run ``create_base_datapoints``.
    3. If the golden_base tests pass, run ``create_trajectory_augmentations`` (reverse-patch
       restore when golden_base was built via the patches fallback).

    ``on_state`` is invoked with every filled ``StateDatapoint`` produced along the way
    (rebench_base, golden_base, and each trajectory augmentation).

    Returns a dict with ``golden_commit`` (None when unidentified), ``integration_failures``
    (the list of detected edge-case failure types, empty when clean), ``base_datapoints``
    (result from step 2), and ``trajectories`` (list from step 3, or None if golden_base
    was skipped/failed).
    """
    dp = load_rebench_datapoint(rebench_id)

    golden_result = identify_golden_commit(dp)
    environment_patch_error = detect_environment_patch(str(dp["docker_image"]))

    integration_failures: list[str] = []
    if isinstance(golden_result, GoldenCommitError):
        integration_failures.append(golden_result.value)
        golden_commit: str | None = None
    else:
        golden_commit = golden_result
    if environment_patch_error is not None:
        integration_failures.append(environment_patch_error.value)

    if golden_commit is None:
        golden_mode = GoldenBaseMode.PATCHES
    elif environment_patch_error is EnvironmentPatchError.ENVIRONMENT_PATCH_PRESENT:
        golden_mode = GoldenBaseMode.PATCHES
    elif environment_patch_error is EnvironmentPatchError.INSPECT_FAILED:
        golden_mode = GoldenBaseMode.SKIP
    else:
        golden_mode = GoldenBaseMode.CHECKOUT

    base_datapoints, filled_by_id = create_base_datapoints(
        dp,
        golden_commit=golden_commit,
        golden_mode=golden_mode,
        integration_failures=integration_failures,
        timeout=timeout,
    )
    if on_state is not None:
        for state in filled_by_id.values():
            on_state(state)

    result: dict[str, Any] = {
        "golden_commit": golden_commit,
        "integration_failures": integration_failures,
        "base_datapoints": base_datapoints,
        "trajectories": None,
    }

    if base_datapoints.get("golden_base_test_run_status") != "ok":
        return result

    golden_base_state = filled_by_id[base_datapoints["golden_base_instance_id"]]
    result["trajectories"] = create_trajectory_augmentations(
        dp,
        golden_base_state.model_dump(),
        use_reverse_patch=(golden_mode == GoldenBaseMode.PATCHES),
        workers=workers,
        timeout=timeout,
        on_filled=on_state,
    )
    return result


def select_rebench_ids(
    ds: Any,
    *,
    rebench_ids: list[str] | None = None,
    indices: list[int] | None = None,
    limit: int | None = None,
) -> list[str]:
    """Resolve the CLI selectors into an ordered list of bare rebench instance ids.

    Selection precedence:
    - ``rebench_ids``: taken verbatim (validated against the dataset).
    - ``indices``: positional rows ``ds[i]``.
    - neither: every instance in the dataset, in order.

    ``limit`` truncates the resolved list (applied last), so ``--limit`` bounds any of
    the three modes.
    """
    if rebench_ids:
        available = {str(row["instance_id"]) for row in ds}
        missing = [rid for rid in rebench_ids if rid not in available]
        if missing:
            raise KeyError(f"instance_id(s) not found in dataset: {', '.join(missing)}")
        selected = list(rebench_ids)
    elif indices:
        selected = [str(ds[i]["instance_id"]) for i in indices]
    else:
        selected = [str(row["instance_id"]) for row in ds]

    if limit is not None:
        selected = selected[:limit]
    return selected


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the full SWE-rebench pipeline (rebench_base/golden_base build + validation, then\n"
            "trajectory augmentations) for one or more rebench instances and append a\n"
            "JSONL result record per instance.\n"
            "\n"
            "For each selected instance process_rebench_instance:\n"
            "  1. Loads the rebench datapoint and verifies its golden commit vs GitHub.\n"
            "  2. Builds the rebench_base + golden_base images, collects both as standalone\n"
            "     datapoints, and validates f2p/p2p against the golden_base container.\n"
            "  3. If the golden_base tests pass, generates and collects trajectory\n"
            "     augmentation datapoints.\n"
            "\n"
            "Choose WHICH instances to process with exactly one selector (--rebench-id,\n"
            "--index, or neither = the whole split). Note: this entrypoint builds from the\n"
            "pre-built SWE-rebench eval image; the create-rebench-examples skill builds the\n"
            "rebench_base/golden_base images from scratch instead."
        ),
        epilog=(
            "examples:\n"
            "  # Process a single instance by its bare rebench id\n"
            "  uv run src/sourceworldbench_benchmarks/swerebench/rebench_processing.py \\\n"
            "      --rebench-id 12rambau__sepal_ui-411\n"
            "\n"
            "  # Reproduce the old hardcoded behaviour (dataset row 154)\n"
            "  uv run src/sourceworldbench_benchmarks/swerebench/rebench_processing.py --index 154\n"
            "\n"
            "  # Process the first 10 instances of the split with 4 workers\n"
            "  uv run src/sourceworldbench_benchmarks/swerebench/rebench_processing.py --limit 10 --workers 4\n"
            "\n"
            "  # Process the entire split, writing results to a chosen file\n"
            "  uv run src/sourceworldbench_benchmarks/swerebench/rebench_processing.py --out run.jsonl"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--rebench-id",
        action="append",
        dest="rebench_ids",
        metavar="ID",
        help=(
            "Bare rebench instance_id to process, e.g. 12rambau__sepal_ui-411. Repeat the "
            "flag to process several. Mutually exclusive with --index; if neither is given, "
            "every instance in the split is processed."
        ),
    )
    selection.add_argument(
        "--index",
        action="append",
        type=int,
        dest="indices",
        metavar="N",
        help="Positional dataset row(s) to process (ds[N]). Repeatable. Mutually exclusive with --rebench-id.",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Process at most N of the selected instances (applied after selection).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of parallel collection workers passed to collect_states (default: %(default)s).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        metavar="SECONDS",
        help="Per-container test-run timeout in seconds (default: %(default)s).",
    )
    parser.add_argument(
        "--rebench-dataset",
        default=DEFAULT_REBENCH_DATASET,
        dest="rebench_dataset",
        help="Source SWE-rebench dataset to read instances from (default: %(default)s).",
    )
    parser.add_argument(
        "--rebench-split",
        default=DEFAULT_REBENCH_SPLIT,
        dest="rebench_split",
        help="Split of the source rebench dataset to read (default: %(default)s).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        dest="out",
        help="JSONL file to append result records to (default: rebench_run_<UTC timestamp>.jsonl).",
    )
    parser.add_argument(
        "--states-out",
        type=Path,
        default=None,
        dest="states_out",
        help=(
            "JSONL file to append every filled single-state datapoint to (rebench_base, golden_base, "
            "and each "
            "trajectory augmentation). When omitted, filled states are not saved."
        ),
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Keep going when one instance raises, recording an error record, instead of aborting the run.",
    )
    return parser


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger(__name__)

    args = _build_arg_parser().parse_args()

    run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    log_path = args.out if args.out is not None else Path(f"rebench_run_{run_ts}.jsonl")
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def _append(record: dict[str, Any]) -> None:
        with log_path.open("a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")

    on_state: Callable[[StateDatapoint], None] | None = None
    if args.states_out is not None:
        states_path = args.states_out
        states_path.parent.mkdir(parents=True, exist_ok=True)

        def _write_state(state: StateDatapoint) -> None:
            with states_path.open("a") as fh:
                fh.write(state.model_dump_json() + "\n")

        on_state = _write_state

    log.info("Loading %s [%s]...", args.rebench_dataset, args.rebench_split)
    ds = load_dataset(args.rebench_dataset, split=args.rebench_split)
    rebench_ids = select_rebench_ids(
        ds,
        rebench_ids=args.rebench_ids,
        indices=args.indices,
        limit=args.limit,
    )

    log.info("Starting rebench pipeline run → %s (%d instance(s))", log_path, len(rebench_ids))
    for rebench_id in rebench_ids:
        log.info("Processing %s", rebench_id)
        try:
            result = process_rebench_instance(rebench_id, workers=args.workers, timeout=args.timeout, on_state=on_state)
        except Exception as exc:
            log.exception("ERROR processing %s: %s", rebench_id, exc)
            _append({"status": "error", "instance_id": rebench_id, "error": str(exc)})
            if args.continue_on_error:
                continue
            raise

        _append({"status": "ok", "instance_id": rebench_id, **result})
        bg = result.get("base_datapoints") or {}
        log.info(
            "%s  golden_commit=%s failures=%s mode=%s f2p=%s p2p=%s trajectories=%s",
            rebench_id,
            result.get("golden_commit"),
            result.get("integration_failures"),
            bg.get("golden_mode"),
            bg.get("f2p_match"),
            bg.get("p2p_match"),
            len(result["trajectories"] or []),
        )

    log.info("Done. Results written to %s", log_path)
