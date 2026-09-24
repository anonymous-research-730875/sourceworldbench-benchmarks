"""Build sourceworldbench-benchmarks StateDatapoint JSON rows from SWE-rebench datapoints."""

import argparse
import json
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from datasets import load_dataset

from sourceworldbench_benchmarks.dataset_io import (
    DEFAULT_DATASET_REPO_ID,
    commit_description,
    load_existing_rows,
    push_repo_for,
    push_rows,
    upsert_rows,
)
from sourceworldbench_benchmarks.image_builder import local_tag
from sourceworldbench_benchmarks.schema import BASE_ID_METADATA_KEY, StateDatapoint
from sourceworldbench_benchmarks.swerebench.create_docker import render_test_command
from sourceworldbench_benchmarks.swerebench.environment_patch import detect_environment_patch
from sourceworldbench_benchmarks.swerebench.golden_commit import GoldenCommitError, identify_golden_commit

DEFAULT_REBENCH_DATASET = "nebius/SWE-rebench"
DEFAULT_REBENCH_SPLIT = "filtered"
DEFAULT_REBENCH_TEST_SCOPE = ["tests/"]


def rebench_base_id(commit: str | None) -> str:
    """Return the ``<base-id>`` instance-id segment for a base commit.

    The base commit's short (8-char) SHA when the commit is known, or a
    ``none_<timestamp>`` placeholder when it is not (e.g. the golden commit could not be
    identified — see :func:`identify_golden_commit`).  The placeholder is an opaque,
    filesystem-safe token — never parsed back into a commit — matching the
    ``StateDatapoint`` instance_id convention.  The timestamp is UTC, formatted
    ``%Y%m%d-%H%M%S`` (e.g. ``none_20260723-120000``).
    """
    if commit:
        return commit[:8]
    return f"none_{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"


def rebench_base_stem(repo: str, commit: str | None) -> str:
    """Return the ``<repo-leaf>__<base-id>`` instance-id stem.

    Follows the repo-wide ``<repo>__<base_commit>__<suffix>`` convention so
    ``instance_id.split("__")`` recovers the repo and base-id just as it does for every
    other data source.  The golden_base state and its augmentations anchor on the *golden
    commit* (the merged PR's resolving commit — see :func:`identify_golden_commit`); the
    standalone rebench_base state anchors on the rebench *base commit*.  When the commit
    is unknown, ``<base-id>`` is a ``none_<timestamp>`` placeholder (see
    :func:`rebench_base_id`).
    """
    return f"{repo.split('/')[-1]}__{rebench_base_id(commit)}"


def rebench_base_container(instance_id: str) -> str:
    """Default local image tag for a base state, keyed by its ``<repo>__<base-id>`` stem."""
    base_stem = "__".join(instance_id.split("__")[:-1])
    return local_tag(base_stem)


def rebench_datapoint_row(
    datapoint: Mapping[str, Any],
    *,
    instance_id: str,
    base_commit: str | None,
    golden_commit: str | None,
    integration_failures: list[str] | None = None,
    dataset: str = DEFAULT_REBENCH_DATASET,
    container: str | None = None,
    command: str | None = None,
    test_scope: list[str] | None = None,
) -> dict[str, Any]:
    """Return a ``StateDatapoint`` dict from a rebench datapoint.

    ``instance_id`` is the full environment id including suffix, e.g.
    ``sepal_ui__deb5b9ab__base``.  ``base_commit`` is the row's base state commit, passed
    explicitly: the golden_commit for the golden_base state (so it and its augmentations
    share ``(repo, golden_commit)``), the rebench *base commit* for the standalone
    rebench_base state, or ``None`` when the golden commit could not be identified.

    All rebench-sourced provenance is nested under ``metadata['rebench_integration']``:

    - ``dataset``: source SWE-rebench dataset the instance was read from.
    - ``rebench_instance_id``: the bare rebench instance id (``owner__repo-<N>``).
    - ``commit``: the instance_id's trailing suffix segment (``base`` for a base state).
    - ``golden_commit``: the verified resolving commit, or ``None`` when unidentified.
    - ``rebench_base_commit``: the rebench datapoint's original (pre-fix) base commit.
    - ``integration_failures``: the edge-case failure types that forced a fallback
      integration for this instance (empty when the instance integrated cleanly). See
      :func:`sourceworldbench_benchmarks.swerebench.rebench_processing.process_rebench_instance`.

    ``metadata[BASE_ID_METADATA_KEY]`` records the instance's ``<base-id>`` segment so the
    base state stays identifiable (and its registry key stays distinct) when ``base_commit``
    is ``None`` — see :func:`sourceworldbench_benchmarks.schema.base_key`.
    """
    row = StateDatapoint(
        instance_id=instance_id,
        repo=str(datapoint["repo"]),
        base_commit=base_commit,
        patch=None,
        container=container or rebench_base_container(instance_id),
        command=command or render_test_command(str(datapoint["install_config"]["test_cmd"])),
        test_scope=list(DEFAULT_REBENCH_TEST_SCOPE if test_scope is None else test_scope),
        metadata={
            BASE_ID_METADATA_KEY: instance_id.split("__")[1],
            "rebench_integration": {
                "dataset": dataset,
                "rebench_instance_id": str(datapoint["instance_id"]),
                "commit": instance_id.split("__")[-1],
                "golden_commit": golden_commit,
                "rebench_base_commit": str(datapoint["base_commit"]),
                "integration_failures": list(integration_failures or []),
            }
        },
    )
    return row.model_dump()


def load_rebench_datapoint(
    rebench_id: str,
    *,
    dataset: str = DEFAULT_REBENCH_DATASET,
    split: str = DEFAULT_REBENCH_SPLIT,
) -> dict[str, Any]:
    """Load a single rebench datapoint by its bare rebench ``instance_id``.

    ``rebench_id`` is the bare rebench id, e.g. ``12rambau__sepal_ui-411``.
    """
    ds = load_dataset(dataset, split=split)
    for row in ds:
        if row["instance_id"] == rebench_id:
            return dict(row)
    raise KeyError(f"instance_id {rebench_id!r} not found in {dataset} [{split}]")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=(
            "Generate a single StateDatapoint from a SWE-rebench instance and push it to\n"
            "the sourceworldbench-benchmarks single-state dataset.\n"
            "\n"
            "What it does:\n"
            "  1. Loads the rebench instance REBENCH_ID from the source dataset.\n"
            "  2. Verifies its golden commit against GitHub (identify_golden_commit) and\n"
            "     uses that commit as the row's base_commit.\n"
            "  3. Builds a StateDatapoint row whose instance_id is\n"
            "     `<repo>__<golden_commit[:8]>__<suffix>` (SUFFIX defaults to `base` and is\n"
            "     recorded in metadata['commit']).\n"
            "  4. Optionally writes the row to a JSON file (--out) and/or pushes it to the\n"
            "     target dataset (skipped with --no-push).\n"
            "\n"
            "The row is partial: it carries container/command/test_scope but no test\n"
            "outcomes. Run `sourceworldbench-benchmarks collect` (or the rebench_processing pipeline) to\n"
            "fill in the outcomes."
        ),
        epilog=(
            "examples:\n"
            "  # Generate and push the golden_base row for one instance\n"
            "  uv run src/sourceworldbench_benchmarks/swerebench/to_datapoint.py 12rambau__sepal_ui-411\n"
            "\n"
            "  # Preview the JSON without touching any dataset\n"
            "  uv run src/sourceworldbench_benchmarks/swerebench/to_datapoint.py 12rambau__sepal_ui-411 --no-push\n"
            "\n"
            "  # Write to a file and overwrite an existing row on push\n"
            "  uv run src/sourceworldbench_benchmarks/swerebench/to_datapoint.py 12rambau__sepal_ui-411 \\\n"
            "      --out row.json --overwrite\n"
            "\n"
            "  # Pin the test command and scope instead of the auto-generated defaults\n"
            "  uv run src/sourceworldbench_benchmarks/swerebench/to_datapoint.py 12rambau__sepal_ui-411 \\\n"
            "      --command '/app/.venv/bin/pytest tests/ --junitxml=/results/junit.xml' \\\n"
            "      --test-scope tests/ --test-scope integration/"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "rebench_id",
        help="Bare rebench instance_id to convert, e.g. 12rambau__sepal_ui-411.",
    )
    parser.add_argument(
        "--suffix",
        default="base",
        help=(
            "Trailing instance-id segment appended after the golden commit, recorded in "
            "metadata['commit'] (default: %(default)s). Use `base` for the golden_base state."
        ),
    )
    parser.add_argument(
        "--container",
        default=None,
        help=(
            "Full image reference for the row's `container` field. Defaults to the local "
            "tag of the `<repo>__<base-id>` stem (rebench_base_container(instance_id))."
        ),
    )
    parser.add_argument(
        "--command",
        default=None,
        help=(
            "Shell command run inside the container to produce test output. Defaults to the "
            "conda-wrapped form of the instance's install_config.test_cmd; override this for "
            "images that are not the SWE-rebench eval image."
        ),
    )
    parser.add_argument(
        "--test-scope",
        action="append",
        metavar="PATH",
        dest="test_scope",
        help=(
            "Test path recorded in `test_scope`; repeat the flag for multiple paths "
            "(default: %r). Should reflect the full test footprint, not just what --command "
            "runs." % DEFAULT_REBENCH_TEST_SCOPE
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write the generated JSON row to this file (in addition to any push).",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing --out file or an existing row in the target dataset instead of erroring.",
    )
    parser.add_argument(
        "--rebench-dataset",
        default=DEFAULT_REBENCH_DATASET,
        dest="rebench_dataset",
        help="Source SWE-rebench dataset to read REBENCH_ID from (default: %(default)s).",
    )
    parser.add_argument(
        "--rebench-split",
        default=DEFAULT_REBENCH_SPLIT,
        dest="rebench_split",
        help="Split of the source rebench dataset to read (default: %(default)s).",
    )
    parser.add_argument(
        "--dataset",
        default=DEFAULT_DATASET_REPO_ID,
        help="Target sourceworldbench-benchmarks dataset the generated row is pushed to (default: %(default)s).",
    )
    parser.add_argument(
        "--no-push",
        action="store_true",
        help="Do not push to the target dataset; print the generated row to stdout instead.",
    )
    args = parser.parse_args()

    print(f"Loading {args.rebench_id} from {args.rebench_dataset} [{args.rebench_split}]...")
    try:
        dp = load_rebench_datapoint(args.rebench_id, dataset=args.rebench_dataset, split=args.rebench_split)
    except KeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)

    # Edge-case fallback (mirrors process_rebench_instance): an unidentifiable golden
    # commit or an environment patch no longer aborts — the row is still emitted, with the
    # failure recorded in metadata. When the golden commit is unidentified, base_commit is
    # None and the id gets a `none_<timestamp>` base-id instead of the short SHA.
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

    instance_id = f"{rebench_base_stem(str(dp['repo']), golden_commit)}__{args.suffix}"
    print(f"Golden commit {golden_commit} → instance_id {instance_id} (integration_failures={integration_failures})")

    row = rebench_datapoint_row(
        dp,
        instance_id=instance_id,
        base_commit=golden_commit,
        golden_commit=golden_commit,
        integration_failures=integration_failures,
        dataset=args.rebench_dataset,
        container=args.container,
        command=args.command,
        test_scope=args.test_scope,
    )

    if args.out is not None:
        if args.out.exists() and not args.overwrite:
            print(f"error: {args.out} already exists; pass --overwrite to replace it", file=sys.stderr)
            sys.exit(2)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(row, indent=2), encoding="utf-8")
        print(f"Wrote JSON to {args.out}")

    if args.no_push:
        print(json.dumps(row, indent=2))
        sys.exit(0)

    validated = StateDatapoint.model_validate(row)
    push_repo = push_repo_for(args.dataset)
    existing = load_existing_rows(push_repo)
    existed = validated.instance_id in {r["instance_id"] for r in existing}
    if existed and not args.overwrite:
        print(
            f"error: {validated.instance_id!r} already exists in {push_repo}; pass --overwrite to replace it",
            file=sys.stderr,
        )
        sys.exit(2)

    action = "Replace" if existed else "Add"
    merged = upsert_rows(existing, {validated.instance_id: validated.model_dump()})
    push_rows(
        push_repo,
        merged,
        commit_message=f"{action} rebench datapoint {validated.instance_id}",
        commit_description=commit_description([validated.instance_id]),
    )
    print(f"{action.lower()}d {validated.instance_id} in {push_repo} (total rows: {len(merged)}).")
    print(f"  container: {validated.container}")
    print(f"  command:   {validated.command}")
