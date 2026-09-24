"""Build the single-test-status dataset from the single-state dataset and push it to the Hub.

Asks: given one state of a repository — a base commit, plus a patch for the states that have one —
and one test name, how does that test behave in that state?

    in    anonymous-research-730875/sourceworldbench-single-state         `train` split
    out   anonymous-research-730875/sourceworldbench-single-test-status   `test` split

Which states are used
    The loader drops nothing. Every state this build leaves out is dropped by one of these, applied
    in `main` in this order:

      drop_states_without_swefficiency    no `swefficiency` block in `metadata`: no instance, and so
                                          no group to place the state in
      drop_states_without_test_outcomes   reported nothing at all: no label to give, and no passed
                                          set to read a group's candidates off
      drop_container_conflicts            `(repo, base_commit)` stem shared with another container,
                                          which today means the `base`/`fixed` pair
      drop_unknown_swefficiency_states    instance missing from `swefficiency/swefficiency`
      drop_groups_without_variant         group holds no `base` state, so it has no candidates

Which rows are written
    A group is every state of one swefficiency instance. Each contributes at most `ROWS_PER_GROUP`
    rows, all of them asking about the same test:

      candidates   the tests the group's `base` state passes
      the test     one candidate some state of the group fails, weighted towards the ones more of
                   it fails; a group that agrees on everything draws uniformly instead
      the states   never `base` or `fixed`; prefers one state that passes the test and one that
                   does not; a state that reported no outcome for the test is never drawn

    Seeded, so an unchanged source rebuilds to the same rows.

What a row holds
      instance_id, repo, base_commit,    copied straight from the source state
      patch, container, command
      test                               the sampled test name
      label                              this state's own reported outcome for it
      base_label, fixed_label            what the group's `base`/`fixed` state reports for that same
                                         test: its status, `OTHER` if it reported none, `None` if
                                         the group holds no state of that variant
      Q3, Q4, labels_description         the same on every row
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
from datasets import Dataset, Features, Value
from huggingface_hub import HfApi

# The source every task-specific build reads, and the dataset this one writes.
DEFAULT_DATASET_REPO_ID = "anonymous-research-730875/sourceworldbench-single-state"
DEFAULT_TEST_STATUS_DATASET_REPO_ID = "anonymous-research-730875/sourceworldbench-single-test-status"
SOURCE_SPLIT = "train"
SPLIT = "test"

# Columns an output row copies straight from its source state.
COPIED_COLUMNS = ("instance_id", "repo", "base_commit", "patch", "container", "command")

# The one status sampling treats as positive; every other reported status is a non-passed outcome.
PASSED_STATUS = STATUS_BY_OUTCOME_FIELD["passed_tests"]

# A reference variant's label when it ran but reported nothing for the test — kept distinct from
# `None`, which means the group has no state of that variant.
OTHER_STATUS = "OTHER"

# Every source column this build reads, and nothing else: the loader projects the source onto these,
# so the source's giant columns are never materialized.
SOURCE_COLUMNS = (*COPIED_COLUMNS, "metadata", *STATUS_BY_OUTCOME_FIELD)

# Which swefficiency instances exist, keyed by the instance id in a source row's metadata.
SWEFFICIENCY_REPO_ID = "swefficiency/swefficiency"
SWEFFICIENCY_SPLIT = "test"

QUESTIONS = {
    "Q3": "Assign the test one of two labels: 'PASSED' or 'NOT_PASSED'.",
    "Q4": "Assign the test one of the following labels: 'PASSED', 'FAILED', 'ERROR'.",
}

TEST_LABELS = {
    "PASSED": "The test is expected to complete successfully.",
    "FAILED": "The test runs but an assertion or explicit failure occurs.",
    "ERROR": "The test cannot complete because of an exception during execution.",
}

# The same on every row: the question prompts and the JSON-serialized label glossary.
CONSTANT_COLUMNS = {**QUESTIONS, "labels_description": json.dumps(TEST_LABELS)}

# The two reference variants and the column each is reported in: `base` is the unpatched state the
# candidates are read off, `fixed` the reference solution. Being references, neither is ever sampled.
BASE_VARIANT = "base"
FIXED_VARIANT = "fixed"
LABEL_COLUMN_BY_VARIANT = {BASE_VARIANT: "base_label", FIXED_VARIANT: "fixed_label"}  # base_label is always PASSED
UNSAMPLED_VARIANTS = (BASE_VARIANT, FIXED_VARIANT)

# Columns in output order. Nullable ones: `patch` is `None` for a bare base state, and a reference
# label is `None` when the group holds no such state. `label` is always set — a state that reported
# no outcome for a test is never sampled with it.
FEATURES = Features(
    {
        "instance_id": Value("string"),
        "repo": Value("string"),
        "base_commit": Value("string"),
        "patch": Value("string"),
        "container": Value("string"),
        "command": Value("string"),
        "test": Value("string"),
        "label": Value("string"),
    }
    | {column: Value("string") for column in LABEL_COLUMN_BY_VARIANT.values()}
    | {column: Value("string") for column in CONSTANT_COLUMNS}
)

# Rows one group contributes, and the seed that makes the draw reproducible across rebuilds.
ROWS_PER_GROUP = 2
SAMPLING_SEED = 0


VariantLabels = dict[str, str | None]  # Reference variant -> its label for one test, `None` if absent.
# One output row: a state, the test it is asked about, and that test's reference-variant labels.
SampledRow = tuple[StateRef, str, VariantLabels]
GroupStatuses = dict[str, dict[int, str]]  # One group's candidate tests -> source row index -> status.


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


def reported_status(row: dict, test: str) -> str | None:
    # `None` for a test the state reported no outcome for (e.g. its module hit a discovery error).
    return reported_statuses(row, {test}).get(test)


# --- Row sampling ---


class GroupSampler:
    """Draws the `(state, test, variant labels)` rows one group of states contributes.

    One test per group: its rows differ only in which state is asked about, and share the reference
    labels. The test is one the group's states disagree about whenever the group has such a test.
    """

    def __init__(self, source: Dataset, passed: PassedTests, seed: int, rows_per_group: int) -> None:
        if rows_per_group < 0:
            raise ValueError("rows_per_group must be non-negative")

        self._source = source
        self._passed = passed
        self._rng = random.Random(seed)
        self._rows_per_group = rows_per_group

    def sample(self, group: list[StateRef]) -> list[SampledRow]:
        if self._rows_per_group == 0:
            return []

        # Cheap first: a group of nothing but `base`/`fixed` contributes no row, so it is never read.
        states = self._sampled_states(group)
        if not states:
            return []

        statuses = self._group_statuses(group)
        test = self._draw_test(statuses, group_size=len(group))
        if test is None:
            return []

        labels = self._variant_labels(group, statuses[test])
        return [(state, test, labels) for state in self._draw_states(states, statuses[test])]

    def _sampled_states(self, group: list[StateRef]) -> list[StateRef]:
        # `base` and `fixed` are never sampled: `base` passes every candidate test by construction,
        # and `fixed` is the reference solution, so neither asks anything about a predicted patch.
        return [state for state in group if state.swefficiency_variant not in UNSAMPLED_VARIANTS]

    def _group_statuses(self, group: list[StateRef]) -> GroupStatuses:
        # What every state of the group — `base` and `fixed` included — reports for each candidate,
        # a candidate being a test the group's `base` state passes: a test that already fails at base
        # says nothing about what a state's own changes did to it. The only place a group's source
        # rows are read; both draws below work off the result.
        base_state = find_variant_state(group, BASE_VARIANT)
        if base_state is None:  # `drop_groups_without_variant` runs first, so this cannot happen.
            raise ValueError(f"{group[0].swefficiency_instance_id}: group has no {BASE_VARIANT!r} state")

        candidates = self._passed.of(base_state)
        statuses: GroupStatuses = {test: {} for test in sorted(candidates)}
        for state in group:
            for test, status in reported_statuses(self._source[state.row_index], candidates).items():
                statuses[test][state.row_index] = status
        return statuses

    def _variant_labels(self, group: list[StateRef], by_state: dict[int, str]) -> VariantLabels:
        # What the reference variants report for the drawn test, off the statuses already scanned:
        # the variant's own status, `OTHER_STATUS` if it reported none for this test, `None` if the
        # group holds no state of that variant.
        labels: VariantLabels = {}
        for variant in LABEL_COLUMN_BY_VARIANT:
            state = find_variant_state(group, variant)
            labels[variant] = None if state is None else by_state.get(state.row_index, OTHER_STATUS)
        return labels

    def _draw_test(self, statuses: GroupStatuses, group_size: int) -> str | None:
        # A test the group disagrees about, weighted towards the ones more of it fails: those are
        # what tell the states apart. A test they all pass is drawn — uniformly — only for a group
        # that disagrees about nothing.
        disagreed: list[str] = []
        weights: list[float] = []
        agreed: list[str] = []

        for test, by_state in statuses.items():
            passed_count = sum(status == PASSED_STATUS for status in by_state.values())
            if passed_count < len(by_state):
                disagreed.append(test)
                weights.append(1.0 - passed_count / group_size)
            elif passed_count == group_size:
                agreed.append(test)
            # Otherwise every state that reported the test passed it but some state reported nothing,
            # which is neither a disagreement nor something the whole group agrees on: left out.

        if disagreed:
            return self._rng.choices(disagreed, weights=weights, k=1)[0]
        if agreed:
            return self._rng.choice(agreed)
        return None

    def _draw_states(self, states: list[StateRef], by_state: dict[int, str]) -> list[StateRef]:
        # Up to `rows_per_group` of the states that reported the drawn test, preferring one that
        # passes it and one that does not, so a group that disagrees says so in its rows. A state
        # with no outcome for the test has no label to give and is left out.
        reported = [state for state in states if state.row_index in by_state]
        passed = [state for state in reported if by_state[state.row_index] == PASSED_STATUS]
        non_passed = [state for state in reported if by_state[state.row_index] != PASSED_STATUS]

        sampled: list[StateRef] = []
        for preferred in (passed, non_passed):
            if preferred and len(sampled) < self._rows_per_group:
                sampled.append(self._rng.choice(preferred))

        # Whatever is left of the quota goes to the other reported states, regardless of status.
        picked = {state.row_index for state in sampled}
        remaining = [state for state in reported if state.row_index not in picked]
        self._rng.shuffle(remaining)
        sampled.extend(remaining[: self._rows_per_group - len(sampled)])

        # Output order deterministic with respect to source order.
        return sorted(sampled, key=lambda state: state.row_index)


def sample_rows(states: list[StateRef], sampler: GroupSampler) -> dict[str, list[SampledRow]]:
    # What every group contributes, keyed by instance, in source order. Groups that contribute
    # nothing are kept, so the build can count them.
    return {instance_id: sampler.sample(group) for instance_id, group in group_states_by_instance(states).items()}


# --- Output rows ---


def build_test_status_row(row: dict, test: str, variant_labels: VariantLabels) -> dict:
    return {column: row[column] for column in COPIED_COLUMNS} | {
        "test": test,
        "label": reported_status(row, test),
        **{column: variant_labels[variant] for variant, column in LABEL_COLUMN_BY_VARIANT.items()},
        **CONSTANT_COLUMNS,
    }


# --- Build and push ---


def iter_test_status_rows(source: Dataset, sampled: list[SampledRow]) -> Iterator[dict]:
    # One row at a time, in sampled order. Side-effect free: `Dataset.from_generator` may re-run it.
    for state, test, variant_labels in sampled:
        yield build_test_status_row(source[state.row_index], test, variant_labels)


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
    label_counts = Counter(record["label"] for record in dataset)
    print(f"{len(dataset)} rows: {dict(label_counts.most_common())}")


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

    # At most `ROWS_PER_GROUP` rows per swefficiency instance, fixed seed.
    sampler = GroupSampler(source, passed_tests, seed=SAMPLING_SEED, rows_per_group=ROWS_PER_GROUP)
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
