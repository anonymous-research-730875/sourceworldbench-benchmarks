"""Shared by the task-specific dataset build scripts under `task_specific_datasets/`: how a build opens and
addresses its source dataset, and the Hub release mechanics.

`load_source_dataset` hands a build its source projected onto the columns it asked for, plus a
`StateRef` per source row and a `PassedTests` read-back — the two handles that keep the source's
large columns out of Python. Nothing is filtered out here: a ref carries what a build filters on,
including whether the state has a `swefficiency` block and whether it reported any test outcome, but
which states a build can use is decided elsewhere — by the `drop_*` filters of `dataset_filters.py`,
in the order that build's `main` applies them. This half knows the single-state dataset's columns, its
swefficiency metadata fields included, and is the only shared code that does.

Nothing here changes dataset contents. Every repo id and split name is supplied by the caller and
`push_dataset` takes a finished `Dataset`, so the release half only picks the next release tag,
works around the stale `dataset_info` block on a repo card, and pushes and tags.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

from datasets import Dataset, DownloadMode, load_dataset
from huggingface_hub import CommitInfo, DatasetCard, HfApi
from huggingface_hub.errors import EntryNotFoundError, RepositoryNotFoundError

# Release tags are `vX.Y.Z`. Anything else on the repo is ignored when picking the latest one.
TAG_PATTERN = re.compile(r"^(v?)(\d+)\.(\d+)\.(\d+)$")
INITIAL_TAG = "v0.1.0"

# Spellings the tag prompt accepts besides the answer keys themselves.
ANSWER_ALIASES = {"major": "x", "minor": "y", "patch": "z", "none": "n", "no": "n"}


# --- Source states ---

# Source outcome list -> the label every test in it gets. Also the only columns a state's reported
# outcomes are read from, so a state with all of them empty reported nothing at all.
STATUS_BY_OUTCOME_FIELD = {
    "passed_tests": "PASSED",
    "failed_tests": "FAILED",
    "errored_tests": "ERROR",
}

RepoStateKey = tuple[str, str]  # Each Repo state is identified by its repo and base commit.


@dataclass(frozen=True, slots=True)
class StateRef:
    """One source row, as the scalars filtering and grouping need plus its row index.

    A ref exists for every source row, usable by a build or not, so the two swefficiency fields are
    `None` for a state whose `metadata` carries no `swefficiency` block, and `has_test_outcomes` is
    `False` for one that reported no outcome in any of `STATUS_BY_OUTCOME_FIELD`. Both are here for a
    build to filter on.
    """

    row_index: int
    instance_id: str
    repo: str
    base_commit: str
    container: str
    swefficiency_instance_id: str | None
    swefficiency_variant: str | None
    has_test_outcomes: bool

    @property
    def stem_key(self) -> RepoStateKey:
        return self.repo, self.base_commit


class PassedTests:
    """The `passed_tests` list of one state, read back from the source a single row at a time."""

    def __init__(self, source: Dataset) -> None:
        self._column = source.select_columns(["passed_tests"])

    def of(self, state: StateRef) -> set[str]:
        return set(self._column[state.row_index]["passed_tests"] or [])


# --- Loading the source ---


def swefficiency_metadata(row: dict) -> dict | None:
    return json.loads(row["metadata"] or "{}").get("swefficiency")


def has_test_outcomes(row: dict) -> bool:
    return any(row[field] for field in STATUS_BY_OUTCOME_FIELD)


def collect_state_refs(source: Dataset) -> list[StateRef]:
    # A ref per source row, in source order — every later step keeps that order. Nothing is left out
    # here, which is why a ref carries the swefficiency fields and the outcome flag a build's own
    # filters read. The scan runs over a narrowed view, so the output-only columns are not converted
    # to Python here.
    index_source = source.select_columns(
        ["instance_id", "repo", "base_commit", "container", "metadata", *STATUS_BY_OUTCOME_FIELD]
    )

    states = []
    for row_index, row in enumerate(index_source):
        swefficiency = swefficiency_metadata(row)
        states.append(
            StateRef(
                row_index=row_index,
                instance_id=row["instance_id"],
                repo=row["repo"],
                base_commit=row["base_commit"],
                container=row["container"],
                # A `swefficiency` block is expected to name both, so a block missing either is an
                # error rather than a state to skip quietly.
                swefficiency_instance_id=None if swefficiency is None else swefficiency["instance_id"],
                swefficiency_variant=None if swefficiency is None else swefficiency["variant"],
                has_test_outcomes=has_test_outcomes(row),
            )
        )
    return states


def load_source_dataset(
    repo_id: str, split: str, columns: tuple[str, ...]
) -> tuple[Dataset, list[StateRef], PassedTests]:
    # The source and the two handles onto it: a ref per source row, and the passed-test
    # read-back. Force-redownloaded, so a build always sees the current Hub state; the dataset stays
    # disk-backed and projected onto `columns`, which keeps the parquet reader from building the
    # local cache out of source columns the build has no use for.
    columns = set(columns) | set(STATUS_BY_OUTCOME_FIELD)
    source = load_dataset(
        repo_id,
        split=split,
        download_mode=DownloadMode.FORCE_REDOWNLOAD,
        keep_in_memory=False,
        columns=list(columns),
    )
    return source, collect_state_refs(source), PassedTests(source)


def load_swefficiency_instance_ids(repo_id: str, split: str) -> set[str]:
    dataset = load_dataset(repo_id, split=split, keep_in_memory=False, columns=["instance_id"])
    return {row["instance_id"] for row in dataset}


# --- Workload profile reports (temporary) ---

# Where the profiling reports lie, and the key the workload's own test is recorded under in a report's
# `tests` map. Named once so the two readers below agree on both: a report that spells the key
# differently is a report neither of them can see.
PROFILE_ROOT = Path("reports/workloads")
WORKLOAD_TEST = "test_workload.py::test_workload"


def load_profile_times(
    root: Path = PROFILE_ROOT,
    workload_test: str = WORKLOAD_TEST,
) -> dict[str, float]:
    # TEMPORARY, and deliberately one function: `sourceworldbench-single-state` carries no timing, so a build reads
    # it off the profiling reports lying under `root` — one directory per state, named after that
    # state's `instance_id`, with a `cprofile_output.json` somewhere inside it. Returns
    # `instance_id -> profile_time_s` for the states whose report timed `workload_test`.
    #
    # Not every report carries that timing, and that is expected rather than a failure: the runs
    # behind these reports are not all clean. Such a state is simply absent from the result, so a
    # build skips its `instance_id` instead of raising, and the count printed here is what says how
    # many went that way.
    #
    # Being local and relative, `root` also means a build using this is only reproducible on a machine
    # that holds those reports — which is the whole reason this goes away as soon as the timing lands
    # in the source dataset.
    times: dict[str, float] = {}
    untimed = 0

    for path in sorted(root.rglob("cprofile_output.json")):
        test = json.loads(path.read_text()).get("tests", {}).get(workload_test)
        if test is None or test.get("profile_time_s") is None:
            untimed += 1
            continue
        times[path.relative_to(root).parts[0]] = float(test["profile_time_s"])

    if untimed:
        print(f"no {workload_test!r} timing in {untimed} of {untimed + len(times)} report(s) under {root}")
    return times


def load_profile_workloads(
    root: Path = PROFILE_ROOT,
    workload_test: str = WORKLOAD_TEST,
) -> dict[str, dict]:
    # TEMPORARY, and the sibling of `load_profile_times`: the same scan over the same reports, laid out
    # the same way, handing back the whole record `workload_test` was profiled into rather than one
    # number off it. So a build asking about the workload's functions, call counts or outcome needs no
    # reader of its own. Returns `instance_id -> record`, a record being what the profiler wrote:
    # `wall_time_s`, `profile_time_s`, `outcome`, and a `functions` map keyed by dotted function path.
    #
    # A report that never profiled `workload_test` is absent from the result rather than an error, the
    # same way an untimed one is absent above, and the count printed here is what says how many went
    # that way. Both readers go away as soon as the profiling data lands in the source dataset.
    #
    # Every report is parsed in full to reach one record, and some of them are hundreds of megabytes of
    # per-test profiles this never looks at, so a scan is slow out of proportion to what it returns.
    # Tolerable while the reports are local and few; not a reason to grow a streaming parser here.
    workloads: dict[str, dict] = {}
    unprofiled = 0

    for path in sorted(root.rglob("cprofile_output.json")):
        record = json.loads(path.read_text()).get("tests", {}).get(workload_test)
        if record is None:
            unprofiled += 1
            continue
        workloads[path.relative_to(root).parts[0]] = record

    if unprofiled:
        print(f"no {workload_test!r} record in {unprofiled} of {unprofiled + len(workloads)} report(s) under {root}")
    return workloads


# --- Release tags and push ---


def latest_tag(api: HfApi, repo_id: str) -> tuple[str, str, tuple[int, int, int]] | None:
    # The repo's highest `vX.Y.Z` tag as `(name, prefix, version)`, or `None` if it has none yet.
    try:
        refs = api.list_repo_refs(repo_id, repo_type="dataset").tags
    except RepositoryNotFoundError:
        return None

    versions = []
    for ref in refs:
        match = TAG_PATTERN.match(ref.name)
        if match:
            prefix, *parts = match.groups()
            versions.append((ref.name, prefix, tuple(int(part) for part in parts)))
    return max(versions, key=lambda version: version[2]) if versions else None


def ask_choice(prompt: str, choices: dict[str, str | None]) -> str | None:
    while True:
        answer = input(prompt).strip().lower()
        answer = ANSWER_ALIASES.get(answer, answer)
        if answer in choices:
            return choices[answer]
        print(f"expected one of {', '.join(choices)}")


def ask_next_tag(api: HfApi, repo_id: str) -> str | None:
    # The tag for the next push, or `None` to push it untagged. Asked before the build so a
    # rejected tag costs a prompt, not a full rebuild and push.
    current = latest_tag(api, repo_id)
    if current is None:
        print(f"{repo_id} has no vX.Y.Z tag yet")
        return ask_choice(f"push as {INITIAL_TAG} or without a tag? [v/n]: ", {"v": INITIAL_TAG, "n": None})

    name, prefix, (x, y, z) = current
    # Every candidate is strictly greater than the highest existing version, so none can collide.
    candidates = {"x": (x + 1, 0, 0), "y": (x, y + 1, 0), "z": (x, y, z + 1)}
    choices: dict[str, str | None] = {
        part: prefix + ".".join(str(number) for number in version) for part, version in candidates.items()
    }
    choices["n"] = None

    print(f"latest tag on {repo_id}: {name}")
    options = ", ".join(f"{part}={tag if tag is not None else 'no tag'}" for part, tag in choices.items())
    return ask_choice(f"bump which part? {options} [{'/'.join(choices)}]: ", choices)


def clear_stale_dataset_info(repo_id: str) -> None:
    # `Dataset.push_to_hub` reuses the `dataset_info` block already on the repo card and refreshes
    # only its size and split counters (`info_to_dump = repo_info` in `datasets/arrow_dataset.py`);
    # it never refreshes `features`. The one branch that rebuilds them from the data is gated on
    # `remove_other_splits`, which that module hardcodes to `False`, and the mismatch check only
    # fires when the repo has splits other than the one being pushed. So for a single-split repo,
    # adding a column silently leaves the card advertising the old, narrower schema, and
    # `load_dataset` then dies casting the wider parquet down to it. Dropping the block first
    # sends `push_to_hub` down its build-from-scratch branch, which uses the real features.
    try:
        card = DatasetCard.load(repo_id, repo_type="dataset")
    except (RepositoryNotFoundError, EntryNotFoundError):
        return  # No card yet: `push_to_hub` will write a correct one.

    if card.data.pop("dataset_info", None) is None:
        return
    card.push_to_hub(repo_id, repo_type="dataset")
    print("dropped the stale dataset_info block from the repo card")


def push_dataset(dataset: Dataset, *, api: HfApi, repo_id: str, split: str, tag: str | None) -> CommitInfo:
    # Pushes the dataset as given: no column selection, filtering, or feature changes happen here.
    clear_stale_dataset_info(repo_id)
    commit = dataset.push_to_hub(repo_id, split=split)
    if tag is not None:
        # Tag the commit just pushed rather than whatever `main` points at by the time this lands.
        api.create_tag(repo_id, tag=tag, repo_type="dataset", revision=commit.oid)
    return commit
