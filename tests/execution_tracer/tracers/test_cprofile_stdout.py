"""Tests that the cprofile tracer keeps a workload's own printed timing.

Each test drives the real bundle through a real pytest subprocess, the same
activation path a worker pod uses.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from sourceworldbench_benchmarks.execution_tracer.bootstrap import PYTEST_PLUGIN, SITECUSTOMIZE
from sourceworldbench_benchmarks.execution_tracer.runner.spec import BY_NAME

# Setup runs in the test body; only the inner call goes to `timeit`.
_SETUP_SECONDS = 1.0
_WORKLOAD = """
def test_workload():
    import statistics, time, timeit
    import pkg

    time.sleep({setup})

    def workload():
        pkg.hot(2000)

    runtimes = timeit.repeat(workload, number=1, repeat=5)
    print("Mean:", statistics.mean(runtimes))
    print("Std Dev:", statistics.stdev(runtimes))
""".format(setup=_SETUP_SECONDS)


def _run_workload(tmp_path: Path, test_source: str, keep_stdout: bool = True) -> dict:
    """Stage the real bundle, run one test under it, return its tracer record."""
    bundle = tmp_path / "tracing"
    bundle.mkdir()
    shutil.copy(SITECUSTOMIZE, bundle / "sitecustomize.py")
    shutil.copy(PYTEST_PLUGIN, bundle / "sourceworldbench_tracer_plugin.py")
    shutil.copy(BY_NAME["cprofile"].source_file, bundle / "tracer.py")

    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text(
        "def hot(n):\n    return sum(i * i for i in range(n))\n", encoding="utf-8"
    )

    test_file = tmp_path / "test_workload.py"
    test_file.write_text(test_source, encoding="utf-8")
    out_path = tmp_path / "cprofile_output.json"

    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(bundle) + ":" + str(repo),
        "PYTEST_PLUGINS": "sourceworldbench_tracer_plugin",
        "SWEBENCH_PROF_MODE": "pytest",
        "SWEBENCH_PROF_OUTPUT": str(out_path),
        "SWEBENCH_REPO_DIR": str(repo),
    }
    if keep_stdout:
        env["SWEBENCH_PROF_KEEP_STDOUT"] = "1"

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(test_file), "-q", "-p", "no:cacheprovider"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    tests = json.loads(out_path.read_text(encoding="utf-8"))["tests"]
    assert len(tests) == 1, tests
    return list(tests.values())[0]


def test_timeit_summary_is_parsed_out_of_captured_stdout(tmp_path: Path) -> None:
    rec = _run_workload(tmp_path, _WORKLOAD)

    assert rec["workload_mean_s"] > 0.0
    assert rec["workload_stdev_s"] >= 0.0
    # The tracer still sees the repo code, so the profile is unaffected.
    assert "pkg.__init__.hot" in rec["functions"]


def test_wall_time_includes_setup_that_the_mean_excludes(tmp_path: Path) -> None:
    """On the scikit-learn row the setup is a dataset download: 88% of wall."""
    rec = _run_workload(tmp_path, _WORKLOAD)

    assert rec["wall_time_s"] >= _SETUP_SECONDS
    assert rec["workload_mean_s"] < _SETUP_SECONDS / 10


def test_raw_stdout_is_kept_so_other_formats_stay_recoverable(tmp_path: Path) -> None:
    """A workload printing some other shape must not become unrecoverable."""
    rec = _run_workload(
        tmp_path,
        "def test_workload():\n    print('elapsed=1.25 over 5 reps')\n",
    )

    assert "elapsed=1.25 over 5 reps" in rec["stdout"]
    # Nothing matched, so the parsed keys stay absent.
    assert "workload_mean_s" not in rec
    assert "workload_stdev_s" not in rec


def test_silent_test_records_empty_stdout(tmp_path: Path) -> None:
    """Empty `stdout` means "printed nothing"; absent means "not collected"."""
    rec = _run_workload(tmp_path, "def test_workload():\n    pass\n")

    assert rec["stdout"] == ""
    assert rec["stdout_truncated"] is False
    assert "workload_mean_s" not in rec


def test_parser_matches_swefficiency_tolerances(tmp_path: Path) -> None:
    """Upstream tolerates a missing space in `Std Dev` and a non-numeric value."""
    rec = _run_workload(
        tmp_path,
        "def test_workload():\n"
        "    print('Mean:', 1.5e-05)\n"
        "    print('StdDev:', 'nan')\n",
    )

    assert rec["workload_mean_s"] == 1.5e-05
    # `float("nan") != float("nan")`.
    assert rec["workload_stdev_s"] != rec["workload_stdev_s"]


def test_parser_ignores_before_and_after_prefixed_means(tmp_path: Path) -> None:
    """Upstream's unanchored findall matches inside these and pairs them wrongly."""
    rec = _run_workload(
        tmp_path,
        "def test_workload():\n"
        "    print('Before Mean:', 99.0)\n"
        "    print('After Mean:', 88.0)\n"
        "    print('Mean:', 0.25)\n"
        "    print('Std Dev:', 0.01)\n",
    )

    assert rec["workload_mean_s"] == 0.25
    assert rec["workload_stdev_s"] == 0.01


def test_regular_suite_run_keeps_no_stdout_at_all(tmp_path: Path) -> None:
    """Absent, not empty: a suite pass can run 178k tests."""
    rec = _run_workload(tmp_path, _WORKLOAD, keep_stdout=False)

    assert "stdout" not in rec
    assert "stdout_truncated" not in rec
    assert "workload_mean_s" not in rec
    # The profile itself is untouched by the flag.
    assert "pkg.__init__.hot" in rec["functions"]
    assert rec["wall_time_s"] >= _SETUP_SECONDS


def test_chatty_test_is_truncated_to_the_tail(tmp_path: Path) -> None:
    """The summary is printed last, so the tail keeps it."""
    rec = _run_workload(
        tmp_path,
        "def test_workload():\n"
        "    print('x' * 40000)\n"
        "    print('Mean:', 0.5)\n"
        "    print('Std Dev:', 0.25)\n",
    )

    assert rec["stdout_truncated"] is True
    assert len(rec["stdout"]) == 16384
    # Truncated from the front, so the numbers survive.
    assert rec["workload_mean_s"] == 0.5
    assert rec["workload_stdev_s"] == 0.25
