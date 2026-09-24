"""Unit tests for the tracer bootstrap.

The bootstrap is exercised end-to-end inside Docker; these tests cover
just the activation contract — that `sitecustomize.py` loads
`tracer.py` under the `sourceworldbench_tracer` module name, and that
`sourceworldbench_tracer_plugin.py` re-exports its `pytest_*` hooks.

We launch a fresh `python -c` subprocess with PYTHONPATH pointed at a
staged bundle so the host process's `sys.modules` isn't polluted, then
inspect what the child sees.
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from sourceworldbench_benchmarks.execution_tracer.bootstrap import PYTEST_PLUGIN, SITECUSTOMIZE


def _stage_bundle(tmp_path: Path, tracer_source: str) -> Path:
    """Copy bootstrap files + a synthetic tracer.py into a tempdir."""
    bundle = tmp_path / "tracing"
    bundle.mkdir()
    shutil.copy(SITECUSTOMIZE, bundle / "sitecustomize.py")
    shutil.copy(PYTEST_PLUGIN, bundle / "sourceworldbench_tracer_plugin.py")
    (bundle / "tracer.py").write_text(tracer_source)
    return bundle


def _run_python(bundle: Path, code: str) -> subprocess.CompletedProcess[str]:
    env = {"PYTHONPATH": str(bundle), "PATH": "/usr/bin:/bin"}
    return subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def test_sitecustomize_imports_tracer_under_stable_name(tmp_path: Path) -> None:
    bundle = _stage_bundle(tmp_path, "MARKER = 'loaded'\n")
    proc = _run_python(
        bundle,
        "import sys; import sourceworldbench_tracer; print(sourceworldbench_tracer.MARKER); "
        "print('sourceworldbench_tracer' in sys.modules)",
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == ["loaded", "True"]


def test_sitecustomize_swallows_tracer_errors(tmp_path: Path) -> None:
    bundle = _stage_bundle(tmp_path, "raise RuntimeError('boom')\n")
    proc = _run_python(
        bundle,
        "import sys; print('sourceworldbench_tracer' in sys.modules); print('post-import ok')",
    )
    assert proc.returncode == 0, proc.stderr
    assert "[sourceworldbench-tracer] failed to load tracer.py" in proc.stderr
    # Failed load should not leave a broken module in sys.modules.
    assert proc.stdout.splitlines() == ["False", "post-import ok"]


def test_sitecustomize_noop_when_tracer_missing(tmp_path: Path) -> None:
    bundle = tmp_path / "tracing"
    bundle.mkdir()
    shutil.copy(SITECUSTOMIZE, bundle / "sitecustomize.py")
    proc = _run_python(
        bundle,
        "import sys; print('sourceworldbench_tracer' in sys.modules); print('still running')",
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == ["False", "still running"]


def test_pytest_plugin_reexports_hooks(tmp_path: Path) -> None:
    tracer_source = (
        "def pytest_runtest_call(item):\n"
        "    pass\n"
        "def pytest_sessionfinish(session, exitstatus):\n"
        "    pass\n"
        "def helper():\n"  # non-hook, should NOT be re-exported
        "    pass\n"
    )
    bundle = _stage_bundle(tmp_path, tracer_source)
    proc = _run_python(
        bundle,
        "import sourceworldbench_tracer_plugin; "
        "print(hasattr(sourceworldbench_tracer_plugin, 'pytest_runtest_call')); "
        "print(hasattr(sourceworldbench_tracer_plugin, 'pytest_sessionfinish')); "
        "print(hasattr(sourceworldbench_tracer_plugin, 'helper'))",
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == ["True", "True", "False"]


def test_pytest_plugin_noop_when_tracer_not_loaded(tmp_path: Path) -> None:
    """Plugin should import without error even if sitecustomize never ran."""
    bundle = tmp_path / "no_sitecustomize"
    bundle.mkdir()
    shutil.copy(PYTEST_PLUGIN, bundle / "sourceworldbench_tracer_plugin.py")
    proc = subprocess.run(
        [sys.executable, "-c", "import sourceworldbench_tracer_plugin; print('ok')"],
        env={"PYTHONPATH": str(bundle), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"


@pytest.mark.skipif(sys.version_info < (3, 5), reason="floor is 3.5")
def test_bootstrap_files_are_python35_compatible() -> None:
    """The bootstrap targets the same floor as the tracers themselves."""
    import py_compile

    for src in (SITECUSTOMIZE, PYTEST_PLUGIN):
        py_compile.compile(str(src), doraise=True)
