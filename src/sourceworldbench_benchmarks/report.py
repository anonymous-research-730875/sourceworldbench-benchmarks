from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class TestStatus(str, Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    ERROR = "ERROR"


class TestResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    status: TestStatus


class ParsedReport(BaseModel):
    """Parsed results for one benchmark state.

    `outcomes` contains per-testcase results that pytest collected and ran.

    `discovery_errors` contains module-level collection failures, reported
    outside any individual testcase.
    E.g., in python, pytest imports each test module to list its tests; if that import fails,
    pytest emits a single error keyed by the module instead of per-test results.
    Each entry is the name of one such module that failed to collect. This is
    tracked separately from per-test results because such a module contributes no
    testcases to `outcomes`: its tests vanish from the report entirely.
    """

    model_config = ConfigDict(frozen=True)

    outcomes: list[TestResult] = Field(default_factory=list)
    discovery_errors: list[str] = Field(default_factory=list)


class ReportError(Exception):
    pass


class MissingReportError(ReportError):
    pass


class MalformedReportError(ReportError):
    pass


class EmptyReportError(ReportError):
    pass


class ConflictingReportError(ReportError):
    pass
