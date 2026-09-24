import subprocess

import pytest

from sourceworldbench_benchmarks.execution import REBUILD_COMMAND_METADATA_KEY, wrap_command
from sourceworldbench_benchmarks.execution_tracer.runner.script import (
    CONTAINER_BUNDLE_DIR,
    CONTAINER_OUT_DIR,
    build_tracer_script,
    extract_patch_dirs,
    insert_pytest_args,
    resolve_trace_paths,
)
from sourceworldbench_benchmarks.execution_tracer.runner.spec import TRACE
from sourceworldbench_benchmarks.schema import StateDatapoint

_AUG_PATCH = "diff --git a/src/foo.py b/src/foo.py\n--- a/src/foo.py\n+++ b/src/foo.py\n@@\n-1\n+2\n"


def _make_datapoint(**overrides) -> StateDatapoint:
    payload = {
        "instance_id": "demo__abcd__base",
        "repo": "demo/demo",
        "base_commit": "0" * 40,
        "patch": None,
        "container": "demo:latest",
        "command": "/app/.venv/bin/pytest -q",
        "test_scope": ["tests/"],
    }
    payload.update(overrides)
    return StateDatapoint.model_validate(payload)


def test_extract_patch_dirs_top_levels() -> None:
    patch = (
        "diff --git a/src/foo.py b/src/foo.py\n"
        "diff --git a/tests/test_foo.py b/tests/test_foo.py\n"
        "diff --git a/README.md b/README.md\n"
    )
    assert extract_patch_dirs(patch) == ["README.md", "src", "tests"]


def test_extract_patch_dirs_handles_multiple_patches_and_none() -> None:
    patch_a = "diff --git a/src/a.py b/src/a.py\n"
    patch_b = "diff --git a/lib/b.py b/lib/b.py\n"
    assert extract_patch_dirs(patch_a, patch_b, None) == ["lib", "src"]


def test_resolve_trace_paths_patch_scope_follows_the_patch() -> None:
    patch = "diff --git a/src/a.py b/src/a.py\ndiff --git a/docs/x.md b/docs/x.md\n"
    assert resolve_trace_paths("patch", patch) == ["docs", "src"]


def test_resolve_trace_paths_patch_scope_can_miss_every_source_dir() -> None:
    # A patch touching only build config leaves the tracer with nothing to
    # record — the failure mode `repo` scope exists to avoid.
    assert resolve_trace_paths("patch", "diff --git a/pyproject.toml b/pyproject.toml\n") == ["pyproject.toml"]


def test_resolve_trace_paths_repo_scope_ignores_the_patch() -> None:
    # Empty list is how every tracer spells "the whole repository".
    assert resolve_trace_paths("repo", "diff --git a/src/a.py b/src/a.py\n") == []
    assert resolve_trace_paths("repo", None) == []


def test_insert_pytest_args_keeps_original_flags() -> None:
    assert insert_pytest_args("/app/.venv/bin/pytest -q", ["a.py::t"]) == "/app/.venv/bin/pytest a.py::t -q"
    assert insert_pytest_args("python -m pytest --tb=no", ["a.py::t"]) == "python -m pytest a.py::t --tb=no"


def test_insert_pytest_args_skips_a_pip_install_of_pytest() -> None:
    # sympy installs pytest before running it; the first `pytest` in the string
    # is a pip argument, not the invocation.
    command = "python -m pip install pytest 1>&2 && python -m pytest --no-header sympy"
    assert insert_pytest_args(command, ["/tmp/w.py"]) == (
        "python -m pip install pytest 1>&2 && python -m pytest /tmp/w.py --no-header sympy"
    )


@pytest.mark.parametrize("command", ["python setup.py test", "cat pytest.ini", "pip install pytest"])
def test_insert_pytest_args_rejects_commands_that_run_no_pytest(command: str) -> None:
    with pytest.raises(ValueError, match="no `pytest` invocation"):
        insert_pytest_args(command, ["a.py::t"])


def test_insert_pytest_args_quotes_node_ids_for_sh() -> None:
    """The script is handed to `/bin/sh -c` as one argv element, so POSIX quoting applies."""
    nodes = ["tests/t.py::test[axis='columns']", 'tests/t.py::test[s="d"]', "tests/t.py::test[a b$PATH`id`]"]
    command = insert_pytest_args("pytest", nodes)
    # stand `printf` in for pytest to read back the argv a real shell parsed
    echoed = subprocess.run(
        ["/bin/sh", "-c", command.replace("pytest", "printf '%s\\n'", 1)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert echoed.stdout.splitlines() == nodes


def test_build_tracer_script_base_applies_no_patch() -> None:
    dp = _make_datapoint(patch=None)
    script = build_tracer_script(dp, spec=TRACE, repo_dir="/app")
    # tracer activation exports come first
    assert script.index("export PYTHONPATH=") < script.index("/app/.venv/bin/pytest")
    assert f"export PYTHONPATH={CONTAINER_BUNDLE_DIR}:" in script
    assert "export PYTEST_PLUGINS=sourceworldbench_tracer_plugin" in script
    assert f"export SWEBENCH_TRACE_OUTPUT={CONTAINER_OUT_DIR}/trace_output.json" in script
    assert "export SWEBENCH_TRACE_MODE=pytest" in script
    # both the configured root and the /testbed symlink target are exported
    assert "export SWEBENCH_REPO_DIR=/app:/testbed" in script
    # base state carries no patch -> no apply line
    assert "/work/patch.diff" not in script
    # user command still present at end
    assert script.endswith("/app/.venv/bin/pytest -q")


def test_build_tracer_script_augmented_applies_patch_before_command() -> None:
    dp = _make_datapoint(patch=_AUG_PATCH)
    script = build_tracer_script(dp, spec=TRACE, repo_dir="/app")
    # the state's patch is git-applied before the user command runs
    i_patch = script.index("/work/patch.diff")
    i_cmd = script.index("/app/.venv/bin/pytest")
    assert i_patch < i_cmd


def test_build_tracer_script_writes_patch_dirs_into_paths_env() -> None:
    dp = _make_datapoint()
    script = build_tracer_script(
        dp, spec=TRACE, repo_dir="/app", trace_paths=["src", "tests"]
    )
    assert "export SWEBENCH_TRACE_PATHS=src,tests" in script


def test_build_tracer_script_rebuilds_between_the_patch_and_the_command() -> None:
    """Without the rebuild, a patch touching compiled code is traced against a stale binary."""
    dp = _make_datapoint(
        patch=_AUG_PATCH,
        metadata={REBUILD_COMMAND_METADATA_KEY: "pip install -e ."},
    )
    script = build_tracer_script(dp, spec=TRACE, repo_dir="/app")
    i_patch = script.index("/work/patch.diff")
    i_rebuild = script.index("pip install -e .")
    i_cmd = script.index("/app/.venv/bin/pytest")
    assert i_patch < i_rebuild < i_cmd


def test_build_tracer_script_omits_the_rebuild_when_the_row_declares_none() -> None:
    dp = _make_datapoint(patch=_AUG_PATCH)
    assert "exit 98" not in build_tracer_script(dp, spec=TRACE, repo_dir="/app")


def test_build_tracer_script_wraps_the_command_exactly_as_the_collect_path_does() -> None:
    """The tracer and the collect backends must build the same patch/rebuild/command body."""
    dp = _make_datapoint(
        patch=_AUG_PATCH,
        metadata={REBUILD_COMMAND_METADATA_KEY: "pip install -e ."},
    )
    script = build_tracer_script(dp, spec=TRACE, repo_dir="/app")
    expected = wrap_command(dp.command, has_patch=True, rebuild_command="pip install -e .")
    assert script.endswith(expected)
