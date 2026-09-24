"""Build the anchored test-status dataset from the single-state dataset and push it to the Hub.

Asks: given one state of a repository — a base commit, plus a patch for the states that have one —
and a list of test names, how does each of those tests behave in that state?

    in    anonymous-research-730875/sourceworldbench-single-state         `train` split
    out   anonymous-research-730875/sourceworldbench-test-status-anchored `test` split

"Anchored": every test a row asks about is one the state's own `base` variant passes, so a test that
already fails before any patch is never asked about. The tests come from the source states
themselves, not from a supporting dataset's list.

Which states are used
    The loader drops nothing. Every state this build leaves out is dropped by one of these, applied
    in `main` in this order:

      drop_states_without_swefficiency    no `swefficiency` block in `metadata`: no instance, and so
                                          no group to place the state in
      drop_states_without_test_outcomes   reported nothing at all: no label to give, and no passed
                                          set to anchor a group on
      drop_container_conflicts            `(repo, base_commit)` stem shared with another container,
                                          which today means the `base`/`fixed` pair
      drop_unknown_swefficiency_states    instance missing from `swefficiency/swefficiency`
      drop_groups_without_variant         group holds no `base` state, so it has no candidates

Which rows are written
    A group is every state of one swefficiency instance. Each contributes at most `ROWS_PER_GROUP`
    rows, and unlike the single-test build every row draws its own tests:

      candidates   the tests the group's `base` state passes
      the states   drawn uniformly; never `base` or `fixed`, the anchor and the reference solution
      the tests    per row, at most `TESTS_PER_ROW` of the candidates that row's own state reported
                   an outcome for, weighted towards the ones more of the group fails

    Seeded, so an unchanged source rebuilds to the same rows.

What a row holds
      instance_id, repo, base_commit,    copied straight from the source state
      patch, container, command
      tests                              this row's sampled test list
      labels                             the state's own outcome per test, same index. Always one of
                                         `PASSED`/`FAILED`/`ERROR`: a test reported as anything else
                                         is never sampled, so no row carries `SKIPPED` or `OTHER`
      Q-bin, Q-sort, Q-bin-per-test,     the same on every row
      Q-label-per-test, labels_description
"""

import json
import random
from collections import Counter
from collections.abc import Iterator

from dataset_filters import (
    drop_container_conflicts,
    drop_groups_without_variant,
    drop_states_without_swefficiency,
    drop_states_without_test_outcomes,
    drop_unknown_swefficiency_states,
    find_variant_state,
    group_states_by_instance,
    report_skipped,
)
from dataset_utils import (
    STATUS_BY_OUTCOME_FIELD,
    PassedTests,
    StateRef,
    ask_next_tag,
    load_source_dataset,
    load_swefficiency_instance_ids,
    push_dataset,
)
from datasets import Dataset, Features, Sequence, Value
from huggingface_hub import HfApi

# The source every task-specific build reads, and the dataset this one writes.
DEFAULT_DATASET_REPO_ID = "anonymous-research-730875/sourceworldbench-single-state"
DEFAULT_TEST_STATUS_DATASET_REPO_ID = "anonymous-research-730875/sourceworldbench-test-status-anchored"
SOURCE_SPLIT = "train"
SPLIT = "test"

# Columns an output row copies straight from its source state.
COPIED_COLUMNS = ("instance_id", "repo", "base_commit", "patch", "container", "command")

# The one status sampling treats as positive; every other reported status is a non-passed outcome.
# The three statuses of `STATUS_BY_OUTCOME_FIELD` are also the only labels a row can carry, so
# `skipped_tests` is never read: a skipped test just drops out of that row's candidates.
PASSED_STATUS = STATUS_BY_OUTCOME_FIELD["passed_tests"]

# Every source column this build reads, and nothing else: the loader projects the source onto these,
# so the source's giant columns are never materialized.
SOURCE_COLUMNS = (*COPIED_COLUMNS, "metadata", *STATUS_BY_OUTCOME_FIELD)

# Which swefficiency instances exist, keyed by the instance id in a source row's metadata. Only the
# ids are read: unlike the single-test build, a row's tests do not come from here.
SWEFFICIENCY_REPO_ID = "swefficiency/swefficiency"
SWEFFICIENCY_SPLIT = "test"

QUESTIONS = {
    "Q-bin": "Are all of the provided tests expected to complete successfully with 'PASSED' outcome?",
    "Q-sort": "Order the provided tests so that all tests labeled 'PASSED' appear first.",
    "Q-bin-per-test": "Assign each test one of two labels: 'PASSED' or 'NOT_PASSED'.",
    "Q-label-per-test": "Assign each test one of the following labels: 'PASSED', 'FAILED', 'ERROR'.",
}

TEST_LABELS = {
    "PASSED": "The test is expected to complete successfully.",
    "FAILED": "The test runs but an assertion or explicit failure occurs.",
    "ERROR": "The test cannot complete because of an exception during execution.",
}

# The same on every row: the four question prompts and the JSON-serialized label glossary.
CONSTANT_COLUMNS = {**QUESTIONS, "labels_description": json.dumps(TEST_LABELS)}

# The anchor the candidates are read off, and the reference solution. Being references, neither is
# ever sampled as a row.
BASE_VARIANT = "base"
FIXED_VARIANT = "fixed"
UNSAMPLED_VARIANTS = (BASE_VARIANT, FIXED_VARIANT)

# Columns in output order. `patch` is `None` for a bare base state; `tests` and `labels` are
# parallel lists, one label per test in the same order.
FEATURES = Features(
    {
        "instance_id": Value("string"),
        "repo": Value("string"),
        "base_commit": Value("string"),
        "patch": Value("string"),
        "container": Value("string"),
        "command": Value("string"),
        "tests": Sequence(Value("string")),
        "labels": Sequence(Value("string")),
    }
    | {column: Value("string") for column in CONSTANT_COLUMNS}
)

# Rows one group contributes, tests one row asks about, and the seed that makes the draw
# reproducible across rebuilds.
ROWS_PER_GROUP = 2
TESTS_PER_ROW = 100
SAMPLING_SEED = 0


# One output row: a state and the tests it is asked about. Labels are not carried along — they are
# read back off the source when the row is written.
SampledRow = tuple[StateRef, list[str]]


# --- Reported test outcomes ---


def reported_statuses(row: dict, wanted: set[str]) -> dict[str, str]:
    # What one state reports for the tests in `wanted`, and nothing else: the cost is the number of
    # tests asked about, not the length of the state's outcome lists. A test it never reported is
    # absent from the result.
    status_by_test: dict[str, str] = {}
    for field, status in STATUS_BY_OUTCOME_FIELD.items():
        for test in row[field] or []:
            if test not in wanted:
                continue

            previous = status_by_test.setdefault(test, status)
            if previous != status:
                raise ValueError(f"{row['instance_id']}: test {test!r} is both {previous!r} and {status!r}")
    return status_by_test


# --- Row sampling ---


def sampling_weights(counts: Counter[str], tests: list[str]) -> list[float]:
    # A test the whole group passes weighs less than every counted test, but more than zero, so it
    # can still be drawn.
    positive = [count for test in tests if (count := counts[test]) > 0]

    if not positive:
        return [1.0] * len(tests)

    zero_count = len(tests) - len(positive)
    epsilon = min(positive) / (zero_count + 1)

    return [counts[test] or epsilon for test in tests]


class GroupSampler:
    """Draws the `(state, tests)` rows one group of states contributes.

    Candidates are the tests the group's `base` state passes; how much of the group fails each is
    what weights the draw. Each row samples its own tests, so every test a row asks about is one that
    row's state actually reported an outcome for.
    """

    def __init__(
        self,
        source: Dataset,
        passed: PassedTests,
        seed: int,
        rows_per_group: int,
        tests_per_row: int,
    ) -> None:
        if rows_per_group < 0:
            raise ValueError("rows_per_group must be non-negative")
        if tests_per_row < 1:
            raise ValueError("tests_per_row must be positive")

        self._source = source
        self._passed = passed
        self._rng = random.Random(seed)
        self._rows_per_group = rows_per_group
        self._tests_per_row = tests_per_row

    def sample(self, group: list[StateRef]) -> list[SampledRow]:
        if self._rows_per_group == 0:
            return []

        # Cheap first: a group of nothing but `base`/`fixed` contributes no row, so neither its
        # candidates nor its counts are ever read.
        states = self._sampled_states(group)
        if not states:
            return []

        candidates = self._candidate_tests(group)
        if not candidates:
            return []

        counts = self._non_passed_counts(group, candidates)
        rows = []
        for state in self._draw_states(states):
            tests = self._draw_tests(state, candidates, counts)
            # A state that reported none of the candidates has nothing to be asked about.
            if tests:
                rows.append((state, tests))
        return rows

    def _sampled_states(self, group: list[StateRef]) -> list[StateRef]:
        # `base` and `fixed` are never sampled: `base` passes every candidate test by construction,
        # and `fixed` is the reference solution, so neither asks anything about a predicted patch.
        return [state for state in group if state.swefficiency_variant not in UNSAMPLED_VARIANTS]

    def _candidate_tests(self, group: list[StateRef]) -> set[str]:
        # The tests the group's `base` state passes. A test that already fails at base says nothing
        # about what a state's own changes did to it, so it is not a candidate.
        base_state = find_variant_state(group, BASE_VARIANT)
        if base_state is None:  # `drop_groups_without_variant` runs first, so this cannot happen.
            raise ValueError(f"{group[0].swefficiency_instance_id}: group has no {BASE_VARIANT!r} state")
        return self._passed.of(base_state)

    def _non_passed_counts(self, group: list[StateRef], candidates: set[str]) -> Counter[str]:
        # How much of the group — `base` and `fixed` included — reports each candidate as anything
        # other than passed, states that never reported it included. A test the whole group passes
        # stays out of the counter; only one state's passed set is held at a time.
        counts: Counter[str] = Counter()
        for state in group:
            counts.update(candidates - self._passed.of(state))
        return counts

    def _draw_states(self, states: list[StateRef]) -> list[StateRef]:
        # Up to `rows_per_group` of the group's sampleable states, drawn uniformly. Output order
        # deterministic with respect to source order.
        sampled = self._rng.sample(states, k=min(self._rows_per_group, len(states)))
        return sorted(sampled, key=lambda state: state.row_index)

    def _draw_tests(self, state: StateRef, candidates: set[str], counts: Counter[str]) -> list[str]:
        # Up to `tests_per_row` of the candidates this state reported an outcome for, weighted
        # towards the ones more of the group fails. Drawing only from what the state reported keeps
        # every label one of the three the glossary describes.
        reported = sorted(reported_statuses(self._source[state.row_index], candidates))
        if len(reported) <= self._tests_per_row:
            return reported

        weights = sampling_weights(counts, reported)
        ranked = sorted(
            zip(reported, weights, strict=True),
            key=lambda item: self._rng.expovariate(item[1]),
        )

        return sorted(test for test, _ in ranked[: self._tests_per_row])


def sample_rows(states: list[StateRef], sampler: GroupSampler) -> dict[str, list[SampledRow]]:
    # What every group contributes, keyed by instance, in source order. Groups that contribute
    # nothing are kept, so the build can count them.
    return {instance_id: sampler.sample(group) for instance_id, group in group_states_by_instance(states).items()}


# --- Output rows ---


def labels_for(row: dict, tests: list[str]) -> list[str]:
    # The state's own outcome per sampled test, in the sampled order. Sampling only draws tests the
    # state reported, so a missing status means this row was sampled off some other state.
    statuses = reported_statuses(row, set(tests))
    missing = [test for test in tests if test not in statuses]
    if missing:
        raise ValueError(f"{row['instance_id']}: no reported outcome for {len(missing)} test(s), e.g. {missing[0]!r}")
    return [statuses[test] for test in tests]


def build_test_status_row(row: dict, tests: list[str]) -> dict:
    return {column: row[column] for column in COPIED_COLUMNS} | {
        "tests": tests,
        "labels": labels_for(row, tests),
        **CONSTANT_COLUMNS,
    }


# --- Build and push ---


def iter_test_status_rows(source: Dataset, sampled: list[SampledRow]) -> Iterator[dict]:
    # One row at a time, in sampled order. Side-effect free: `Dataset.from_generator` may re-run it.
    for state, tests in sampled:
        yield build_test_status_row(source[state.row_index], tests)


def build_test_status_dataset(source: Dataset, sampled: list[SampledRow], features: Features) -> Dataset:
    return Dataset.from_generator(
        iter_test_status_rows,
        features=features,
        gen_kwargs={
            # Hashed into the cache key, so a rebuilt source or a changed draw cannot be answered
            # out of a previous run's output.
            "source": source,
            "sampled": sampled,
        },
        keep_in_memory=False,
    )


def print_dataset_summary(dataset: Dataset) -> None:
    label_counts: Counter[str] = Counter()
    for record in dataset:
        label_counts.update(record["labels"])
    print(f"{len(dataset)} rows, {sum(label_counts.values())} labelled tests: {dict(label_counts.most_common())}")


def main() -> None:
    api = HfApi()
    tag = ask_next_tag(api, DEFAULT_TEST_STATUS_DATASET_REPO_ID)

    source, states, passed_tests = load_source_dataset(DEFAULT_DATASET_REPO_ID, SOURCE_SPLIT, SOURCE_COLUMNS)

    # Every state this build leaves out is left out below. The first two are counted rather than
    # listed one line each: they cover the whole part of the source this build is not about, and
    # listing it would bury the rest of the output.
    states, without_swefficiency = drop_states_without_swefficiency(states)
    states, without_outcomes = drop_states_without_test_outcomes(states)
    print(
        f"keeping {len(states)} of {len(source)} rows; "
        f"{len(without_swefficiency)} carry no swefficiency metadata and "
        f"{len(without_outcomes)} reported no test outcome"
    )

    states, conflicting = drop_container_conflicts(states)
    report_skipped("whose stem spans several containers (TODO)", conflicting, lambda state: state.container)

    known_instance_ids = load_swefficiency_instance_ids(SWEFFICIENCY_REPO_ID, SWEFFICIENCY_SPLIT)
    states, unknown = drop_unknown_swefficiency_states(states, known_instance_ids)
    report_skipped(
        f"whose swefficiency instance is not in {SWEFFICIENCY_REPO_ID}",
        unknown,
        lambda state: state.swefficiency_instance_id,
    )

    states, without_base = drop_groups_without_variant(states, BASE_VARIANT)
    report_skipped(
        f"whose swefficiency instance has no {BASE_VARIANT!r} state",
        without_base,
        lambda state: state.swefficiency_variant,
    )

    # At most `ROWS_PER_GROUP` rows per swefficiency instance, each asking about at most
    # `TESTS_PER_ROW` of its group's candidates, fixed seed.
    sampler = GroupSampler(
        source,
        passed_tests,
        seed=SAMPLING_SEED,
        rows_per_group=ROWS_PER_GROUP,
        tests_per_row=TESTS_PER_ROW,
    )
    rows_by_group = sample_rows(states, sampler)
    sampled = [row for rows in rows_by_group.values() for row in rows]
    contributing = sum(1 for rows in rows_by_group.values() if rows)
    print(f"sampled {len(sampled)} row(s) from {contributing} of {len(rows_by_group)} group(s)")

    dataset = build_test_status_dataset(source, sampled, FEATURES)
    print_dataset_summary(dataset)

    push_dataset(dataset, api=api, repo_id=DEFAULT_TEST_STATUS_DATASET_REPO_ID, split=SPLIT, tag=tag)
    suffix = f"as {tag}" if tag is not None else "without a tag"
    print(f"pushed to {DEFAULT_TEST_STATUS_DATASET_REPO_ID} ({SPLIT} split) {suffix}")


if __name__ == "__main__":
    main()
