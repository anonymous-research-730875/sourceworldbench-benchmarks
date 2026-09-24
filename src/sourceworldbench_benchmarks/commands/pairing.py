"""Build the pairwise HF dataset from filled single-state HF rows."""

import itertools
import json
import logging
from collections import defaultdict
from collections.abc import Iterable, Iterator
from types import UnionType
from typing import Any, get_args, get_origin

import typer
from datasets import Dataset, Features, Sequence, Value, load_dataset
from huggingface_hub.errors import RepositoryNotFoundError
from pydantic import BaseModel, ValidationError

from sourceworldbench_benchmarks.dataset_io import (
    DEFAULT_DATASET_REPO_ID,
    DEFAULT_PAIRWISE_DATASET_REPO_ID,
    commit_description,
    load_existing_rows,
)
from sourceworldbench_benchmarks.schema import STATE_OUTCOME_FIELDS, BaseKey, StateDatapoint, base_key
from sourceworldbench_benchmarks.schema_pair import PairDatapoint, is_base_state, make_pair_id

logger = logging.getLogger(__name__)


_STATUS_FIELDS: tuple[str, ...] = ("passed_tests", "failed_tests", "skipped_tests", "errored_tests")
_SHARED_STATE_FIELDS: tuple[str, ...] = ("repo", "base_commit", "container", "command")


class PairSummary(BaseModel):
    ok: int = 0
    skip: int = 0
    fail: int = 0


def _is_state_filled(state: StateDatapoint) -> bool:
    return all(getattr(state, field) is not None for field in STATE_OUTCOME_FIELDS)


# Pairwise metadata is modeled as `dict[str, Any]` (like `StateDatapoint.metadata`) but stored on
# the Hub as a JSON string, since an empty struct cannot be written to Parquet. Encode/decode happen
# only at the Hub boundary, mirroring `dataset_io._row_for_hub` / `_row_from_hub`.
_PAIR_METADATA_FIELDS: tuple[str, ...] = ("metadata_A", "metadata_B", "pair_metadata")


def _pair_row_for_hub(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for field in _PAIR_METADATA_FIELDS:
        value = out.get(field)
        if isinstance(value, dict):
            out[field] = json.dumps(value, sort_keys=True, default=str)
        elif value is None:
            out[field] = "{}"
    return out


def _pair_row_from_hub(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for field in _PAIR_METADATA_FIELDS:
        value = out.get(field)
        if value is None or value == "":
            out[field] = {}
        elif isinstance(value, str):
            out[field] = json.loads(value)
    return out


def _remove_none(annotation: Any) -> Any:
    if get_origin(annotation) is not UnionType:
        return annotation
    non_none_args = tuple(arg for arg in get_args(annotation) if arg is not type(None))
    if len(non_none_args) == 1:
        return non_none_args[0]
    return annotation


def _pair_feature_from_annotation(annotation: Any) -> Any:
    annotation = _remove_none(annotation)
    if annotation is str:
        return Value("string")
    if annotation is bool:
        return Value("bool")
    if annotation is int:
        return Value("int64")
    if annotation is float:
        return Value("float64")
    if get_origin(annotation) is list and get_args(annotation) == (str,):
        return Sequence(Value("string"))
    if get_origin(annotation) is dict and get_args(annotation) == (str, Any):
        # Arbitrary dicts are JSON-encoded strings in the Hub parquet schema.
        return Value("string")
    raise TypeError(f"Unsupported PairDatapoint field annotation: {annotation!r}")


def _pair_features() -> Features:
    return Features(
        {name: _pair_feature_from_annotation(field.annotation) for name, field in PairDatapoint.model_fields.items()}
    )


def _load_pair_rows(repo_id: str) -> list[dict[str, Any]]:
    try:
        ds = load_dataset(repo_id, split="train")
    except (FileNotFoundError, RepositoryNotFoundError, ValueError):
        return []
    except Exception as exc:
        typer.echo(f"error: could not load pairwise dataset {repo_id}: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    return [_pair_row_from_hub(dict(row)) for row in ds]


def _push_pair_rows(
    repo_id: str,
    rows: list[dict[str, Any]],
    *,
    commit_message: str,
    commit_description: str | None = None,
) -> None:
    Dataset.from_list([_pair_row_for_hub(row) for row in rows], features=_pair_features()).push_to_hub(
        repo_id,
        commit_message=commit_message,
        commit_description=commit_description,
    )


def is_healthy(state: StateDatapoint) -> bool:
    """True when a state has no failed or errored tests or discovery errors."""
    return not (state.failed_tests or state.errored_tests or state.discovery_errors)


def _status_map(state: StateDatapoint) -> dict[str, str]:
    status_map: dict[str, str] = {}
    for field in _STATUS_FIELDS:
        for name in getattr(state, field) or []:
            status_map[name] = field
    return status_map


def flipped_tests(state_a: StateDatapoint, state_b: StateDatapoint) -> tuple[list[str], list[str]]:
    a_map = _status_map(state_a)
    b_map = _status_map(state_b)
    names = sorted(set(a_map) | set(b_map))
    flipped = [name for name in names if a_map.get(name) != b_map.get(name)]
    non_flipped = [name for name in names if a_map.get(name) == b_map.get(name)]
    return flipped, non_flipped


def make_pair(state_a: StateDatapoint, state_b: StateDatapoint, *, is_base_healthy: bool) -> PairDatapoint:
    mismatched = [field for field in _SHARED_STATE_FIELDS if getattr(state_a, field) != getattr(state_b, field)]
    if mismatched:
        raise ValueError(
            f"cannot pair {state_a.instance_id!r} with {state_b.instance_id!r}: "
            f"states disagree on {', '.join(mismatched)}"
        )

    a_passed, b_passed = sorted(state_a.passed_tests or []), sorted(state_b.passed_tests or [])
    a_skipped, b_skipped = sorted(state_a.skipped_tests or []), sorted(state_b.skipped_tests or [])

    flipped, non_flipped = flipped_tests(state_a, state_b)
    no_errored = len(state_a.errored_tests) == 0 and len(state_b.errored_tests) == 0
    no_discovery_errors = len(state_a.discovery_errors) == 0 and len(state_b.discovery_errors) == 0
    no_failed = len(state_a.failed_tests) == 0 and len(state_b.failed_tests) == 0
    skipped_match = set(a_skipped) == set(b_skipped)
    passed_match = set(a_passed) == set(b_passed)

    if is_base_healthy:
        is_clone = (passed_match and skipped_match and no_errored and no_discovery_errors and no_failed)
    else:
        is_clone = None

    num_flipped = len(flipped)
    total_observed = num_flipped + len(non_flipped)

    return PairDatapoint(
        pair_id=make_pair_id(state_a.instance_id, state_b.instance_id),
        repo=state_a.repo,
        base_commit=state_a.base_commit,
        container=state_a.container,
        command=state_a.command,
        is_base_healthy=is_base_healthy,
        instance_id_A=state_a.instance_id,
        instance_id_B=state_b.instance_id,
        patch_A=state_a.patch,
        patch_B=state_b.patch,
        passed_tests_A=a_passed,
        passed_tests_B=b_passed,
        failed_tests_A=state_a.failed_tests,
        failed_tests_B=state_b.failed_tests,
        skipped_tests_A=state_a.skipped_tests,
        skipped_tests_B=state_b.skipped_tests,
        errored_tests_A=state_a.errored_tests,
        errored_tests_B=state_b.errored_tests,
        discovery_errors_A=sorted(set(state_a.discovery_errors or [])),
        discovery_errors_B=sorted(set(state_b.discovery_errors or [])),
        metadata_A=state_a.metadata,
        metadata_B=state_b.metadata,
        is_clone=is_clone,
        num_flipped_tests=num_flipped,
        flipped_ratio=(num_flipped / total_observed) if total_observed else 0.0,
        pair_metadata={},
    )


def _grouped_by_base(states: Iterable[StateDatapoint]) -> Iterator[tuple[StateDatapoint | None, list[StateDatapoint]]]:
    groups: dict[BaseKey, list[StateDatapoint]] = defaultdict(list)
    for state in states:
        groups[base_key(state)].append(state)
    for members in groups.values():
        ordered = sorted(members, key=lambda s: (not is_base_state(s.patch), s.instance_id))
        base_state = next((member for member in members if is_base_state(member.patch)), None)
        yield base_state, ordered


def _load_filled_states(rows: list[dict[str, Any]]) -> tuple[list[StateDatapoint], int, int]:
    states: list[StateDatapoint] = []
    seen_ids: set[str] = set()
    fail = skip = 0

    for row in rows:
        try:
            state = StateDatapoint.model_validate(row)
        except ValidationError:
            fail += 1
            continue

        if state.instance_id in seen_ids:
            skip += 1
            continue
        seen_ids.add(state.instance_id)

        if not _is_state_filled(state):
            skip += 1
            continue

        states.append(state)

    return states, fail, skip


def _build_new_pairs(
    states: list[StateDatapoint],
    existing_pair_ids: set[str],
) -> tuple[list[PairDatapoint], int]:
    pairs: list[PairDatapoint] = []
    skip = 0
    seen_unordered: set[frozenset[str]] = set()

    for base_state, ordered in _grouped_by_base(states):
        if base_state is None:
            skip += 1
            continue

        base_healthy = is_healthy(base_state)
        for state_a, state_b in itertools.combinations(ordered, 2):
            key = frozenset({state_a.instance_id, state_b.instance_id})
            if key in seen_unordered:
                continue
            seen_unordered.add(key)

            pair = make_pair(state_a, state_b, is_base_healthy=base_healthy)
            if pair.pair_id in existing_pair_ids:
                skip += 1
                continue

            pairs.append(pair)
            logger.info("ok pair=%s is_clone=%s flipped=%d", pair.pair_id, pair.is_clone, pair.num_flipped_tests)

    return pairs, skip


def pair_states(
    *,
    in_repo: str,
    out_repo: str,
) -> PairSummary:
    """Pair filled single-state rows from `in_repo` and push new pairs to `out_repo`."""
    input_rows = load_existing_rows(in_repo)
    if not input_rows:
        return PairSummary()

    states, fail, skip = _load_filled_states(input_rows)
    existing_pairs = _load_pair_rows(out_repo)
    existing_pair_ids = {row["pair_id"] for row in existing_pairs}

    new_pairs, pair_skip = _build_new_pairs(states, existing_pair_ids)
    skip += pair_skip

    if not new_pairs:
        return PairSummary(ok=0, skip=skip, fail=fail)

    all_rows = existing_pairs + [pair.model_dump() for pair in new_pairs]
    pair_ids = [pair.pair_id for pair in new_pairs]
    _push_pair_rows(
        out_repo,
        all_rows,
        commit_message=f"Add {len(new_pairs)} pair(s)",
        commit_description=commit_description(pair_ids),
    )
    return PairSummary(ok=len(new_pairs), skip=skip, fail=fail)


def pair(
    in_repo: str = typer.Option(
        DEFAULT_DATASET_REPO_ID,
        "--in",
        help="Single-state HF dataset repo id.",
    ),
    out_repo: str = typer.Option(
        DEFAULT_PAIRWISE_DATASET_REPO_ID,
        "--out",
        help="Pairwise HF dataset repo id.",
    ),
) -> None:
    """Build pairwise rows from filled single-state rows and push them to the Hub."""
    typer.echo(f"Pairing filled rows from {in_repo} -> {out_repo}...")
    summary = pair_states(in_repo=in_repo, out_repo=out_repo)

    if summary.ok == 0 and summary.skip == 0 and summary.fail == 0:
        typer.echo(f"Input dataset {in_repo} is empty; nothing to pair.")
        return

    typer.echo(f"done: ok={summary.ok} skip={summary.skip} fail={summary.fail}")
    if summary.ok:
        typer.echo(f"Pushed {summary.ok} new pair(s) to {out_repo}.")
