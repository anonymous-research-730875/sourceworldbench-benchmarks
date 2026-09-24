"""Build the time-prediction dataset from the single-state dataset and push it to the Hub.

Asks: given one state of a repository — a base commit, plus a patch for the states that have one —
and the workload command to run in it, how long does that command take in wall-clock seconds?

    in    anonymous-research-730875/sourceworldbench-single-state      `train` split
    out   anonymous-research-730875/sourceworldbench-time-prediction   `test` split

TEMPORARY: where the answer comes from
    `sourceworldbench-single-state` carries no timing, so `dataset_utils.load_profile_times` reads it off local
    profiling reports — one directory per state under `reports/workloads`, named after its
    `instance_id`. So a build only runs on a machine holding those reports, and a state without one
    is left out. Both go away once the timing lands in the source dataset.

Which states are used
    The loader drops nothing. Every state this build leaves out is dropped by one of these, applied
    in `main` in this order:

      drop_states_without_swefficiency     no `swefficiency` block in `metadata`: no instance, and
                                           so no group for `GroupSampler` to work on
      drop_states_without_profile_report   no local workload timing; most of the source, while the
                                           timing lives on disk, and expected rather than an error

Which rows are written
    Every surviving state, one row each. States are grouped by swefficiency instance and
    `GroupSampler` keeps each group whole, so the group is a seam for a future draw rather than a
    filter. No test is ever drawn — a row asks about the whole workload.

    TODO: the test-status builds cap rows per instance so that no one instance dominates. This build
    will likely want the same; meanwhile the output is as unbalanced across instances as the reports
    on disk are.

What the questions ask
      Q-seconds     the wall-clock seconds outright; `workload_time_s` is the answer
      Q-bucket      which of `NUM_BUCKETS` buckets holds the time — see below
      Q-budget-*s   does the workload fit inside `BUDGETS_S` seconds? No answer column of its own:
                    `workload_time_s` against the budget named in the prompt is the answer, a time
                    exactly on the budget counting as fitting, so nothing can drift out of step with
                    the seconds. Adding a budget adds its prompt and nothing else

    The budgets suit the workloads on hand, whose times are bimodal — a fast cluster of a few seconds
    and a slow one in the hundreds, with almost nothing between. 5s cuts the fast cluster and 300s
    the slow one; 60s sits in the empty middle and so mostly asks which cluster a workload is in.
    Worth revisiting once more repos are profiled and that gap fills in.

How the buckets are drawn
    `NUM_BUCKETS` buckets cover every duration, the first open at the bottom and the last at the top.
    A row stores the `NUM_BUCKETS - 1` dividers between them; the labels the prompt names come from
    those dividers (`bucket_labels`).

      - the answer bucket is drawn per row, uniformly over the buckets that can hold this time at
        all. Drawing a ladder instead would favour whichever bucket admits more of them;
      - dividers are `NICE_EDGES` values at a constant stride, so a row offers round seconds growing
        at one rate — a ladder a person would have written — not an arbitrary partition;
      - drawn from that ladder rather than rounded onto it, so the time lands in the middle half of
        its bucket (`BUCKET_POSITION`): never against a divider, not always dead centre. The open top
        bucket has no width, so it asks for a modest multiple of its one divider (`OPEN_TOP_MARGIN`).

    Drawn while sampling, not while writing rows: `Dataset.from_generator` may re-run the writer, and
    a draw there would give two runs of one build different buckets for the same row.

What a row holds
      instance_id, repo, base_commit,     copied straight from the source state. `command_workload`
      patch, container, command_workload  rather than `command`: it is the one being timed
      workload_time_s                     its runtime in seconds, as the profiling report measured it
      buckets                             this row's dividers, in seconds, ascending
      bucket                              the label of the one bucket holding `workload_time_s`
      Q-bucket                            rendered per row from `Q_BUCKET_TEMPLATE`, since a question
                                          offering a choice has to name that row's own choices —
                                          which is also why there is no `labels_description` column
      Q-seconds, Q-budget-*s              the same on every row
"""

import random
from collections import Counter, defaultdict
from collections.abc import Iterator

from dataset_filters import (
    drop_states_without_profile_report,
    drop_states_without_swefficiency,
    group_states_by_instance,
)
from dataset_utils import StateRef, ask_next_tag, load_profile_times, load_source_dataset, push_dataset
from datasets import Dataset, Features, Sequence, Value
from huggingface_hub import HfApi

# The source every task-specific build reads, and the dataset this one writes.
DEFAULT_DATASET_REPO_ID = "anonymous-research-730875/sourceworldbench-single-state"
DEFAULT_TIME_PREDICTION_DATASET_REPO_ID = "anonymous-research-730875/sourceworldbench-time-prediction"
SOURCE_SPLIT = "train"
SPLIT = "test"

# Columns an output row copies straight from its source state. `command_workload` rather than the
# `command` the test-status builds copy: it is the command whose runtime is asked about.
COPIED_COLUMNS = ("instance_id", "repo", "base_commit", "patch", "container", "command_workload")

# Every source column this build reads, and nothing else: the loader projects the source onto these.
# `metadata` is here because `load_source_dataset` reads the swefficiency block off it; the answer
# comes from local reports rather than from the source, so nothing here carries it.
SOURCE_COLUMNS = (*COPIED_COLUMNS, "metadata")

# How many buckets `Q-bucket` offers, two at the very least: the first is open at the bottom and the
# last open at the top, so `NUM_BUCKETS - 1` dividers separate them.
NUM_BUCKETS = 4

# Where the real time may sit inside the answer bucket, as a fraction of that bucket's width: the
# middle half, so no answer turns on a hair, and drawn from the range rather than fixed at 0.5, so
# the answer is not always the bucket the time sits dead centre of.
BUCKET_POSITION = (0.25, 0.75)

# The open top bucket has no width to sit inside, so its rule is a margin: the real time has to be
# this many times its one divider — clear of it, but not absurdly far past it.
OPEN_TOP_MARGIN = (1.4, 3.0)

# Dividers are taken from `NICE_EDGES` at one of these strides, so the buckets of a row grow at a
# single rate. A wider stride is a coarser ladder.
BUCKET_STRIDES = (1, 2, 3)

# The only values a divider is ever drawn from: seconds a person would have written. Ascending, and
# wide enough at both ends to bracket every workload time the reports hold.
NICE_EDGES = (
    0.1, 0.2, 0.5, 1, 2, 3, 5, 8, 10, 15, 20, 30, 45, 60, 90, 120, 180, 240, 300, 450,
    600, 900, 1200, 1800, 2700, 3600, 5400, 7200, 10800, 14400,
)

# The wall-clock budgets `Q-budget-*` asks about, in seconds: one question per budget, none with an
# answer column of its own. Adding a budget here adds its prompt and touches nothing else.
BUDGETS_S = (5, 60, 300)

Q_BUDGET_TEMPLATE = "Will the provided command finish within {budget:g}s of wall-clock time?"

# The prompts that are the same on every row: the raw-seconds question, and one per budget.
QUESTIONS = {
    "Q-seconds": "Predict the wall-clock time in seconds for the provided command to run test_workload.py with pytest.",
} | {f"Q-budget-{budget:g}s": Q_BUDGET_TEMPLATE.format(budget=budget) for budget in BUDGETS_S}

# `Q-bucket` cannot be one of those: the buckets it offers are drawn per row, so the prompt has to
# name that row's own labels. `{buckets}` takes them comma-separated, in output order.
Q_BUCKET_COLUMN = "Q-bucket"
Q_BUCKET_TEMPLATE = "Choose the bucket that the wall-clock time of the provided command falls into: {buckets}."

# Columns in output order: what a row is about, then the answers, then the prompts. `patch` is `None`
# for a bare base state; `workload_time_s` is always set, a state with no timing being dropped rather
# than written with a null.
FEATURES = Features(
    {
        "instance_id": Value("string"),
        "repo": Value("string"),
        "base_commit": Value("string"),
        "patch": Value("string"),
        "container": Value("string"),
        "command_workload": Value("string"),
        "workload_time_s": Value("float64"),
        "buckets": Sequence(Value("float64")),
        "bucket": Value("string"),
    }
    | {Q_BUCKET_COLUMN: Value("string")}
    | {column: Value("string") for column in QUESTIONS}
)

# The seed the bucket draw runs off — the only randomness here — so a rebuild of an unchanged source
# writes the same ladders.
SAMPLING_SEED = 0


# One output row: a state, its drawn dividers, and which of those buckets is the answer. The workload
# time is not carried along — it is read back off `times` when the row is written.
SampledRow = tuple[StateRef, tuple[float, ...], int]


# --- Row sampling ---


class GroupSampler:
    """Draws the rows one group of states contributes.

    Placeholder: it keeps the group whole, so every timed state becomes a row. It exists so that the
    group is already the unit a future draw works on. It never draws a test — a row is asked about
    the state's whole workload.
    """

    def sample(self, group: list[StateRef]) -> list[StateRef]:
        # TODO: sample here, the way the test-status builds do — at most `ROWS_PER_GROUP` of the
        # group's states, off one `random.Random(SAMPLING_SEED)`, re-sorted by `row_index` to keep
        # the output order deterministic. Needs a `ROWS_PER_GROUP` constant and this class to take
        # the seed. Meanwhile the whole group is kept, unbalanced instances included.
        return list(group)


def sample_rows(states: list[StateRef], sampler: GroupSampler) -> dict[str, list[StateRef]]:
    # What every group contributes, keyed by instance, in source order. Groups that contribute
    # nothing are kept, so the build can count them.
    return {instance_id: sampler.sample(group) for instance_id, group in group_states_by_instance(states).items()}


# --- Answer buckets ---


def bucket_labels(dividers: tuple[float, ...]) -> list[str]:
    # The `NUM_BUCKETS` labels a row's dividers describe: `<10s`, `10-20s`, `20-45s`, `>45s`. First
    # and last are open on one side, so they read as a single bound; `g` keeps a whole divider whole
    # and a fractional one short.
    bounds: list[float | None] = [None, *dividers, None]

    labels = []
    for low, high in zip(bounds, bounds[1:], strict=False):
        if low is None:
            labels.append(f"<{high:g}s")
        elif high is None:
            labels.append(f">{low:g}s")
        else:
            labels.append(f"{low:g}-{high:g}s")
    return labels


def q_bucket_prompt(dividers: tuple[float, ...]) -> str:
    # The one prompt that is not the same on every row: a question that offers a choice has to name
    # the choices, and those are drawn per row.
    return Q_BUCKET_TEMPLATE.format(buckets=", ".join(bucket_labels(dividers)))


def bucket_position(dividers: tuple[float, ...], correct: int, time_s: float) -> float:
    # Where `time_s` sits in bucket `correct` of one ladder, which is what decides whether that bucket
    # may be the answer:
    #   - a closed bucket, and the first one measured from zero, report a fraction of their width;
    #   - the last has no width, so it reports how many times its one divider `time_s` is — a
    #     different quantity, hence held to `OPEN_TOP_MARGIN` rather than `BUCKET_POSITION`.
    if correct == 0:
        return time_s / dividers[0]
    if correct == len(dividers):
        return time_s / dividers[-1]

    low, high = dividers[correct - 1], dividers[correct]
    return (time_s - low) / (high - low)


def draw_buckets(time_s: float, rng: random.Random) -> tuple[tuple[float, ...], int]:
    # The `NUM_BUCKETS - 1` dividers one row offers, and the index of the bucket among them holding
    # `time_s`. Called while sampling, not while writing a row: `Dataset.from_generator` may re-run
    # the writer, and drawing there would give two runs different buckets for the same row.
    if time_s <= 0:
        raise ValueError(f"workload time must be positive, got {time_s}")

    # Every ladder the rules allow, grouped by which of its buckets is then the answer. A ladder is
    # `NUM_BUCKETS - 1` dividers off `NICE_EDGES` at a constant stride, so it can be neither
    # non-monotonic nor a mix of a hair-thin bucket and a decade-wide one.
    ladders: dict[int, list[tuple[float, ...]]] = defaultdict(list)
    for stride in BUCKET_STRIDES:
        span = (NUM_BUCKETS - 2) * stride
        for start in range(len(NICE_EDGES) - span):
            dividers = tuple(float(NICE_EDGES[start + step * stride]) for step in range(NUM_BUCKETS - 1))
            for correct in range(NUM_BUCKETS):
                low, high = OPEN_TOP_MARGIN if correct == NUM_BUCKETS - 1 else BUCKET_POSITION
                if low <= bucket_position(dividers, correct, time_s) <= high:
                    ladders[correct].append(dividers)

    if not ladders:
        raise ValueError(f"no ladder of {len(NICE_EDGES)} nice edges can hold {time_s}s as one of {NUM_BUCKETS}")

    # The answer bucket first, uniformly over the buckets this time can occupy, and only then a
    # ladder for it: drawing a ladder directly would favour whichever bucket admits more of them.
    # Sorted before either draw, so the same seed and the same time always agree.
    correct = rng.choice(sorted(ladders))
    return rng.choice(sorted(ladders[correct])), correct


def draw_bucket_rows(states: list[StateRef], times: dict[str, float], rng: random.Random) -> list[SampledRow]:
    # One row per state, in the order given. Every draw the build makes happens here, once, so that
    # writing a row is a pure function of what this returns. `times` is indexed, not queried:
    # `drop_states_without_profile_report` ran first, so a missing entry should raise.
    return [(state, *draw_buckets(times[state.instance_id], rng)) for state in states]


# --- Output rows ---


def build_time_prediction_row(row: dict, time_s: float, dividers: tuple[float, ...], correct: int) -> dict:
    return {column: row[column] for column in COPIED_COLUMNS} | {
        "workload_time_s": time_s,
        "buckets": list(dividers),
        "bucket": bucket_labels(dividers)[correct],
        Q_BUCKET_COLUMN: q_bucket_prompt(dividers),
        **QUESTIONS,
    }


# --- Build and push ---


def iter_time_prediction_rows(source: Dataset, sampled: list[SampledRow], times: dict[str, float]) -> Iterator[dict]:
    # One row at a time, in sampled order. Side-effect free: `Dataset.from_generator` may re-run it,
    # and the ladders were drawn before this ran and are carried in `sampled`.
    for state, dividers, correct in sampled:
        row = source[state.row_index]
        yield build_time_prediction_row(row, times[state.instance_id], dividers, correct)


def build_time_prediction_dataset(
    source: Dataset, sampled: list[SampledRow], times: dict[str, float], features: Features
) -> Dataset:
    return Dataset.from_generator(
        iter_time_prediction_rows,
        features=features,
        gen_kwargs={
            # Hashed into the cache key, so a rebuilt source, a changed draw or a re-profiled
            # workload cannot be answered out of a previous run's output.
            "source": source,
            "sampled": sampled,
            "times": times,
        },
        keep_in_memory=False,
    )


def print_dataset_summary(dataset: Dataset) -> None:
    # Where the answer landed on the ladder — this build's nearest thing to a label distribution —
    # and the span of the times behind it. `index` also re-checks that every row's `bucket` really
    # is one of the labels its own `buckets` describe.
    if len(dataset) == 0:
        print("0 rows")
        return
    positions = Counter(bucket_labels(tuple(record["buckets"])).index(record["bucket"]) for record in dataset)
    seconds = sorted(record["workload_time_s"] for record in dataset)

    print(f"{len(dataset)} rows, {seconds[0]:.2f}s to {seconds[-1]:.2f}s")
    print(f"answer bucket: {dict(sorted(positions.items()))}")


def main() -> None:
    api = HfApi()
    tag = ask_next_tag(api, DEFAULT_TIME_PREDICTION_DATASET_REPO_ID)

    times = load_profile_times()
    source, states, _ = load_source_dataset(DEFAULT_DATASET_REPO_ID, SOURCE_SPLIT, SOURCE_COLUMNS)

    # Every state this build leaves out is left out here. All of it is counted rather than listed one
    # line each, unlike the test-status builds: nearly the whole source goes while the timing lives
    # on disk, and thousands of lines would bury the rest of the output.
    states, without_swefficiency = drop_states_without_swefficiency(states)
    states, untimed = drop_states_without_profile_report(states, times)
    print(
        f"keeping {len(states)} of {len(source)} rows; "
        f"{len(without_swefficiency)} carry no swefficiency metadata, "
        f"{len(untimed)} eligible state(s) have no timing"
    )

    # Grouping by swefficiency instance. No state draw happens yet, so every timed state survives.
    rows_by_group = sample_rows(states, sampler=GroupSampler())
    sampled_states = [state for group_rows in rows_by_group.values() for state in group_rows]
    print(f"sampled {len(sampled_states)} row(s) from {len(rows_by_group)} group(s)")

    # Drawing each row's buckets, from one seeded generator, before any row is written.
    sampled = draw_bucket_rows(sampled_states, times, random.Random(SAMPLING_SEED))

    dataset = build_time_prediction_dataset(source, sampled, times, FEATURES)
    print_dataset_summary(dataset)

    push_dataset(dataset, api=api, repo_id=DEFAULT_TIME_PREDICTION_DATASET_REPO_ID, split=SPLIT, tag=tag)
    suffix = f"as {tag}" if tag is not None else "without a tag"
    print(f"pushed to {DEFAULT_TIME_PREDICTION_DATASET_REPO_ID} ({SPLIT} split) {suffix}")


if __name__ == "__main__":
    main()
