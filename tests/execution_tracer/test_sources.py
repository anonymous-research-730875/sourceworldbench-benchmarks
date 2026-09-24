"""Docker-free tests for the on-demand source fetcher."""

from __future__ import annotations

from pathlib import Path

import pytest

from sourceworldbench_benchmarks.docker_runner import DockerError, RunResult
from sourceworldbench_benchmarks.execution_tracer import sources as sources_mod
from sourceworldbench_benchmarks.execution_tracer.sources import (
    CONTAINER_OUT_DIR,
    _build_extract_command,
    fetch_sources,
    materialize_to_dir,
)
from sourceworldbench_benchmarks.schema import StateDatapoint


def _datapoint(**overrides) -> StateDatapoint:
    payload = {
        "instance_id": "demo__demo-1__base",
        "repo": "demo/demo",
        "base_commit": "a" * 40,
        "patch": "diff --git a/src/x.py b/src/x.py\n--- a/src/x.py\n+++ b/src/x.py\n@@\n-1\n+2\n",
        "container": "demo:latest",
        "command": "pytest",
        "test_scope": ["tests/"],
    }
    payload.update(overrides)
    return StateDatapoint.model_validate(payload)


def test_build_extract_command_targets_repo_dir_and_paths_file() -> None:
    cmd = _build_extract_command("/testbed")
    assert "cd /testbed" in cmd
    assert CONTAINER_OUT_DIR in cmd
    assert "/_paths.txt" in cmd
    assert "cp \"$p\"" in cmd


def test_fetch_sources_returns_empty_when_no_paths() -> None:
    assert fetch_sources(_datapoint(), [], repo_dir="/testbed") == {}


def test_fetch_sources_reads_files_from_mount_after_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}

    def _fake_run_state(**kwargs):
        captured.update(kwargs)
        # Find the bind-mount for the source-out dir; simulate the container
        # cp loop by populating two of the three requested files on the host.
        extra = kwargs["extra_mounts"]
        out_dir = next(src for src, dst, _ro in extra if dst == CONTAINER_OUT_DIR)
        (out_dir / "src").mkdir(parents=True, exist_ok=True)
        (out_dir / "src" / "x.py").write_text("print('augmented')\n")
        (out_dir / "tests").mkdir(parents=True, exist_ok=True)
        (out_dir / "tests" / "t.py").write_text("assert True\n")
        # `src/missing.py` is intentionally NOT written — should be omitted.
        return RunResult(exit_code=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(sources_mod, "run_state", _fake_run_state)

    result = fetch_sources(
        _datapoint(),
        ["src/x.py", "src/missing.py", "tests/t.py"],
        repo_dir="/testbed",
    )

    assert result == {
        "src/x.py": "print('augmented')\n",
        "tests/t.py": "assert True\n",
    }
    # A state carrying a patch passes a patch_path; a bare base would not.
    assert captured["patch_path"] is not None
    assert captured["image"] == "demo:latest"


def test_fetch_sources_base_state_omits_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}

    def _fake_run_state(**kwargs):
        captured.update(kwargs)
        return RunResult(exit_code=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(sources_mod, "run_state", _fake_run_state)
    fetch_sources(_datapoint(patch=None), ["src/x.py"], repo_dir="/testbed")
    assert captured["patch_path"] is None


def test_fetch_sources_returns_empty_on_docker_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(**_kwargs):
        raise DockerError("nope", stdout=b"", stderr=b"")

    monkeypatch.setattr(sources_mod, "run_state", _boom)
    assert fetch_sources(_datapoint(), ["src/x.py"], repo_dir="/testbed") == {}


def test_materialize_to_dir_writes_nested_files(tmp_path: Path) -> None:
    materialize_to_dir(
        {"src/a/b.py": "B\n", "tests/t.py": "T\n"},
        tmp_path / "snap",
    )
    assert (tmp_path / "snap" / "src" / "a" / "b.py").read_text() == "B\n"
    assert (tmp_path / "snap" / "tests" / "t.py").read_text() == "T\n"
