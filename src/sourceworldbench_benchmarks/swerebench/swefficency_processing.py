"""Full processing pipeline for one swefficiency instance.

States
------
Every state of a swefficiency instance shares the same base commit and the same base
image; they differ only in the patch applied on top before tests run. Three named
states, each a single-state :class:`~sourceworldbench_benchmarks.schema.StateDatapoint`, keep the
distinction explicit (``instance_id`` suffix in parentheses):

  - **base state** (``__base``): the base commit unmodified (``patch=None``). The base
    image already carries the PASS_TO_PASS suite the other states are measured against.
  - **fixed state** (``__fixed``): the base commit with the *expert* performance patch
    applied, plus a ``rebuild_command`` the collect worker runs after ``git apply``.
  - **prediction state** (``__prediction_<run>``): the base commit with one
    model-harness run's predicted patch applied, plus the same ``rebuild_command``; one
    per prediction run of the instance. Built from the swefficiency prediction files via
    :mod:`sourceworldbench_benchmarks.swerebench.from_predictions`.

Pipeline
--------
:func:`process_sweff_instance` does everything end-to-end for a single raw swefficiency
dataset row:

  1. Writes the base Docker environment (``Dockerfile`` + ``run_tests.sh``) under
     ``environments/<stem>/`` via :func:`create_dockerimage_from_rebench`.
  2. Builds — and optionally pushes — the base image via
     :func:`sourceworldbench_benchmarks.image_builder.build_image`.
  3. Constructs the base state and the fixed state, both pointing at that image.
  4. When prediction files are supplied, constructs one prediction state per prediction
     of the instance (:func:`create_prediction_augmentations`). These states carry no
     test outcomes: swefficiency ships no data to validate them against, so the pipeline
     runs no tests for them.
  5. Appends every constructed state to the local single-state JSONL file. Nothing is
     pushed to Hugging Face.

Runnable as a CLI (``uv run src/sourceworldbench_benchmarks/swerebench/swefficency_processing.py``):
processes one instance (``--instance-id``) or the whole dataset, and (given
``--prediction-file``) emits prediction states alongside the base and fixed states.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any

from datasets import load_dataset

from sourceworldbench_benchmarks.image_builder import build_image
from sourceworldbench_benchmarks.parsers.junit import parse_junit_xml
from sourceworldbench_benchmarks.parsers.registry import PARSERS
from sourceworldbench_benchmarks.schema import BASE_ID_METADATA_KEY, StateDatapoint
from sourceworldbench_benchmarks.swerebench.create_docker import (
    CONDA_BASE_REBENCH,
    create_dockerimage_from_rebench,
    render_test_command,
)
from sourceworldbench_benchmarks.swerebench.from_predictions import (
    DEFAULT_CACHE_DIR,
    PredictionContext,
    prediction_augmentation_datapoint,
)
from sourceworldbench_benchmarks.swerebench.from_sweff import (
    base_stem_for,
    iid_base,
    iid_fixed,
    sweff_row_to_dp,
    wrap_rebuild_cmd,
)

log = logging.getLogger(__name__)

DATASET = "swefficiency/swefficiency"
SPLIT = "test"

# Key under which the post-patch rebuild shell command is stored on augmentation
# rows. Must stay in sync with sourceworldbench_benchmarks.execution.REBUILD_COMMAND_METADATA_KEY.
REBUILD_COMMAND_METADATA_KEY = "rebuild_command"

# Default local single-state JSONL that new datapoints are appended to.
DEFAULT_SINGLE_STATE_PATH = Path("data") / "swefficiency" / "single_state.jsonl"


def process_sweff_instance(
    row: dict[str, Any],
    *,
    repo_root: Path | None = None,
    push: bool = True,
    tag: str = "dev",
    single_state_path: Path | None = None,
    predictions: PredictionContext | None = None,
) -> tuple[StateDatapoint, StateDatapoint, list[StateDatapoint]]:
    """End-to-end pipeline for one swefficiency dataset row.

    Steps
    -----
    1. Write the base Docker environment (Dockerfile + run_tests.sh) under
       ``environments/<stem>/``.
    2. Build the base image via :func:`build_image`; with ``push=True`` (default)
       also push it to the registry and use the digest-pinned reference.
    3. Build the base state and the fixed state, both pointing at the resulting
       image. The fixed state carries the expert perf patch plus a
       ``rebuild_command`` metadata entry consumed at collect-time.
    4. When ``predictions`` is given, build one prediction state per prediction of
       this instance (see :func:`create_prediction_augmentations`). No tests are run.
    5. Append every constructed state to ``single_state_path`` (default
       ``data/swefficiency/single_state.jsonl``) — nothing is pushed to
       Hugging Face.

    Registers the JUnit parser for ``(repo, base_commit)`` in
    :data:`~sourceworldbench_benchmarks.parsers.registry.PARSERS` so subsequent collects in
    this process can parse the output.

    Parameters
    ----------
    row:
        A raw row from the ``swefficiency/swefficiency`` dataset.
    repo_root:
        Repository root used to resolve ``environments/`` and the local single-state
        JSONL path. Defaults to the current working directory.
    push:
        Push the built image to the registry and use the digest-pinned reference
        for the ``container`` field. Default ``True``. When ``False`` the image
        is built locally only.
    tag:
        Image tag when pushing (or naming the local build). Default ``"dev"``.
    single_state_path:
        Absolute path to the JSONL file to append the datapoints to.
        Defaults to ``<repo_root>/data/swefficiency/single_state.jsonl``.
    predictions:
        A :class:`~sourceworldbench_benchmarks.swerebench.from_predictions.PredictionContext`
        over one or more prediction files. When given, prediction states for this
        instance are constructed and appended too. When ``None`` (default) only the
        base and fixed states are produced.

    Returns
    -------
    tuple[StateDatapoint, StateDatapoint, list[StateDatapoint]]
        ``(base_state, fixed_state, prediction_states)`` — all with ``container`` set
        to the built or pushed reference. The prediction-state list is empty when
        ``predictions`` is ``None`` or the instance has no predictions.
    """
    dp = sweff_row_to_dp(row)

    repo = dp["repo"]
    base_commit = dp["base_commit"]
    conda_base = dp.get("_conda_base", CONDA_BASE_REBENCH)
    test_cmd = dp["install_config"]["test_cmd"]

    stem = base_stem_for(dp)
    test_scope = ["tests/"]
    # swefficiency's own harness runs every script with bash and never `/bin/sh`,
    # so its images' conda hooks assume bash. See render_test_command's `use_bash`.
    command = render_test_command(test_cmd, conda_base=conda_base, use_bash=True)
    rebuild_command = wrap_rebuild_cmd(dp)

    resolved_repo_root = (
        Path(repo_root).resolve() if repo_root is not None else Path.cwd()
    )

    # ------------------------------------------------------------------
    # 1. Write base Docker environment
    # ------------------------------------------------------------------
    log.info(
        "[%s] Writing base environment → environments/%s/", dp["instance_id"], stem
    )
    env_dir = create_dockerimage_from_rebench(
        dp,
        patches={},  # no patches baked into the base image
        base_stem=stem,
        conda_base=conda_base,
        repo_root=resolved_repo_root,
        skip_pull=True,  # no need to pre-pull; build_image below will pull
    )
    log.info("[%s]   → %s", dp["instance_id"], env_dir)

    # ------------------------------------------------------------------
    # 2. Build (and optionally push) the base image
    # ------------------------------------------------------------------
    log.info("[%s] %s image (tag=%s) …", stem, "Pushing" if push else "Building", tag)
    container_ref = build_image(
        repo_root=resolved_repo_root,
        instance_id=stem,
        push=push,
        tag=tag,
        build_args={},
        ssh=None,
    )
    log.info("[%s]   container → %s", stem, container_ref)

    # ------------------------------------------------------------------
    # Register the JUnit parser (swefficiency always emits JUnit XML).
    # Both base and fixed (augmentation of base) share this parser via base_key.
    # ------------------------------------------------------------------
    parser_key = (repo, base_commit)
    if parser_key not in PARSERS:
        log.info(
            "[%s] Registering parse_junit_xml for %s", dp["instance_id"], parser_key
        )
        PARSERS[parser_key] = parse_junit_xml

    # ------------------------------------------------------------------
    # 3. Construct StateDatapoint rows
    # ------------------------------------------------------------------
    base_metadata: dict[str, Any] = {
        BASE_ID_METADATA_KEY: base_commit[:8],
        "swefficiency": {
            "dataset": DATASET,
            "instance_id": dp["instance_id"],
            "variant": "base",
        },
    }
    base_datapoint = StateDatapoint(
        instance_id=iid_base(dp),
        repo=repo,
        base_commit=base_commit,
        patch=None,
        container=container_ref,
        command=command,
        test_scope=test_scope,
        metadata=base_metadata,
    )

    fixed_metadata: dict[str, Any] = {
        BASE_ID_METADATA_KEY: base_commit[:8],
        "swefficiency": {
            "dataset": DATASET,
            "instance_id": dp["instance_id"],
            "variant": "fixed",
        },
        REBUILD_COMMAND_METADATA_KEY: rebuild_command,
    }
    fixed_datapoint = StateDatapoint(
        instance_id=iid_fixed(dp),
        repo=repo,
        base_commit=base_commit,
        patch=dp["patch"],
        container=container_ref,
        command=command,
        test_scope=test_scope,
        metadata=fixed_metadata,
    )

    # ------------------------------------------------------------------
    # 4. Append base + fixed rows to the local single-state JSONL
    # ------------------------------------------------------------------
    out_path = (
        Path(single_state_path)
        if single_state_path is not None
        else resolved_repo_root / DEFAULT_SINGLE_STATE_PATH
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a", encoding="utf-8") as fh:
        fh.write(base_datapoint.model_dump_json() + "\n")
        fh.write(fixed_datapoint.model_dump_json() + "\n")
    log.info("[%s] Appended base + fixed rows → %s", dp["instance_id"], out_path)

    # ------------------------------------------------------------------
    # 5. Prediction states (optional; no test execution)
    # ------------------------------------------------------------------
    prediction_states: list[StateDatapoint] = []
    if predictions is not None:
        prediction_states = create_prediction_augmentations(
            base_datapoint,
            predictions,
            rebuild_command=rebuild_command,
            single_state_path=out_path,
        )

    return base_datapoint, fixed_datapoint, prediction_states


def create_prediction_augmentations(
    base_datapoint: StateDatapoint,
    predictions: PredictionContext,
    *,
    rebuild_command: str,
    single_state_path: Path,
) -> list[StateDatapoint]:
    """Build and persist one prediction state per model prediction of an instance.

    For each prediction of ``base_datapoint``'s instance (across every file in
    ``predictions``) it constructs a prediction state — the base commit with the model's
    ``model_patch`` applied on top, sharing the base state's image, command, test_scope,
    and the ``rebuild_command`` run after ``git apply`` — and appends it to
    ``single_state_path``. The joined per-patch facts are recorded under
    ``metadata['swefficiency']['prediction']``.

    No tests are run: swefficiency ships no data to validate a prediction state against,
    so its outcome fields stay ``None`` (a partial row).

    Returns the constructed prediction states (empty when the instance has no
    predictions).
    """
    sweff = (base_datapoint.metadata or {}).get("swefficiency") or {}
    instance_id = sweff.get("instance_id")
    if not instance_id:
        raise ValueError(
            f"{base_datapoint.instance_id!r} has no metadata['swefficiency']['instance_id']; "
            "cannot look up its predictions"
        )

    prediction_states: list[StateDatapoint] = []
    with single_state_path.open("a", encoding="utf-8") as fh:
        for source_stem, enriched in predictions.iter_predictions(str(instance_id)):
            state = prediction_augmentation_datapoint(
                base_datapoint,
                enriched,
                rebuild_command=rebuild_command,
                source_stem=source_stem,
            )
            fh.write(state.model_dump_json() + "\n")
            prediction_states.append(state)

    log.info(
        "[%s] Appended %d prediction state(s) → %s",
        instance_id,
        len(prediction_states),
        single_state_path,
    )
    return prediction_states


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )

    parser = argparse.ArgumentParser(
        description=(
            "Full swefficiency pipeline: write Dockerfile, build (+push) the base "
            "image, construct the base + fixed StateDatapoint rows (and, with "
            "--prediction-file, one prediction augmentation per model prediction), "
            "and append them to the local single-state JSONL. Nothing is pushed to HF."
        ),
    )
    parser.add_argument(
        "--instance-id",
        metavar="IID",
        help=(
            "Process only this instance (e.g. pydata__xarray-7472). "
            "Omit to process the whole dataset."
        ),
    )
    parser.add_argument(
        "--no-push",
        action="store_true",
        help="Build the image locally without pushing to the registry.",
    )

    parser.add_argument(
        "--tag",
        default="dev",
        help="Image tag when pushing / naming the local build (default: dev).",
    )
    parser.add_argument(
        "--single-state-path",
        metavar="PATH",
        type=Path,
        default=None,
        help=(
            "JSONL file to append base + fixed rows to. "
            "Defaults to <repo>/data/swefficiency/single_state.jsonl."
        ),
    )
    parser.add_argument(
        "--limit",
        metavar="N",
        type=int,
        default=None,
        help="Stop after processing this many instances (useful for testing).",
    )
    parser.add_argument(
        "--repo-root",
        metavar="PATH",
        type=Path,
        default=None,
        help="Repository root for environments/ and the single-state JSONL. Defaults to cwd.",
    )
    parser.add_argument(
        "--prediction-file",
        metavar="NAME",
        action="append",
        dest="prediction_files",
        default=None,
        help=(
            "A swefficiency prediction file to build prediction states from, given as a "
            "stem, a file name, or a repo path (e.g. oh_gpt5 | oh_gpt5.jsonl | "
            "predictions/converted/oh_gpt5.jsonl). Repeat to include several. When omitted "
            "(and without --all-predictions), only base + fixed states are produced."
        ),
    )
    parser.add_argument(
        "--all-predictions",
        action="store_true",
        help=(
            "Build prediction states from every prediction file in the swefficiency repo, "
            "not just those named with --prediction-file (the two combine and de-duplicate)."
        ),
    )
    parser.add_argument(
        "--cache-dir",
        metavar="PATH",
        type=Path,
        default=DEFAULT_CACHE_DIR,
        help=(
            "Disk cache for prediction/report files fetched from GitHub "
            "(default: %(default)s). Pass '' to disable caching."
        ),
    )
    args = parser.parse_args()

    predictions: PredictionContext | None = None
    if args.prediction_files or args.all_predictions:
        predictions = PredictionContext.create(
            args.prediction_files or [],
            cache_dir=args.cache_dir or None,
            include_all=args.all_predictions,
        )
        log.info(
            "Loaded %d prediction file(s) for prediction states.",
            len(predictions.prediction_files),
        )

    log.info("Loading %s / %s …", DATASET, SPLIT)
    ds = load_dataset(DATASET, split=SPLIT)
    log.info("  %d instances", len(ds))

    ok = failed = 0
    for row in ds:
        if args.limit is not None and ok + failed >= args.limit:
            break
        row = dict(row)
        if args.instance_id and row["instance_id"] != args.instance_id:
            continue
        try:
            base_dp, _, prediction_states = process_sweff_instance(
                row,
                repo_root=args.repo_root,
                push=not args.no_push,
                tag=args.tag,
                single_state_path=args.single_state_path,
                predictions=predictions,
            )
            log.info("  ✓ %s (+%d prediction state(s))", base_dp.instance_id, len(prediction_states))
            ok += 1
        except Exception:
            log.exception("  ✗ %s — skipping", row["instance_id"])
            failed += 1

    log.info("Done. ok=%d  failed=%d", ok, failed)


if __name__ == "__main__":
    main()
