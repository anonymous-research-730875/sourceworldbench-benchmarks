"""Pluggable per-row test-outcome providers.

The runner produces a trace JSON; the benchmark layer needs a
`{test_id: outcome}` map for a single state. Different input sources
fill that map differently:

  - sourceworldbench-benchmarks-built rows: outcomes already live on the
    single-state Datapoint in its `passed/failed/skipped/errored_tests`
    fields.
  - SWE-bench Verified rows: outcomes come from grading the container's
    raw stdout with swebench's per-repo log parser.

Both implementations satisfy the `OutcomeProvider` protocol below so
the benchmark layer is agnostic to the input format.
"""

from __future__ import annotations

from typing import Protocol

from sourceworldbench_benchmarks.execution_tracer.outcomes.from_datapoint import FilledDatapointOutcomes
from sourceworldbench_benchmarks.execution_tracer.outcomes.from_swebench_log import SweBenchLogOutcomes
from sourceworldbench_benchmarks.execution_tracer.outcomes.merge import merge_outcomes_into_trace
from sourceworldbench_benchmarks.schema import StateDatapoint


class OutcomeProvider(Protocol):
    """Per-state test-outcome lookup.

    `run_log` is the captured stdout/stderr of the test container run,
    available when the outcomes come from grading a log (swebench).
    Providers that don't need it (`FilledDatapointOutcomes`) ignore the
    argument.
    """

    def outcomes_for(
        self,
        datapoint: StateDatapoint,
        run_log: bytes | None = None,
    ) -> dict[str, str]:
        ...

__all__ = [
    "OutcomeProvider",
    "FilledDatapointOutcomes",
    "SweBenchLogOutcomes",
    "merge_outcomes_into_trace",
]
