import json
import subprocess
from pathlib import Path

import pytest

from sourceworldbench_benchmarks.execution import REBUILD_COMMAND_METADATA_KEY
from sourceworldbench_benchmarks.execution_tracer.runner.script import TraceScope
from sourceworldbench_benchmarks.schema import StateDatapoint
from sourceworldbench_benchmarks.trace_worker import HOST_SCHEMA_VERSION, cpu_info, execute_worker_trace


def _run_git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return proc.stdout


def _make_pytest_repo(tmp_path: Path) -> tuple[Path, StateDatapoint]:
    """A real git checkout with one passing pytest test under `pkg/`, plus a patch touching `pkg/`."""
    repo = tmp_path / "repo"
    pkg = repo / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (pkg / "test_calc.py").write_text(
        "from pkg.calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n", encoding="utf-8"
    )
    _run_git(repo, "init")
    _run_git(repo, "config", "user.email", "test@example.com")
    _run_git(repo, "config", "user.name", "Test User")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "base")
    base_commit = _run_git(repo, "rev-parse", "HEAD").strip()
    (pkg / "calc.py").write_text("def add(a, b):\n    return a + b  # patched\n", encoding="utf-8")
    patch = _run_git(repo, "diff")
    _run_git(repo, "reset", "--hard")
    return repo, StateDatapoint(
        instance_id="repo__row__base",
        repo="owner/repo",
        base_commit=base_commit,
        patch=patch,
        container="registry.example.com/sourceworldbench/row@sha256:abc",
        command="python -m pytest pkg/test_calc.py -q",
        test_scope=["pkg/"],
    )


def _make_repo_patched_outside_the_source_dir(tmp_path: Path) -> tuple[Path, StateDatapoint]:
    """Like `_make_pytest_repo`, but the patch touches only a root-level file.

    Reproduces what several model predictions do: edit build config and nothing
    under the package. `patch` scope then resolves to `["pyproject.toml"]`, which
    matches no source file, so the tracer records an empty profile.
    """
    repo, row = _make_pytest_repo(tmp_path)
    (repo / "pyproject.toml").write_text("[project]\nname = 'pkg'\n", encoding="utf-8")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "add build config")
    base_commit = _run_git(repo, "rev-parse", "HEAD").strip()
    (repo / "pyproject.toml").write_text("[project]\nname = 'pkg'\nversion = '2'\n", encoding="utf-8")
    patch = _run_git(repo, "diff")
    _run_git(repo, "reset", "--hard")
    return repo, row.model_copy(update={"base_commit": base_commit, "patch": patch})


@pytest.mark.parametrize(
    ("trace_scope", "expected_paths", "records_functions"),
    [("patch", ["pyproject.toml"], False), ("repo", [], True)],
)
def test_trace_scope_decides_whether_a_patch_outside_the_source_dir_records_anything(
    tmp_path: Path, trace_scope: TraceScope, expected_paths: list[str], records_functions: bool
) -> None:
    """`repo` scope profiles the package even when the patch never touches it."""
    repo, row = _make_repo_patched_outside_the_source_dir(tmp_path)
    input_path = tmp_path / "rows.jsonl"
    input_path.write_text(row.model_dump_json() + "\n", encoding="utf-8")
    artifacts = tmp_path / "artifacts"

    outcome = execute_worker_trace(
        input_path=input_path,
        instance_id=row.instance_id,
        tracer="cprofile",
        repo_dir=repo,
        artifacts_dir=artifacts,
        timeout=120,
        tempdir=tmp_path / "work",
        trace_scope=trace_scope,
    )

    assert outcome.ok
    output = json.loads((artifacts / "cprofile_output.json").read_text(encoding="utf-8"))
    assert output["trace_paths"] == expected_paths
    assert output["pass"]["trace_scope"] == trace_scope
    functions = [name for test in output["tests"].values() for name in test["functions"]]
    assert bool(functions) is records_functions
    if records_functions:
        assert any(name.startswith("pkg.calc.") for name in functions)


def _make_workload_repo(tmp_path: Path) -> tuple[Path, StateDatapoint]:
    """Like `_make_pytest_repo`, plus a workload test that prints its own timing."""
    repo, row = _make_pytest_repo(tmp_path)
    (repo / "pkg" / "test_workload.py").write_text(
        "def test_workload():\n"
        "    import statistics, timeit\n"
        "    from pkg.calc import add\n"
        "    runtimes = timeit.repeat(lambda: add(1, 2), number=100, repeat=5)\n"
        "    print('Mean:', statistics.mean(runtimes))\n"
        "    print('Std Dev:', statistics.stdev(runtimes))\n",
        encoding="utf-8",
    )
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "add workload")
    base_commit = _run_git(repo, "rev-parse", "HEAD").strip()
    return repo, row.model_copy(
        update={
            "base_commit": base_commit,
            "patch": None,
            "command_workload": "python -m pytest pkg/test_workload.py -q",
        }
    )


@pytest.mark.parametrize("use_workload", [True, False])
def test_workload_pass_keeps_the_printed_timing_and_a_suite_pass_does_not(
    tmp_path: Path, use_workload: bool
) -> None:
    """`--workload` is the only thing that opts the tracer into keeping stdout."""
    repo, row = _make_workload_repo(tmp_path)
    input_path = tmp_path / "rows.jsonl"
    input_path.write_text(row.model_dump_json() + "\n", encoding="utf-8")
    artifacts = tmp_path / "artifacts"

    outcome = execute_worker_trace(
        input_path=input_path,
        instance_id=row.instance_id,
        tracer="cprofile",
        repo_dir=repo,
        artifacts_dir=artifacts,
        timeout=120,
        tempdir=tmp_path / "work",
        workload_column="command_workload" if use_workload else None,
        trace_scope="repo",
    )

    assert outcome.ok
    tests = json.loads((artifacts / "cprofile_output.json").read_text(encoding="utf-8"))["tests"]
    record = list(tests.values())[0]

    if use_workload:
        assert record["workload_mean_s"] > 0.0
        assert record["workload_stdev_s"] >= 0.0
        assert "Mean:" in record["stdout"]
    else:
        assert "workload_mean_s" not in record
        assert "stdout" not in record


@pytest.mark.parametrize(
    ("tracer", "output_file"),
    [("trace", "trace_output.json"), ("walltime", "walltime_output.json")],
)
def test_execute_worker_trace_runs_one_tracer(tmp_path: Path, tracer: str, output_file: str) -> None:
    """A single tracer pass writes its output, command logs, and an ok marker."""
    repo, row = _make_pytest_repo(tmp_path)
    input_path = tmp_path / "rows.jsonl"
    input_path.write_text(row.model_dump_json() + "\n", encoding="utf-8")
    artifacts = tmp_path / "artifacts"

    outcome = execute_worker_trace(
        input_path=input_path,
        instance_id=row.instance_id,
        tracer=tracer,
        repo_dir=repo,
        artifacts_dir=artifacts,
        timeout=120,
        tempdir=tmp_path / "work",
    )

    assert outcome.ok
    assert (artifacts / output_file).exists()
    assert (artifacts / "logs" / "cmd.stdout").exists()
    marker = json.loads((artifacts / "trace.json").read_text(encoding="utf-8"))
    assert marker["tracer"] == tracer
    assert marker["status"] == "ok"
    assert marker["test_count"] >= 1


def test_execute_worker_trace_stamps_host_into_output_and_marker(tmp_path: Path, monkeypatch) -> None:
    """The machine lands in the tracer output, not just the marker, which download skips."""
    monkeypatch.setenv("SOURCEWORLDBENCH_NODE_NAME", "gke-xxx-nap-n2-standard-16-1-abcd1234-wxyz")
    monkeypatch.setenv("SOURCEWORLDBENCH_POD_NAME", "sourceworldbench-collect-run-w-deadbeef-x7zq7")
    monkeypatch.setenv("SOURCEWORLDBENCH_POD_UID", "6f0d0d3c-9d1f-4e33-9d0e-2f0a9a1b2c3d")
    monkeypatch.setenv("SOURCEWORLDBENCH_JOB_NAME", "sourceworldbench-collect-run-w-deadbeef")
    monkeypatch.setenv("SOURCEWORLDBENCH_RUN_NAME", "run")
    monkeypatch.setenv("SOURCEWORLDBENCH_CONTAINER_IMAGE", "registry.example.com/sourceworldbench/row@sha256:abc")
    monkeypatch.setenv(
        "SOURCEWORLDBENCH_RUNNER_IMAGE", "registry.example.com/sourceworldbench/sourceworldbench-runner:latest"
    )
    monkeypatch.setenv("SOURCEWORLDBENCH_CPU_LIMIT_MILLICORES", "8000")
    monkeypatch.setenv("SOURCEWORLDBENCH_MEMORY_LIMIT_BYTES", "34359738368")
    repo, row = _make_pytest_repo(tmp_path)
    input_path = tmp_path / "rows.jsonl"
    input_path.write_text(row.model_dump_json() + "\n", encoding="utf-8")
    artifacts = tmp_path / "artifacts"

    execute_worker_trace(
        input_path=input_path,
        instance_id=row.instance_id,
        tracer="walltime",
        repo_dir=repo,
        artifacts_dir=artifacts,
        timeout=120,
        tempdir=tmp_path / "work",
    )

    output = json.loads((artifacts / "walltime_output.json").read_text(encoding="utf-8"))
    marker = json.loads((artifacts / "trace.json").read_text(encoding="utf-8"))

    for payload in (output, marker):
        host = payload["host"]
        assert host["schema_version"] == HOST_SCHEMA_VERSION
        assert host["node"] == "gke-xxx-nap-n2-standard-16-1-abcd1234-wxyz"
        assert host["pod"] == "sourceworldbench-collect-run-w-deadbeef-x7zq7"
        assert host["pod_uid"] == "6f0d0d3c-9d1f-4e33-9d0e-2f0a9a1b2c3d"
        assert host["job"] == "sourceworldbench-collect-run-w-deadbeef"
        assert host["run"] == "run"
        assert host["container_image"] == "registry.example.com/sourceworldbench/row@sha256:abc"
        assert host["runner_image"] == "registry.example.com/sourceworldbench/sourceworldbench-runner:latest"
        assert host["cpu_limit_millicores"] == 8000
        assert host["memory_limit_bytes"] == 34359738368
        assert host["cpu_count"] >= 1
        assert host["platform"]

    # The output nests the pass block; the marker keeps those keys at the top
    # level, where `instance_id`, `tracer` and `status` already lived.
    for record in (output["pass"], marker):
        assert record["instance_id"] == row.instance_id
        assert record["tracer"] == "walltime"
        assert record["workload"] is False
        assert record["status"] == "ok"
        assert record["duration_s"] >= 0
        assert record["rebuild_s"] is None
        assert record["started_at"] <= record["finished_at"]


def test_the_trace_marker_records_how_long_the_rebuild_took(tmp_path: Path) -> None:
    """`duration_s` alone cannot say whether a pass spent its time rebuilding or testing."""
    repo, row = _make_pytest_repo(tmp_path)
    row = row.model_copy(update={"metadata": {REBUILD_COMMAND_METADATA_KEY: "sleep 0.4"}})
    input_path = tmp_path / "rows.jsonl"
    input_path.write_text(row.model_dump_json() + "\n", encoding="utf-8")
    artifacts = tmp_path / "artifacts"

    execute_worker_trace(
        input_path=input_path,
        instance_id=row.instance_id,
        tracer="walltime",
        repo_dir=repo,
        artifacts_dir=artifacts,
        timeout=120,
        tempdir=tmp_path / "work",
    )

    marker = json.loads((artifacts / "trace.json").read_text(encoding="utf-8"))
    assert marker["rebuild_s"] >= 0.4
    # The rebuild is part of the pass, so it cannot exceed the pass.
    assert marker["rebuild_s"] <= marker["duration_s"]


# A gVisor pod's own `/proc/cpuinfo`, trimmed: the sandbox blanks `model name`
# and `stepping` but leaves the CPUID numbers and the feature flags intact.
_GVISOR_CPUINFO = """processor\t: 0
vendor_id\t: GenuineIntel
cpu family\t: 6
model\t\t: 79
model name\t: unknown
stepping\t: unknown
cpu MHz\t\t: 2199.998
cache size\t: 8192 KB
flags\t\t: fpu vme de pse tsc msr avx avx2 bmi1 bmi2

processor\t: 1
vendor_id\t: GenuineIntel
model\t\t: 79
"""


@pytest.mark.parametrize(
    ("vendor", "family", "model", "normalized", "expected"),
    [
        # What a c2d pod really reports: 175 is 0xAF, the packed form of family 25.
        ("AuthenticAMD", 175, 1, 25, "AMD Milan"),
        ("AuthenticAMD", 25, 1, 25, "AMD Milan"),
        # 143 is 0x8F, the packed form of family 23; both encodings must agree.
        ("AuthenticAMD", 143, 49, 23, "AMD Rome"),
        ("AuthenticAMD", 23, 49, 23, "AMD Rome"),
        # Intel's base nibble is 6, so normalizing leaves it untouched.
        ("GenuineIntel", 6, 79, 6, "Intel Broadwell"),
        ("GenuineIntel", 6, 999, 6, None),
    ],
)
def test_cpu_info_names_the_silicon_gvisor_hides(
    tmp_path: Path, monkeypatch, vendor: str, family: int, model: int, normalized: int, expected: str | None
) -> None:
    """The CPUID numbers survive the sandbox, so the chip is identifiable without `model name`."""
    cpuinfo = tmp_path / "cpuinfo"
    cpuinfo.write_text(
        _GVISOR_CPUINFO.replace("GenuineIntel", vendor)
        .replace("cpu family\t: 6", f"cpu family\t: {family}")
        .replace("model\t\t: 79", f"model\t\t: {model}"),
        encoding="utf-8",
    )
    monkeypatch.setattr("sourceworldbench_benchmarks.trace_worker.CPUINFO_PATH", cpuinfo)

    info = cpu_info()

    assert info["microarch"] == expected
    assert info["vendor"] == vendor
    assert info["family"] == normalized
    assert info["model"] == model
    # "unknown" is the sandbox's placeholder, never reported as a real name.
    assert info["model_name"] is None
    assert info["stepping"] is None
    assert info["mhz"] == 2199.998
    # The file's `cache size` is a sandbox constant, so it is not reported.
    assert "cache_size" not in info
    assert info["flag_count"] == 10
    assert len(info["flags_hash"]) == 12


def test_cpu_info_tolerates_a_missing_cpuinfo(tmp_path: Path, monkeypatch) -> None:
    """Off a Linux node there is no `/proc/cpuinfo`; the block is empty, not an error."""
    monkeypatch.setattr("sourceworldbench_benchmarks.trace_worker.CPUINFO_PATH", tmp_path / "absent")

    info = cpu_info()

    assert info["vendor"] is None
    assert info["microarch"] is None
    assert info["flag_count"] == 0
    assert info["flags_hash"] is None


def test_execute_worker_trace_records_patch_failure_with_logs(tmp_path: Path) -> None:
    """A pass whose patch cannot apply writes logs and a non-ok marker, and is not fatal."""
    repo, row = _make_pytest_repo(tmp_path)
    broken = row.model_copy(update={"patch": "diff --git a/nope.py b/nope.py\n@@ -1 +1 @@\n-x\n+y\n"})
    input_path = tmp_path / "rows.jsonl"
    input_path.write_text(broken.model_dump_json() + "\n", encoding="utf-8")
    artifacts = tmp_path / "artifacts"

    outcome = execute_worker_trace(
        input_path=input_path,
        instance_id=broken.instance_id,
        tracer="trace",
        repo_dir=repo,
        artifacts_dir=artifacts,
        timeout=120,
        tempdir=tmp_path / "work",
    )

    assert not outcome.ok
    assert (artifacts / "logs" / "cmd.stderr").exists()
    marker = json.loads((artifacts / "trace.json").read_text(encoding="utf-8"))
    assert marker["status"] == "PatchApplyFailure"
    assert marker["test_count"] == 0


def test_execute_worker_trace_tolerates_unreadable_tracer_output(tmp_path: Path) -> None:
    """A pass that leaves a truncated output file (e.g. killed mid-write) is recorded, not fatal."""
    repo, row = _make_pytest_repo(tmp_path)
    truncated_writer = row.model_copy(
        update={"command": """python -c 'import os; open(os.environ["SWEBENCH_TRACE_OUTPUT"], "w").write("{")'"""}
    )
    input_path = tmp_path / "rows.jsonl"
    input_path.write_text(truncated_writer.model_dump_json() + "\n", encoding="utf-8")
    artifacts = tmp_path / "artifacts"

    outcome = execute_worker_trace(
        input_path=input_path,
        instance_id=truncated_writer.instance_id,
        tracer="trace",
        repo_dir=repo,
        artifacts_dir=artifacts,
        timeout=120,
        tempdir=tmp_path / "work",
    )

    assert not outcome.ok
    assert not (artifacts / "trace_output.json").exists()
    marker = json.loads((artifacts / "trace.json").read_text(encoding="utf-8"))
    assert marker["status"] == "invalid_output"
    assert marker["test_count"] == 0
