from pathlib import Path

from sourceworldbench_benchmarks.parsers.junit import parse_junit_xml
from sourceworldbench_benchmarks.report import ParsedReport, TestResult


def parse_ansible_test_junit_xml(results_dir: Path) -> ParsedReport:
    """Parse JUnit XML emitted by `ansible-test units`.

    `ansible-test units` writes paths relative to `test/units/`. Returned
    names are normalized to match pytest nodeids from the repository root.
    """
    raw = parse_junit_xml(results_dir)
    return ParsedReport(
        outcomes=[TestResult(name=_prefix_ansible_test_path(r.name), status=r.status) for r in raw.outcomes],
        discovery_errors=raw.discovery_errors,
    )


def _prefix_ansible_test_path(nodeid: str) -> str:
    prefix = "test/units/"
    if nodeid.startswith(prefix):
        return nodeid
    return f"{prefix}{nodeid}"
