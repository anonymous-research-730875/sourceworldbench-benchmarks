"""Shared by the task-specific dataset build scripts under `task_specific_datasets/`: which source states a build can
leave out, and how it says so.

`dataset_utils.load_source_dataset` drops nothing — it hands a build one `StateRef` per source row,
carrying the scalars a filter reads. This module is the other half of that: the `drop_*` filters
themselves. Each takes the states a build still holds and hands back `(kept, skipped)` in source
order, so the build can pass the skipped half to `report_skipped` and say out loud what went.

Every filter here leaves a state out over a property of *the source* — no `swefficiency` block in its
metadata, no reported test outcome, a stem spanning two containers, an instance the supporting dataset
has never heard of, no local profiling report. None of them decides anything on a task's behalf:
**which filters a build applies, and in which order, stays in that build's `main`**, which is still the
one place to read to see everything a dataset left out.

`group_states_by_instance` and `find_variant_state` are the group view the two group-level filters work
on. They live here because those filters need them, and a build's sampler then reads its groups back
out of the same two functions instead of out of a copy of them.
"""

from collections import defaultdict
from collections.abc import Callable, Mapping

from dataset_utils import RepoStateKey, StateRef


# --- The shape of a filter ---


def partition(states: list[StateRef], keep: Callable[[StateRef], bool]) -> tuple[list[StateRef], list[StateRef]]:
    # `(kept, skipped)`, both in source order. Every filter below is one call to this, so whatever any
    # of them dropped is reported the same way.
    kept = [state for state in states if keep(state)]
    skipped = [state for state in states if not keep(state)]
    return kept, skipped


def report_skipped(reason: str, skipped: list[StateRef], detail: Callable[[StateRef], str]) -> None:
    # One line per state left out, so "why is my instance not in here?" is answered by the run's own
    # output. A filter that drops most of the source is counted rather than listed instead — that call
    # belongs to the build, next to the filter it is about.
    if not skipped:
        return
    print(f"leaving out {len(skipped)} row(s) {reason}:")
    for state in skipped:
        print(f"  - {state.instance_id} ({detail(state)})")


# --- Groups of states ---


def group_states_by_instance(states: list[StateRef]) -> dict[str, list[StateRef]]:
    # One group per swefficiency instance: all the states sampling treats as one instance. Groups
    # appear in the order their instance is first seen in the source, and hold source order within.
    groups: dict[str, list[StateRef]] = defaultdict(list)
    for state in states:
        groups[state.swefficiency_instance_id].append(state)
    return groups


def find_variant_state(group: list[StateRef], variant: str) -> StateRef | None:
    # The group's state of one variant, or `None` if the group has none — a group is not guaranteed to
    # hold every variant.
    for state in group:
        if state.swefficiency_variant == variant:
            return state
    return None


# --- Filters ---


def drop_states_without_swefficiency(states: list[StateRef]) -> tuple[list[StateRef], list[StateRef]]:
    # A state whose `metadata` carries no `swefficiency` block belongs to no swefficiency instance,
    # and the instance is the group every build in this folder works on.
    return partition(states, lambda state: state.swefficiency_instance_id is not None)


def drop_states_without_test_outcomes(states: list[StateRef]) -> tuple[list[StateRef], list[StateRef]]:
    # A state that reported no test outcome at all has no label to give and no passed set to read a
    # group's candidate tests off, so it can be neither asked about nor its group's anchor. A build
    # that asks about something other than tests still drops it: a run that reported nothing at all is
    # not a state to build a row from.
    return partition(states, lambda state: state.has_test_outcomes)


def drop_container_conflicts(states: list[StateRef]) -> tuple[list[StateRef], list[StateRef]]:
    # TODO: revisit — a stem spanning several containers is currently the swefficiency `base` vs
    # `fixed` variant pair (same repo, same base_commit, different image). Decide whether the two
    # variants are one state that shares a test subset or two distinct states keyed by container;
    # until then the whole stem is left out.
    groups: dict[RepoStateKey, list[StateRef]] = defaultdict(list)
    for state in states:
        groups[state.stem_key].append(state)

    conflicting = {
        state.instance_id
        for group in groups.values()
        if len({state.container for state in group}) > 1
        for state in group
    }
    return partition(states, lambda state: state.instance_id not in conflicting)


def drop_unknown_swefficiency_states(
    states: list[StateRef], known_instance_ids: set[str]
) -> tuple[list[StateRef], list[StateRef]]:
    # A state whose swefficiency instance is not in the supporting dataset belongs to an instance
    # this build knows nothing about.
    return partition(states, lambda state: state.swefficiency_instance_id in known_instance_ids)


def drop_groups_without_variant(states: list[StateRef], variant: str) -> tuple[list[StateRef], list[StateRef]]:
    # A group with no state of `variant` has nothing to anchor on — the test-status builds read their
    # candidate tests off the group's `base` state — so every one of its states goes. Which variant a
    # group has to hold is the build's own opinion, hence the argument rather than a constant here.
    with_variant = {
        instance_id
        for instance_id, group in group_states_by_instance(states).items()
        if find_variant_state(group, variant) is not None
    }
    return partition(states, lambda state: state.swefficiency_instance_id in with_variant)


def drop_states_without_profile_report(
    states: list[StateRef], records: Mapping[str, object]
) -> tuple[list[StateRef], list[StateRef]]:
    # A state whose `instance_id` is absent from `records` — what one of the local report scans of
    # `dataset_utils` (`load_profile_times`, `load_profile_workloads`) found, keyed by `instance_id` —
    # has nothing to answer with, so there is no row to write for it. This covers both a state with no
    # report on disk at all and one whose report the build could not use. Temporary, like those scans.
    return partition(states, lambda state: state.instance_id in records)
