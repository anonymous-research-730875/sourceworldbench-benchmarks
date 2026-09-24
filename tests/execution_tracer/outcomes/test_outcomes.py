"""Tests for the two OutcomeProvider implementations."""

from __future__ import annotations

import pytest

from sourceworldbench_benchmarks.execution_tracer.outcomes import FilledDatapointOutcomes, SweBenchLogOutcomes
from sourceworldbench_benchmarks.schema import StateDatapoint


def _partial(**overrides) -> StateDatapoint:
    payload = {
        "instance_id": "demo__demo-1__base",
        "repo": "demo/demo",
        "base_commit": "a" * 40,
        "patch": "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@\n-1\n+2\n",
        "container": "demo:latest",
        "command": "pytest",
        "test_scope": ["tests/"],
    }
    payload.update(overrides)
    return StateDatapoint.model_validate(payload)


def test_filled_datapoint_outcomes_reads_own_state() -> None:
    dp = _partial(
        passed_tests=["t1", "t2"],
        failed_tests=["t3"],
        skipped_tests=[],
        errored_tests=[],
        discovery_errors=[],
    )
    # Outcomes must be lowercase to match what the builder expects.
    assert FilledDatapointOutcomes().outcomes_for(dp) == {
        "t1": "passed",
        "t2": "passed",
        "t3": "failed",
    }


def test_filled_datapoint_outcomes_skipped_and_errored() -> None:
    dp = _partial(
        passed_tests=["t1"],
        failed_tests=["t2"],
        skipped_tests=["t3"],
        errored_tests=["t4"],
        discovery_errors=[],
    )
    outcomes = FilledDatapointOutcomes().outcomes_for(dp)

    assert outcomes["t2"] == "failed"
    assert outcomes["t3"] == "skipped"
    assert outcomes["t4"] == "error"


def test_filled_datapoint_outcomes_raises_when_unfilled() -> None:
    dp = _partial()
    with pytest.raises(ValueError, match="not filled"):
        FilledDatapointOutcomes().outcomes_for(dp)


def test_swebench_log_outcomes_uses_repo_parser() -> None:
    captured: list[str] = []

    def _fake_parser(log_text: str) -> dict[str, str]:
        captured.append(log_text)
        return {"test_a": "PASSED", "test_b": "FAILED"}

    provider = SweBenchLogOutcomes(parser_map={"demo/demo": _fake_parser})
    dp = _partial()

    outcomes = provider.outcomes_for(dp, run_log=b"some test log lines\n")

    assert outcomes == {"test_a": "PASSED", "test_b": "FAILED"}
    assert captured == ["some test log lines\n"]


def test_swebench_log_outcomes_requires_run_log() -> None:
    provider = SweBenchLogOutcomes(parser_map={"demo/demo": lambda _: {}})
    with pytest.raises(ValueError, match="requires the container stdout"):
        provider.outcomes_for(_partial(), run_log=None)


def test_swebench_log_outcomes_rejects_unknown_repo() -> None:
    provider = SweBenchLogOutcomes(parser_map={"other/repo": lambda _: {}})
    with pytest.raises(ValueError, match="no swebench log parser"):
        provider.outcomes_for(_partial(repo="unknown/repo"), run_log=b"")
