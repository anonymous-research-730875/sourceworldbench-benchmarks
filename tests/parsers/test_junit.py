from pathlib import Path

import pytest

from sourceworldbench_benchmarks.parsers import parse_junit_xml
from sourceworldbench_benchmarks.report import (
    ConflictingReportError,
    EmptyReportError,
    MalformedReportError,
    MissingReportError,
    TestStatus,
)


def _write_junit(path: Path, xml: str) -> None:
    path.write_text(xml.strip())


def test_parse_junit_xml_module_level_function(tmp_path: Path) -> None:
    _write_junit(
        tmp_path / "report.xml",
        """
        <testsuite>
          <testcase file="tests/test_a.py" classname="tests.test_a" name="test_one"/>
        </testsuite>
        """,
    )

    report = parse_junit_xml(tmp_path)
    [result] = report.outcomes

    assert result.name == "tests/test_a.py::test_one"
    assert result.status is TestStatus.PASSED
    assert report.discovery_errors == []


def test_parse_junit_xml_class_method(tmp_path: Path) -> None:
    _write_junit(
        tmp_path / "report.xml",
        """
        <testsuite>
          <testcase file="tests/test_a.py" classname="tests.test_a.TestThing" name="test_x"/>
        </testsuite>
        """,
    )

    [result] = parse_junit_xml(tmp_path).outcomes

    assert result.name == "tests/test_a.py::TestThing::test_x"


def test_parse_junit_xml_failure_error_skip(tmp_path: Path) -> None:
    _write_junit(
        tmp_path / "report.xml",
        """
        <testsuite>
          <testcase file="t.py" classname="t" name="ok"/>
          <testcase file="t.py" classname="t" name="bad"><failure/></testcase>
          <testcase file="t.py" classname="t" name="boom"><error/></testcase>
          <testcase file="t.py" classname="t" name="meh"><skipped/></testcase>
        </testsuite>
        """,
    )

    statuses = {r.name: r.status for r in parse_junit_xml(tmp_path).outcomes}

    assert statuses["t.py::ok"] is TestStatus.PASSED
    assert statuses["t.py::bad"] is TestStatus.FAILED
    assert statuses["t.py::boom"] is TestStatus.ERROR
    assert statuses["t.py::meh"] is TestStatus.SKIPPED


def test_parse_junit_xml_preserves_parametrized_ids(tmp_path: Path) -> None:
    (tmp_path / "report.xml").write_text(
        '<testsuite><testcase file="t.py" classname="t" name="test_p[&#10;collections:&#10;- x&#10;]"/></testsuite>'
    )

    [result] = parse_junit_xml(tmp_path).outcomes

    assert result.name == "t.py::test_p[\ncollections:\n- x\n]"


def test_parse_junit_xml_collection_error_routes_to_discovery_errors(tmp_path: Path) -> None:
    """Empty `classname` testcases are collection errors, not outcomes."""
    _write_junit(
        tmp_path / "report.xml",
        """
        <testsuite>
          <testcase classname="t" name="ok"/>
          <testcase classname="" name="tests.common.libs.test_pyiceberg">
            <error message="collection failure"/>
          </testcase>
        </testsuite>
        """,
    )

    report = parse_junit_xml(tmp_path)

    assert [r.name for r in report.outcomes] == ["t.py::ok"]
    assert report.discovery_errors == ["tests.common.libs.test_pyiceberg"]


def test_parse_junit_xml_module_level_skip_is_skipped_outcome_not_discovery_error(tmp_path: Path) -> None:
    """Empty `classname` testcases with `<skipped>` are module skips, not discovery errors."""
    _write_junit(
        tmp_path / "report.xml",
        """
        <testsuite>
          <testcase classname="t" name="ok"/>
          <testcase classname="" name="tests.unit_tests.api.auth_methods.test_azure">
            <skipped message="Vault executable not found"/>
          </testcase>
        </testsuite>
        """,
    )

    report = parse_junit_xml(tmp_path)

    assert report.discovery_errors == []
    statuses = {r.name: r.status for r in report.outcomes}
    assert statuses["t.py::ok"] is TestStatus.PASSED
    assert statuses["tests/unit_tests/api/auth_methods/test_azure.py"] is TestStatus.SKIPPED


def test_parse_junit_xml_fixture_error_is_per_test_error_not_discovery_error(tmp_path: Path) -> None:
    """Fixture errors remain per-test errors."""
    _write_junit(
        tmp_path / "report.xml",
        """
        <testsuite>
          <testcase classname="test_demo" name="test_errors"><error message="setup failed"/></testcase>
        </testsuite>
        """,
    )

    report = parse_junit_xml(tmp_path)

    assert report.discovery_errors == []
    [result] = report.outcomes
    assert result.name == "test_demo.py::test_errors"
    assert result.status is TestStatus.ERROR


def test_parse_junit_xml_xunit2_default_no_file_attribute(tmp_path: Path) -> None:
    """XUnit2 reports without `file=` still produce pytest nodeids."""
    _write_junit(
        tmp_path / "report.xml",
        """
        <testsuite>
          <testcase classname="tests.sub.test_ok" name="test_y"/>
          <testcase classname="tests.sub.test_ok.TestGroup" name="test_method"/>
          <testcase classname="" name="tests.sub.test_broken"><error message="collection failure"/></testcase>
        </testsuite>
        """,
    )

    report = parse_junit_xml(tmp_path)

    assert [r.name for r in report.outcomes] == [
        "tests/sub/test_ok.py::TestGroup::test_method",
        "tests/sub/test_ok.py::test_y",
    ]
    assert report.discovery_errors == ["tests.sub.test_broken"]


def test_parse_junit_xml_discovery_errors_are_sorted_and_deduped(tmp_path: Path) -> None:
    _write_junit(
        tmp_path / "report.xml",
        """
        <testsuite>
          <testcase classname="" name="tests.zeta"><error/></testcase>
          <testcase classname="" name="tests.alpha"><error/></testcase>
          <testcase classname="" name="tests.alpha"><error/></testcase>
        </testsuite>
        """,
    )

    report = parse_junit_xml(tmp_path)

    assert report.outcomes == []
    assert report.discovery_errors == ["tests.alpha", "tests.zeta"]


def test_parse_junit_xml_no_files_raises(tmp_path: Path) -> None:
    with pytest.raises(MissingReportError):
        parse_junit_xml(tmp_path)


def test_parse_junit_xml_empty_testsuite_raises(tmp_path: Path) -> None:
    _write_junit(tmp_path / "report.xml", "<testsuite></testsuite>")

    with pytest.raises(EmptyReportError):
        parse_junit_xml(tmp_path)


def test_parse_junit_xml_malformed_xml_raises(tmp_path: Path) -> None:
    _write_junit(tmp_path / "report.xml", "<testsuite>")

    with pytest.raises(MalformedReportError):
        parse_junit_xml(tmp_path)


def test_parse_junit_xml_xunit2_nested_test_classes(tmp_path: Path) -> None:
    """Nested classes stay in the pytest class chain."""
    _write_junit(
        tmp_path / "report.xml",
        """
        <testsuite>
          <testcase classname="tests.test_nested.TestOuter.TestInner" name="test_inner"/>
        </testsuite>
        """,
    )

    [result] = parse_junit_xml(tmp_path).outcomes

    assert result.name == "tests/test_nested.py::TestOuter::TestInner::test_inner"


def test_parse_junit_xml_xunit2_deeply_nested_package(tmp_path: Path) -> None:
    _write_junit(
        tmp_path / "report.xml",
        """
        <testsuite>
          <testcase classname="tests.a.b.c.test_deep" name="test_y"/>
          <testcase classname="tests.a.b.c.test_deep.TestGroup" name="test_method"/>
        </testsuite>
        """,
    )

    names = sorted(r.name for r in parse_junit_xml(tmp_path).outcomes)

    assert names == [
        "tests/a/b/c/test_deep.py::TestGroup::test_method",
        "tests/a/b/c/test_deep.py::test_y",
    ]


def test_parse_junit_xml_xunit2_uppercase_in_module_name_stays_in_module(tmp_path: Path) -> None:
    """Uppercase letters inside a module segment stay in the module name."""
    _write_junit(
        tmp_path / "report.xml",
        """
        <testsuite>
          <testcase classname="tests.test_HTTPHelpers" name="test_x"/>
        </testsuite>
        """,
    )

    [result] = parse_junit_xml(tmp_path).outcomes

    assert result.name == "tests/test_HTTPHelpers.py::test_x"


def test_parse_junit_xml_xunit2_pascal_case_segment_is_class(tmp_path: Path) -> None:
    """Pascal-case segments are interpreted as class names."""
    _write_junit(
        tmp_path / "report.xml",
        """
        <testsuite>
          <testcase classname="tests.TestHelpers" name="test_x"/>
        </testsuite>
        """,
    )

    [result] = parse_junit_xml(tmp_path).outcomes

    assert result.name == "tests.py::TestHelpers::test_x"


def test_parse_junit_xml_xfail_collapses_to_skipped(tmp_path: Path) -> None:
    """Xfail reports are represented as skipped outcomes."""
    _write_junit(
        tmp_path / "report.xml",
        """
        <testsuite>
          <testcase classname="tests.test_x" name="test_xfails">
            <skipped type="pytest.xfail" message="known broken"/>
          </testcase>
        </testsuite>
        """,
    )

    [result] = parse_junit_xml(tmp_path).outcomes

    assert result.status is TestStatus.SKIPPED


def test_parse_junit_xml_xpass_strict_emits_failure(tmp_path: Path) -> None:
    """Strict xpass reports are represented as failed outcomes."""
    _write_junit(
        tmp_path / "report.xml",
        """
        <testsuite>
          <testcase classname="tests.test_x" name="test_strict_xpass">
            <failure message="[XPASS(strict)] strict xpass"/>
          </testcase>
        </testsuite>
        """,
    )

    [result] = parse_junit_xml(tmp_path).outcomes

    assert result.status is TestStatus.FAILED


def test_parse_junit_xml_multiple_xml_files_are_merged(tmp_path: Path) -> None:
    _write_junit(
        tmp_path / "shard1.xml",
        """
        <testsuite>
          <testcase classname="tests.shard_a" name="test_a"/>
        </testsuite>
        """,
    )
    _write_junit(
        tmp_path / "shard2.xml",
        """
        <testsuite>
          <testcase classname="tests.shard_b" name="test_b1"/>
          <testcase classname="tests.shard_b" name="test_b2"><failure/></testcase>
        </testsuite>
        """,
    )

    names = {r.name: r.status for r in parse_junit_xml(tmp_path).outcomes}

    assert names == {
        "tests/shard_a.py::test_a": TestStatus.PASSED,
        "tests/shard_b.py::test_b1": TestStatus.PASSED,
        "tests/shard_b.py::test_b2": TestStatus.FAILED,
    }


def test_parse_junit_xml_conflicting_status_across_files_raises(tmp_path: Path) -> None:
    _write_junit(
        tmp_path / "a.xml",
        '<testsuite><testcase classname="tests.t" name="x"/></testsuite>',
    )
    _write_junit(
        tmp_path / "b.xml",
        '<testsuite><testcase classname="tests.t" name="x"><failure/></testcase></testsuite>',
    )

    with pytest.raises(ConflictingReportError, match="tests/t.py::x"):
        parse_junit_xml(tmp_path)


def test_parse_junit_xml_duplicate_test_id_same_status_is_deduped(tmp_path: Path) -> None:
    _write_junit(
        tmp_path / "a.xml",
        '<testsuite><testcase classname="tests.t" name="x"><failure/></testcase></testsuite>',
    )
    _write_junit(
        tmp_path / "b.xml",
        '<testsuite><testcase classname="tests.t" name="x"><failure/></testcase></testsuite>',
    )

    [result] = parse_junit_xml(tmp_path).outcomes

    assert result.name == "tests/t.py::x"
    assert result.status is TestStatus.FAILED


def test_parse_junit_xml_testsuites_root_with_multiple_testsuites(tmp_path: Path) -> None:
    _write_junit(
        tmp_path / "report.xml",
        """
        <testsuites>
          <testsuite name="a"><testcase classname="tests.a" name="x"/></testsuite>
          <testsuite name="b"><testcase classname="tests.b" name="y"/></testsuite>
        </testsuites>
        """,
    )

    names = sorted(r.name for r in parse_junit_xml(tmp_path).outcomes)

    assert names == ["tests/a.py::x", "tests/b.py::y"]


def test_parse_junit_xml_inherited_test_appears_under_subclass(tmp_path: Path) -> None:
    """Inherited tests keep pytest's reported subclass name."""
    _write_junit(
        tmp_path / "report.xml",
        """
        <testsuite>
          <testcase classname="tests.test_nested.TestBase" name="test_inherited"/>
          <testcase classname="tests.test_nested.TestSub" name="test_inherited"/>
        </testsuite>
        """,
    )

    names = sorted(r.name for r in parse_junit_xml(tmp_path).outcomes)

    assert names == [
        "tests/test_nested.py::TestBase::test_inherited",
        "tests/test_nested.py::TestSub::test_inherited",
    ]


def test_parse_junit_xml_parametrized_id_with_double_colons_preserved(tmp_path: Path) -> None:
    """Parametrize ids with `::` are preserved verbatim."""
    _write_junit(
        tmp_path / "report.xml",
        '<testsuite><testcase classname="tests.t" name="test_p[a::b]"/></testsuite>',
    )

    [result] = parse_junit_xml(tmp_path).outcomes

    assert result.name == "tests/t.py::test_p[a::b]"


def test_parse_junit_xml_missing_testcase_name_raises(tmp_path: Path) -> None:
    _write_junit(
        tmp_path / "report.xml",
        """
        <testsuite>
          <testcase file="tests/test_a.py" classname="tests.test_a"/>
        </testsuite>
        """,
    )

    with pytest.raises(MalformedReportError):
        parse_junit_xml(tmp_path)


def test_parse_junit_xml_merges_per_phase_testcases_in_one_file(tmp_path: Path) -> None:
    """pytest emits one `<testcase>` per phase; a call failure plus a teardown error is not a conflict."""
    _write_junit(
        tmp_path / "junit.xml",
        '<testsuite>'
        '<testcase classname="tests.t" name="x"><failure/></testcase>'
        '<testcase classname="tests.t" name="x"><error/></testcase>'
        "</testsuite>",
    )

    report = parse_junit_xml(tmp_path)

    assert {r.name: r.status for r in report.outcomes} == {"tests/t.py::x": TestStatus.ERROR}
