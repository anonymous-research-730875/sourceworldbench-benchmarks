"""Check SWE-rebench f2p/p2p match between the unfixed and golden_base outcomes."""

from dataclasses import dataclass
from typing import Any


@dataclass
class OutcomeMatch:
    f2p_match: bool
    p2p_match: bool

    @property
    def all_match(self) -> bool:
        return self.f2p_match and self.p2p_match

    def __repr__(self) -> str:
        return f"OutcomeMatch(f2p={self.f2p_match}, p2p={self.p2p_match})"


def _is_passing(test: str, row: dict[str, Any]) -> bool:
    return test in (row.get("passed_tests") or [])


def _is_failing(test: str, row: dict[str, Any]) -> bool:
    """A test is failing if it is not passing and not skipped.

    Tests absent from all sets (e.g. hidden by a discovery error) are treated
    as failing.
    """
    return not _is_passing(test, row) and test not in (row.get("skipped_tests") or [])


def check_outcomes(
    unfixed_row: dict[str, Any],
    golden_base_row: dict[str, Any],
    rebench_dp: dict[str, Any],
) -> OutcomeMatch:
    """Check f2p/p2p across the unfixed and golden_base outcomes.

    ``unfixed_row`` must be the rebench_base_with_golden_tests outcomes — the base commit
    *with* ``test_patch`` applied, so it carries the golden test suite. Passing the bare
    ``rebench_base`` datapoint (no test_patch) would be wrong: its f2p tests never ran.
    """
    f2p: list[str] = rebench_dp.get("FAIL_TO_PASS") or []
    p2p: list[str] = rebench_dp.get("PASS_TO_PASS") or []
    return OutcomeMatch(
        f2p_match=all(_is_failing(t, unfixed_row) and _is_passing(t, golden_base_row) for t in f2p),
        p2p_match=all(_is_passing(t, unfixed_row) and _is_passing(t, golden_base_row) for t in p2p),
    )
