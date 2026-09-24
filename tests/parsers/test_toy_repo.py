from pathlib import Path

import pytest

from sourceworldbench_benchmarks.parsers import parse_toy_repo_results
from sourceworldbench_benchmarks.report import MissingReportError, TestStatus


def test_parse_toy_repo_results(tmp_path: Path) -> None:
    (tmp_path / "report.json").write_text(
        """
        {
          "task_900": {
            "solution": {
              "passed": [true, false, false, false],
              "failed": [false, true, false, false],
              "runtime_errors": [false, false, true, false],
              "timeouts": [false, false, false, true],
              "memory_limit_exceeded": [false, false, false, false]
            }
          }
        }
        """
    )

    report = parse_toy_repo_results(tmp_path)

    assert report.discovery_errors == []
    assert [result.name for result in report.outcomes] == [
        "task_900::solution::case_000",
        "task_900::solution::case_001",
        "task_900::solution::case_002",
        "task_900::solution::case_003",
    ]
    assert [result.status for result in report.outcomes] == [
        TestStatus.PASSED,
        TestStatus.FAILED,
        TestStatus.ERROR,
        TestStatus.ERROR,
    ]


def test_parse_toy_repo_results_missing_report_raises(tmp_path: Path) -> None:
    with pytest.raises(MissingReportError):
        parse_toy_repo_results(tmp_path)
