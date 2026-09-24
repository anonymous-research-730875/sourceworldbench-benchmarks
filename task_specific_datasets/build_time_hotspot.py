"""Build the time-hotspot dataset from the single-state dataset and push it to the Hub.

Asks: given one state of a repository — a base commit, plus a patch for the states that have one —
and the workload command to run in it, which of the repository's own functions spend the most
wall-clock time inside themselves while that command runs?

    in    anonymous-research-730875/sourceworldbench-single-state   `train` split
    out   anonymous-research-730875/sourceworldbench-time-hotspots   `test` split

TEMPORARY: where the answer comes from
    `sourceworldbench-single-state` carries no profiling data, so `dataset_utils.load_profile_workloads` reads it
    off local profiling reports — one directory per state under `reports/workloads`, named after its
    `instance_id`. It finds the workload under one key, `dataset_utils.WORKLOAD_TEST`, the same key
    `build_time_prediction.py` reads its seconds off, so the two builds agree on what "the workload"
    is. So a build only runs on a machine holding those reports, and a state whose report did not
    profile that key is left out. Both go away once the profiling data lands in the source dataset.

    A profiler measures the machine it ran on, so the seconds are not portable and the ranking is the
    part worth asking about — hence every question here is about order, and duration is
    `build_time_prediction.py`'s question.

Which states are used
    The loader drops nothing. Every state this build leaves out is dropped by one of these, applied
    in `main` in this order:

      drop_states_without_swefficiency     no `swefficiency` block in `metadata`: no instance, and
                                           so no group for `GroupSampler` to work on
      drop_states_without_profile_report   no usable workload profile; most of the source, while the
                                           profiling data lives on disk, and expected

    Unlike the test-status builds, a state need not have reported test outcomes: a workload profile
    is not a test run, and nothing here reads a test's result.

    A report is usable when its workload finished (`WORKLOAD_OUTCOME`) and profiled at least one repo
    function spending measurable time in itself. `workload_profiles` decides that and says, one line
    each, why the rest were left out.

Which rows are written
    Every surviving state, one row each. States are grouped by swefficiency instance and
    `GroupSampler` keeps each group whole, so the group is a seam for a future draw rather than a
    filter. Nothing is drawn, so this build has no seed: a row's answer is its profile's own ranking,
    deterministic down to the tie-break (`rank_hotspots`).

    TODO: the test-status builds cap rows per instance so that no one instance dominates. This build
    will want that more than most — the reports on disk hold many states of a handful of instances —
    but meanwhile the output is as unbalanced across instances as those reports are.

What counts as a hotspot
      a repo function   `is_repo_source` keeps what the profiler recorded relative to the repo root
                        and drops interpreter, dependency and built-in frames: a model cannot be
                        asked to find those in the code it was given
      ranked by         `exclusive_time_s`: time in the function's own body rather than in what it
                        called, so a cheap wrapper does not outrank the work it delegates to
      measurable only   a function with no measurable time of its own is never ranked, so a short
                        answer reads literally: five hotspots means the workload has five. `Q-top20`
                        is then unanswerable, and `print_dataset_summary` counts such rows

What a row holds
      instance_id, repo, base_commit,     copied straight from the source state. `command_workload`
      patch, container, command_workload  rather than `command`: it is the one that was profiled
      hotspots                            the answer: up to `MAX_HOTSPOTS` functions, most expensive
                                          first. Every question is a prefix of it — `Q-top1` its
                                          first entry, `Q-top5` its first five — so no column can
                                          drift out of step with the ranking
      functions                           the whole pool `hotspots` was ranked out of, in a stable
                                          order by `key`, for a consumer to score with: partial
                                          credit, a cut at another N, the share of total time a
                                          prediction captured. NOT to be shown to a model — these
                                          questions are open, and the pool contains the answer
      Q-top1, Q-top5, Q-top20             the same on every row

    A function carries the profiler's `key` (its dotted path, e.g.
    `sklearn.decomposition.truncated_svd.TruncatedSVD`) as well as the `filename`, `firstlineno` and
    `qualname` that locate it: the dotted path does not say which file it lives in, and the file does
    not disambiguate two same-named functions. `call_count` is ranked on by nothing and asked about
    by nothing — it is there because exclusive time alone does not say whether the workload found one
    expensive body or called a cheap one a hundred thousand times.
"""

from collections.abc import Iterator
from dataclasses import dataclass

from dataset_filters import (
    drop_states_without_profile_report,
    drop_states_without_swefficiency,
    group_states_by_instance,
)
from dataset_utils import StateRef, ask_next_tag, load_profile_workloads, load_source_dataset, push_dataset
from datasets import Dataset, Features, Value
from huggingface_hub import HfApi

# The source every task-specific build reads, and the dataset this one writes.
DEFAULT_DATASET_REPO_ID = "anonymous-research-730875/sourceworldbench-single-state"
DEFAULT_TIME_HOTSPOT_DATASET_REPO_ID = "anonymous-research-730875/sourceworldbench-time-hotspots"
SOURCE_SPLIT = "train"
SPLIT = "test"

# Columns an output row copies straight from its source state. `command_workload` rather than the
# `command` the test-status builds copy: it is the command that was profiled.
COPIED_COLUMNS = ("instance_id", "repo", "base_commit", "patch", "container", "command_workload")

# Every source column this build reads, and nothing else: the loader projects the source onto these.
# `metadata` is here because `load_source_dataset` reads the swefficiency block off it; the answer
# comes from local profiling reports rather than from the source, so nothing here carries it.
SOURCE_COLUMNS = (*COPIED_COLUMNS, "metadata")

# How many hotspots each question asks for, one question per entry. The longest is how long a row's
# ranked answer is, every question being a prefix of it: an entry above 20 lengthens `hotspots` too,
# one below it costs nothing but the prompt.
TOP_N = (1, 5, 20)
MAX_HOTSPOTS = max(TOP_N)

# The only workload outcome this build ranks. A workload that failed or errored was profiled part of
# the way through, so its ranking is not the ranking of the whole workload.
WORKLOAD_OUTCOME = "passed"

# What a profiled function's `filename` must not look like to count as the repository's own source.
# The profiler records repo files relative to the repo root, so an absolute path is the interpreter
# or a dependency, and a `<...>` name is a built-in or synthetic frame.
NON_REPO_FILENAME_PREFIXES = ("/", "<")
NON_REPO_FILENAME_MARKER = "site-packages"

Q_TOP_TEMPLATE = (
    "List the top {n} function{plural} of this repository by exclusive (self) wall-clock time when the "
    "provided command runs test_workload.py with pytest, most expensive first. Identify each function "
    "by its file path, its first line number and its qualified name."
)

# The same on every row: one prompt per entry of `TOP_N`.
QUESTIONS = {f"Q-top{n}": Q_TOP_TEMPLATE.format(n=n, plural="" if n == 1 else "s") for n in TOP_N}

# One profiled function, shared by both function columns. A list of this struct rather than a
# `Sequence`, which transposes a struct feature into a struct of lists: `hotspots` has to read back
# as a list of functions in rank order, not as six parallel lists.
FUNCTION = {
    "key": Value("string"),
    "filename": Value("string"),
    "firstlineno": Value("int32"),
    "qualname": Value("string"),
    "exclusive_time_s": Value("float64"),
    "call_count": Value("int64"),
}

# Columns in output order: what a row is about, then the answer, then the pool it was ranked out of,
# then the prompts. `patch` is `None` for a bare base state. `hotspots` holds one to `MAX_HOTSPOTS`
# functions — never zero, a state with nothing to rank being dropped rather than written empty.
FEATURES = Features(
    {
        "instance_id": Value("string"),
        "repo": Value("string"),
        "base_commit": Value("string"),
        "patch": Value("string"),
        "container": Value("string"),
        "command_workload": Value("string"),
        "hotspots": [FUNCTION],
        "functions": [FUNCTION],
    }
    | {column: Value("string") for column in QUESTIONS}
)


# One profiled function, holding exactly the keys of `FUNCTION`.
ProfiledFunction = dict[str, str | int | float]


@dataclass(frozen=True, slots=True)
class WorkloadProfile:
    """What one state's profiling report says about its workload, as the two columns a row carries.

    `hotspots` is a prefix of `functions` re-ordered by time, not a second reading of the report:
    both come out of one `workload_profiles` pass, so a row's answer cannot disagree with its pool.
    """

    functions: list[ProfiledFunction]  # The whole repo-source pool, in a stable order by `key`.
    hotspots: list[ProfiledFunction]  # The costliest of them, most expensive first.


# --- Profiled functions ---


def is_repo_source(filename: str) -> bool:
    # Whether a profiled function belongs to the repository the row is about; see
    # `NON_REPO_FILENAME_PREFIXES` for what this keeps out.
    #
    # TODO: this still keeps files the agent itself left at the repository root — several reports
    # list scratch scripts like `bench_workload.py` among their `trace_paths`. None has contributed a
    # profiled function so far; meanwhile one would be ranked alongside the repo's own. Decide
    # whether to drop root-level scripts once one turns up in an answer.
    return (
        bool(filename)
        and not filename.startswith(NON_REPO_FILENAME_PREFIXES)
        and NON_REPO_FILENAME_MARKER not in filename
    )


def profiled_function(key: str, record: dict) -> ProfiledFunction:
    # One entry of a report's `functions` map, projected onto what a row carries. The profiler writes
    # more than this — inclusive time, code-object flags — and a row ranked by exclusive time needs
    # none of it. `call_count` is the exception: nothing ranks on it, but see the module docstring.
    return {
        "key": key,
        "filename": record["filename"],
        "firstlineno": record["firstlineno"],
        "qualname": record["qualname"],
        "exclusive_time_s": record["exclusive_time_s"],
        "call_count": record["call_count"],
    }


def repo_source_functions(functions: dict[str, dict]) -> list[ProfiledFunction]:
    # The pool a row carries: every profiled function of the repository's own source, by `key`.
    # Ordered by identity rather than by time on purpose: `hotspots` is the ranked view of this pool,
    # so a time-independent order keeps the two columns apart and two rows of one repo diffable.
    return sorted(
        (profiled_function(key, record) for key, record in functions.items() if is_repo_source(record["filename"])),
        key=lambda function: function["key"],
    )


def rank_hotspots(functions: list[ProfiledFunction]) -> list[ProfiledFunction]:
    # The row's answer: the `MAX_HOTSPOTS` costliest of the pool, most expensive first, ties broken by
    # `key` so that a rebuild orders two equally expensive functions the same way. A function with no
    # measurable time of its own is never ranked — which is what lets a short answer be read
    # literally, rather than as five real hotspots plus whatever happened to sort next.
    ranked = sorted(
        (function for function in functions if function["exclusive_time_s"] > 0),
        key=lambda function: (-function["exclusive_time_s"], function["key"]),
    )
    return ranked[:MAX_HOTSPOTS]


def workload_profiles(workloads: dict[str, dict]) -> tuple[dict[str, WorkloadProfile], dict[str, str]]:
    # `instance_id -> profile` for every report this build can answer off, and `instance_id -> reason`
    # for the rest. An unusable report is left out rather than raising — the runs behind these reports
    # are not all clean — and `report_unusable` names the ones that went that way.
    profiles: dict[str, WorkloadProfile] = {}
    unusable: dict[str, str] = {}

    for instance_id, record in workloads.items():
        outcome = record.get("outcome")
        if outcome != WORKLOAD_OUTCOME:
            unusable[instance_id] = f"workload outcome {outcome!r}"
            continue

        functions = repo_source_functions(record.get("functions") or {})
        hotspots = rank_hotspots(functions)
        if not hotspots:
            unusable[instance_id] = f"no repo function spent measurable time ({len(functions)} profiled)"
            continue

        profiles[instance_id] = WorkloadProfile(functions=functions, hotspots=hotspots)
    return profiles, unusable


def report_unusable(unusable: dict[str, str]) -> None:
    if not unusable:
        return
    print(f"leaving out {len(unusable)} workload report(s) there is no answer to read off:")
    for instance_id, reason in sorted(unusable.items()):
        print(f"  - {instance_id} ({reason})")


# --- Row sampling ---


class GroupSampler:
    """Draws the rows one group of states contributes.

    Placeholder: it keeps the group whole, so every profiled state becomes a row. It exists so that
    the group is already the unit a future draw works on. It never draws a test — a row is asked
    about the state's whole workload.
    """

    def sample(self, group: list[StateRef]) -> list[StateRef]:
        # TODO: sample here, the way the test-status builds do — at most `ROWS_PER_GROUP` of the
        # group's states, off one seeded `random.Random`, re-sorted by `row_index` to keep the output
        # order deterministic. Needs a `ROWS_PER_GROUP` constant, a seed (this build has none, having
        # nothing to draw) and this class to take it. Meanwhile the whole group is kept.
        return list(group)


def sample_rows(states: list[StateRef], sampler: GroupSampler) -> dict[str, list[StateRef]]:
    # What every group contributes, keyed by instance, in source order. Groups that contribute
    # nothing are kept, so the build can count them.
    return {instance_id: sampler.sample(group) for instance_id, group in group_states_by_instance(states).items()}


# --- Output rows ---


def build_time_hotspot_row(row: dict, profile: WorkloadProfile) -> dict:
    return {column: row[column] for column in COPIED_COLUMNS} | {
        "hotspots": profile.hotspots,
        "functions": profile.functions,
        **QUESTIONS,
    }


# --- Build and push ---


def iter_time_hotspot_rows(
    source: Dataset, sampled: list[StateRef], profiles: dict[str, WorkloadProfile]
) -> Iterator[dict]:
    # One row at a time, in sampled order. Side-effect free: `Dataset.from_generator` may re-run it.
    # `profiles` is indexed, not queried: `drop_states_without_profile_report` ran first, so a missing
    # entry should raise.
    for state in sampled:
        yield build_time_hotspot_row(source[state.row_index], profiles[state.instance_id])


def build_time_hotspot_dataset(
    source: Dataset, sampled: list[StateRef], profiles: dict[str, WorkloadProfile], features: Features
) -> Dataset:
    return Dataset.from_generator(
        iter_time_hotspot_rows,
        features=features,
        gen_kwargs={
            # Hashed into the cache key, so a rebuilt source, a changed set of states or a
            # re-profiled workload cannot be answered out of a previous run's output.
            "source": source,
            "sampled": sampled,
            "profiles": profiles,
        },
        keep_in_memory=False,
    )


def print_dataset_summary(dataset: Dataset) -> None:
    # How long each row's ranked answer is — which decides which of the three questions that row can
    # be asked at all — and how large a pool each was ranked out of.
    if len(dataset) == 0:
        print("0 rows")
        return

    ranked = sorted(len(record["hotspots"]) for record in dataset)
    pooled = sorted(len(record["functions"]) for record in dataset)
    answerable = {f"Q-top{n}": sum(1 for length in ranked if length >= n) for n in TOP_N}

    print(f"{len(dataset)} rows, {pooled[0]}-{pooled[-1]} profiled function(s) each, {ranked[0]}-{ranked[-1]} ranked")
    print(f"rows with enough hotspots to answer: {answerable}")


def main() -> None:
    api = HfApi()
    tag = ask_next_tag(api, DEFAULT_TIME_HOTSPOT_DATASET_REPO_ID)

    # The answers, off the local reports, before the source is touched. A report that cannot be
    # ranked is named one line each: there are few of them, and each is worth seeing.
    profiles, unusable = workload_profiles(load_profile_workloads())
    report_unusable(unusable)

    source, states, _ = load_source_dataset(DEFAULT_DATASET_REPO_ID, SOURCE_SPLIT, SOURCE_COLUMNS)

    # Every state this build leaves out is left out here. Both are counted rather than listed one
    # line each, unlike the test-status builds: nearly the whole source goes while the profiling data
    # lives on disk, and thousands of lines would bury the rest of the output.
    states, without_swefficiency = drop_states_without_swefficiency(states)
    states, unprofiled = drop_states_without_profile_report(states, profiles)
    print(
        f"keeping {len(states)} of {len(source)} rows; "
        f"{len(without_swefficiency)} carry no swefficiency metadata and "
        f"{len(unprofiled)} eligible state(s) have no usable workload profile"
    )

    # Grouping by swefficiency instance. No state draw happens yet, so every profiled state survives.
    rows_by_group = sample_rows(states, sampler=GroupSampler())
    sampled = [state for group_rows in rows_by_group.values() for state in group_rows]
    print(f"sampled {len(sampled)} row(s) from {len(rows_by_group)} group(s)")

    dataset = build_time_hotspot_dataset(source, sampled, profiles, FEATURES)
    print_dataset_summary(dataset)

    push_dataset(dataset, api=api, repo_id=DEFAULT_TIME_HOTSPOT_DATASET_REPO_ID, split=SPLIT, tag=tag)
    suffix = f"as {tag}" if tag is not None else "without a tag"
    print(f"pushed to {DEFAULT_TIME_HOTSPOT_DATASET_REPO_ID} ({SPLIT} split) {suffix}")


if __name__ == "__main__":
    main()
