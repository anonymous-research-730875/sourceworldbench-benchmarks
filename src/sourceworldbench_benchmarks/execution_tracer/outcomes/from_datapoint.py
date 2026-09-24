"""Outcome provider for sourceworldbench-benchmarks-built (filled) single-state Datapoints."""

from __future__ import annotations

from sourceworldbench_benchmarks.schema import StateDatapoint


class FilledDatapointOutcomes:
    """Reads outcomes off a single-state row already filled by the collector.

    Pure dict manipulation, no I/O. A single-state row carries its own
    outcomes, so there is no `side` to select: the `{test_id: outcome}`
    map is built from this state's `passed/failed/skipped/errored_tests`.
    Raises `ValueError` if the row's outcome fields aren't populated.
    """

    def outcomes_for(
        self,
        datapoint: StateDatapoint,
        run_log: bytes | None = None,
    ) -> dict[str, str]:
        passed = datapoint.passed_tests
        failed = datapoint.failed_tests
        skipped = datapoint.skipped_tests or []
        errored = datapoint.errored_tests or []
        if passed is None or failed is None:
            raise ValueError(
                f"datapoint {datapoint.instance_id} is not filled; "
                f"run the collector first or pick a different provider."
            )
        # Use the same lowercase strings the tracer records and the builder
        # expects ("passed", "failed", "error", "skipped").
        outcomes: dict[str, str] = {t: "passed" for t in passed}
        for t in failed:
            outcomes[t] = "failed"
        for t in skipped:
            outcomes[t] = "skipped"
        for t in errored:
            outcomes[t] = "error"
        return outcomes
