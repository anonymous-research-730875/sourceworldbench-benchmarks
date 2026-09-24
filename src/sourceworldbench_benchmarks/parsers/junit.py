import logging
import xml.etree.ElementTree as ET
from pathlib import Path

from sourceworldbench_benchmarks.report import (
    ConflictingReportError,
    EmptyReportError,
    MalformedReportError,
    MissingReportError,
    ParsedReport,
    TestResult,
    TestStatus,
)

logger = logging.getLogger(__name__)

# pytest emits one `<testcase>` per phase, so a test can appear twice in one
# report (e.g. a `call` failure plus a `teardown` error); same-file duplicates
# merge to the most severe status.
_SEVERITY = {TestStatus.PASSED: 0, TestStatus.SKIPPED: 1, TestStatus.FAILED: 2, TestStatus.ERROR: 3}


def parse_junit_xml(results_dir: Path) -> ParsedReport:
    """Parse every `*.xml` file in `results_dir` as JUnit XML.

    Real testcases become outcomes. Module-level collection skips become
    skipped outcomes. Module-level collection errors become discovery errors.
    """
    xml_files = sorted(results_dir.glob("*.xml"))
    if not xml_files:
        raise MissingReportError(f"no JUnit XML files in {results_dir}")

    outcomes: dict[str, TestStatus] = {}
    sources: dict[str, str] = {}
    discovery_errors: set[str] = set()
    for xml_path in xml_files:
        try:
            tree = ET.parse(xml_path)
        except ET.ParseError as exc:
            raise MalformedReportError(f"malformed JUnit XML in {xml_path}: {exc}") from exc
        for testcase in tree.iter("testcase"):
            if _is_module_level_skip(testcase):
                nodeid = _module_skip_nodeid(testcase)
                status = TestStatus.SKIPPED
            elif _is_collection_error(testcase):
                discovery_errors.add(_discovery_error_name(testcase))
                continue
            else:
                try:
                    nodeid = _reconstruct_nodeid(testcase)
                except KeyError as exc:
                    raise MalformedReportError(f"testcase in {xml_path} is missing required attribute {exc}") from exc
                status = _status_of(testcase)
            if nodeid in outcomes:
                if outcomes[nodeid] is not status:
                    if sources[nodeid] != xml_path.name:
                        raise ConflictingReportError(
                            f"test '{nodeid}' has conflicting statuses across reports: "
                            f"{outcomes[nodeid].value} in {sources[nodeid]}, {status.value} in {xml_path.name}"
                        )
                    status = max(outcomes[nodeid], status, key=_SEVERITY.__getitem__)
                else:
                    logger.warning(
                        "test '%s' appears in multiple reports with the same status %s (%s, %s)",
                        nodeid,
                        status.value,
                        sources[nodeid],
                        xml_path.name,
                    )
            outcomes[nodeid] = status
            sources[nodeid] = xml_path.name

    if not outcomes and not discovery_errors:
        raise EmptyReportError(f"JUnit XML files in {results_dir} contained no testcases")

    return ParsedReport(
        outcomes=[TestResult(name=name, status=status) for name, status in sorted(outcomes.items())],
        discovery_errors=sorted(discovery_errors),
    )


def _is_module_level_skip(testcase: ET.Element) -> bool:
    """Return true for module-level collection skips.

    Pytest reports skipped modules with an empty `classname`, the module in
    `name`, and a `<skipped>` child rather than `<error>`.
    """
    return not testcase.attrib.get("classname") and testcase.find("skipped") is not None


def _is_collection_error(testcase: ET.Element) -> bool:
    """Return true for module-level collection errors.

    A real testcase always carries a `classname`. A collection error has no test segment,
    its nodeid is just the module, so the module lands in `name` and `classname` comes out empty.
    """
    return not testcase.attrib.get("classname") and testcase.find("error") is not None


def _module_skip_nodeid(testcase: ET.Element) -> str:
    """Return a pytest-style nodeid for a module-level skip."""
    module_dotted = testcase.attrib.get("name", "")
    return module_dotted.replace(".", "/") + ".py"


def _discovery_error_name(testcase: ET.Element) -> str:
    """Return the module name reported by a collection-error testcase.

    Pytest identifies the uncollectable module in the `name` attribute.
    """
    return testcase.attrib.get("name", "")


def _reconstruct_nodeid(testcase: ET.Element) -> str:
    """Rebuild a pytest-style nodeid from `<testcase>` attributes."""
    classname = testcase.attrib["classname"]
    name = testcase.attrib["name"]
    file_attr = testcase.attrib.get("file", "")

    if not file_attr:
        file_attr, class_chain = _file_and_class_chain_from_classname(classname)
    else:
        if file_attr.endswith(".py"):
            module_dotted = file_attr[:-3].replace("/", ".")
        else:
            module_dotted = file_attr.replace("/", ".")
        class_chain = _class_chain_from_classname(classname, module_dotted)

    if class_chain:
        return f"{file_attr}::{class_chain}::{name}"
    return f"{file_attr}::{name}"


def _file_and_class_chain_from_classname(classname: str) -> tuple[str, str]:
    """Split a dotted `classname` into `(file_path, class_chain)`.

    XUnit2 omits `file=`, so this uses pytest's default convention that
    capitalized segments are test classes. Pascal-case modules are ambiguous.
    """
    parts = classname.split(".")
    module_parts: list[str] = []
    class_parts: list[str] = []
    for index, part in enumerate(parts):
        if part[:1].isupper():
            class_parts = parts[index:]
            break
        module_parts.append(part)
    file_path = "/".join(module_parts) + ".py" if module_parts else ""
    class_chain = "::".join(class_parts)
    return file_path, class_chain


def _class_chain_from_classname(classname: str, module_dotted: str) -> str:
    if not classname or classname == module_dotted:
        return ""
    if classname.startswith(module_dotted + "."):
        return classname[len(module_dotted) + 1 :].replace(".", "::")
    return classname


def _status_of(testcase: ET.Element) -> TestStatus:
    if testcase.find("failure") is not None:
        return TestStatus.FAILED
    if testcase.find("error") is not None:
        return TestStatus.ERROR
    if testcase.find("skipped") is not None:
        return TestStatus.SKIPPED
    return TestStatus.PASSED
