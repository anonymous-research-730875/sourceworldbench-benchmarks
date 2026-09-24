import json
from pathlib import Path
from typing import Any

from sourceworldbench_benchmarks.report import (
    EmptyReportError,
    MalformedReportError,
    MissingReportError,
    ParsedReport,
    TestResult,
    TestStatus,
)


def parse_toy_repo_results(results_dir: Path) -> ParsedReport:
    """Parse sourceworldbench-toy-repo execution summaries written by its pytest harness."""
    report_path = results_dir / "report.json"
    if not report_path.exists():
        raise MissingReportError(f"no toy repo report.json in {results_dir}")

    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MalformedReportError(f"toy repo report is not valid JSON: {exc.msg}") from exc

    if not isinstance(payload, dict):
        raise MalformedReportError("toy repo report must be a JSON object")

    results: list[TestResult] = []
    for task_name, task_payload in sorted(payload.items()):
        if not isinstance(task_payload, dict):
            raise MalformedReportError(f"{task_name} must map to solution results")
        for solution_name, solution_payload in sorted(task_payload.items()):
            results.extend(_parse_solution(task_name, solution_name, solution_payload))

    if not results:
        raise EmptyReportError(f"toy repo report contains no tests: {report_path}")
    return ParsedReport(outcomes=results, discovery_errors=[])


def _parse_solution(task_name: str, solution_name: str, payload: Any) -> list[TestResult]:
    if not isinstance(payload, dict):
        raise MalformedReportError(f"{task_name}/{solution_name} must be an object")

    passed = _bool_list(payload, "passed", task_name, solution_name)
    failed = _bool_list(payload, "failed", task_name, solution_name)
    runtime_errors = _bool_list(payload, "runtime_errors", task_name, solution_name)
    timeouts = _bool_list(payload, "timeouts", task_name, solution_name)
    memory_limit_exceeded = _bool_list(payload, "memory_limit_exceeded", task_name, solution_name)

    lengths = {len(passed), len(failed), len(runtime_errors), len(timeouts), len(memory_limit_exceeded)}
    if len(lengths) != 1:
        raise MalformedReportError(f"{task_name}/{solution_name} status lists have different lengths")

    results: list[TestResult] = []
    for index, is_passed in enumerate(passed):
        name = f"{task_name}::{solution_name}::case_{index:03d}"
        status = _status(
            passed=is_passed,
            failed=failed[index],
            runtime_error=runtime_errors[index],
            timeout=timeouts[index],
            memory_limit_exceeded=memory_limit_exceeded[index],
        )
        results.append(TestResult(name=name, status=status))
    return results


def _bool_list(payload: dict[str, Any], key: str, task_name: str, solution_name: str) -> list[bool]:
    value = payload.get(key)
    if not isinstance(value, list) or not all(isinstance(item, bool) for item in value):
        raise MalformedReportError(f"{task_name}/{solution_name}/{key} must be a list of booleans")
    return value


def _status(
    *,
    passed: bool,
    failed: bool,
    runtime_error: bool,
    timeout: bool,
    memory_limit_exceeded: bool,
) -> TestStatus:
    if runtime_error or timeout or memory_limit_exceeded:
        return TestStatus.ERROR
    if failed:
        return TestStatus.FAILED
    if passed:
        return TestStatus.PASSED
    return TestStatus.SKIPPED
