"""Tests for the swefficiency row -> datapoint adaptation (``swerebench.from_sweff``).

The sympy regression: swefficiency runs sympy through its own ``bin/test``, which
never honours the ``--junitxml`` that :func:`create_docker.render_test_command`
appends. Older commits reject the flag (exit 2, nothing runs); newer ones delegate to
pytest but filter it out, so the suite runs for ~40 minutes and writes no report. Both
shapes fail the row, so sympy must be driven through pytest directly.
"""

from sourceworldbench_benchmarks.swerebench.create_docker import render_test_command
from sourceworldbench_benchmarks.swerebench.from_sweff import sweff_row_to_dp

TEST_SYMPY = "PYTHONWARNINGS='ignore::UserWarning,ignore::SyntaxWarning' bin/test --no-subprocess --verbose"
TEST_PYTEST = "pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors"


def _row(repo: str, test_cmd: str) -> dict:
    return {
        "repo": repo,
        "test_cmd": test_cmd,
        "image_name": "ghcr.io/swefficiency/x:latest",
        "rebuild_cmd": "pip install -e .",
    }


def _cmd(repo: str, test_cmd: str) -> str:
    return sweff_row_to_dp(_row(repo, test_cmd))["install_config"]["test_cmd"]


def test_sympy_runs_pytest_not_bin_test():
    # The sympy images don't ship pytest, so the command installs it first and
    # runs ``python -m pytest`` (the pip user-install's scripts dir is off PATH).
    cmd = _cmd("sympy/sympy", TEST_SYMPY)
    assert "bin/test" not in cmd
    assert cmd.startswith("python -m pip install pytest 1>&2 && ")
    assert " python -m pytest " in cmd


def test_sympy_scopes_collection_to_the_test_suite():
    # Only newer commits set ``testpaths``; without an explicit path the older ones
    # collect ``doc/`` and ``examples/`` as well.
    assert _cmd("sympy/sympy", TEST_SYMPY).endswith(" sympy")


def test_sympy_command_accepts_the_appended_junitxml_flag():
    # render_test_command appends the report flags; the positional path must not
    # swallow them, and pytest must be the thing parsing them.
    full = render_test_command(_cmd("sympy/sympy", TEST_SYMPY))
    assert " pytest " in full
    assert full.endswith("sympy --junitxml=/results/junit.xml --continue-on-collection-errors")


def test_pytest_repos_get_the_xdist_probe():
    # Repos without their own xdist config (pandas/dask) get the runtime probe,
    # which enables parallelism only where pytest-xdist is actually installed.
    cmd = _cmd("astropy/astropy", TEST_PYTEST)
    assert cmd.startswith("pytest $(python -c 'import xdist'")
    assert cmd.endswith(TEST_PYTEST.removeprefix("pytest "))


def test_numpy_is_scoped_to_its_package_dir():
    # Without the scope, pytest collects numpy's vendored meson test scripts,
    # one of which calls exit(0) at import and aborts the whole session.
    assert _cmd("numpy/numpy", TEST_PYTEST).endswith(" numpy/")


def test_overrides_compose_with_ci_xdist_flags():
    # pandas is not in _TEST_CMD_OVERRIDES, so the runner swap leaves it alone and
    # the xdist substitution still applies. Asserts the composition, not the exact
    # flag string, which is tuned independently.
    cmd = _cmd("pandas-dev/pandas", f"{TEST_PYTEST} -n 4")
    assert cmd.startswith("PYTEST_XDIST_AUTO_NUM_WORKERS=8 pytest ")
    assert "-n auto --dist=loadfile" in cmd
    assert " -n 4" not in cmd  # the dataset's serial worker count is replaced
