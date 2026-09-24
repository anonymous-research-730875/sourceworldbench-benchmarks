"""Tests for ``swerebench.validate_outcomes``: the outcome collector that reproduces
the rebench base commit *with* the golden ``test_patch`` applied by running the eval
image directly (apply test_patch → reinstall → run tests), with no ``git checkout`` so
the image's environment patch is never discarded.
"""

from sourceworldbench_benchmarks.docker_runner import CONTAINER_PATCH_PATH, DockerError, RunResult
from sourceworldbench_benchmarks.report import MissingReportError, ParsedReport, TestResult, TestStatus
from sourceworldbench_benchmarks.swerebench import validate_outcomes as vo


def test_script_applies_test_patch_and_reinstalls_without_checkout():
    script = vo._validation_script("python -m pytest tests/")
    # Never rewind via checkout — that would discard the eval image's environment patch.
    assert "git checkout" not in script
    # Runs in the real repo dir (the eval image has no /app symlink).
    assert f"cd {vo.SWE_REBENCH_REPO_DIR}" in script
    # Ordering: apply the (base-generated) test_patch, reinstall, then run the command.
    assert script.index(CONTAINER_PATCH_PATH) < script.index("pip install -e .")
    assert script.index("pip install -e .") < script.index("python -m pytest tests/")
    # No pipefail — the script must run under a POSIX /bin/sh.
    assert "pipefail" not in script


def _report(passed: list[str], failed: list[str]) -> ParsedReport:
    outcomes = [TestResult(name=n, status=TestStatus.PASSED) for n in passed]
    outcomes += [TestResult(name=n, status=TestStatus.FAILED) for n in failed]
    return ParsedReport(outcomes=outcomes)


def test_collect_returns_parsed_outcomes(monkeypatch):
    monkeypatch.setattr(vo, "run_state", lambda **kw: RunResult(exit_code=1))
    monkeypatch.setattr(vo, "parse_junit_xml", lambda results_dir: _report(["t_pass"], ["t_fail"]))

    result = vo.collect_validation_outcomes(eval_image="img", test_patch="diff", test_cmd="pytest")
    assert result["passed_tests"] == ["t_pass"]
    assert result["failed_tests"] == ["t_fail"]


def test_collect_returns_none_on_docker_error(monkeypatch):
    def _boom(**kw):
        raise DockerError("nope")

    monkeypatch.setattr(vo, "run_state", _boom)
    result = vo.collect_validation_outcomes(eval_image="img", test_patch="diff", test_cmd="pytest")
    assert result is None


def test_collect_returns_none_on_missing_report(monkeypatch):
    monkeypatch.setattr(vo, "run_state", lambda **kw: RunResult(exit_code=0))

    def _missing(results_dir):
        raise MissingReportError("no junit.xml")

    monkeypatch.setattr(vo, "parse_junit_xml", _missing)
    result = vo.collect_validation_outcomes(eval_image="img", test_patch="diff", test_cmd="pytest")
    assert result is None
