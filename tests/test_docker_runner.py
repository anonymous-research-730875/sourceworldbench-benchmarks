import subprocess
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from sourceworldbench_benchmarks import docker_runner


def test_run_state_appends_extra_mounts_and_env(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def _fake_invoke(args: list[str], timeout: int) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(docker_runner, "_invoke", _fake_invoke)

    tracer_dir = tmp_path / "tracing"
    out_dir = tmp_path / "out"
    docker_runner.run_state(
        image="example:dev",
        script="true",
        host_results=tmp_path / "results",
        patch_path=None,
        timeout=10,
        extra_mounts=[
            (tracer_dir, "/sourceworldbench-tracing", True),
            (out_dir, "/sourceworldbench-tracing-out", False),
        ],
        extra_env={
            "SOURCEWORLDBENCH_TRACE_MODE": "pytest",
            "SOURCEWORLDBENCH_TRACE_OUTPUT": "/sourceworldbench-tracing-out/trace.json",
        },
    )

    [args] = calls
    mounts = [args[i + 1] for i, tok in enumerate(args) if tok == "--mount"]
    assert f"type=bind,src={tracer_dir},dst=/sourceworldbench-tracing,ro" in mounts
    assert f"type=bind,src={out_dir},dst=/sourceworldbench-tracing-out" in mounts
    envs = [args[i + 1] for i, tok in enumerate(args) if tok == "--env"]
    assert "SOURCEWORLDBENCH_TRACE_MODE=pytest" in envs
    assert "SOURCEWORLDBENCH_TRACE_OUTPUT=/sourceworldbench-tracing-out/trace.json" in envs


def test_run_state_no_extras_unchanged(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def _fake_invoke(args: list[str], timeout: int) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(docker_runner, "_invoke", _fake_invoke)

    docker_runner.run_state(
        image="example:dev",
        script="true",
        host_results=tmp_path / "results",
        patch_path=None,
        timeout=10,
    )

    [args] = calls
    assert "--env" not in args
    # Only the fixed /results mount.
    mount_specs = [args[i + 1] for i, tok in enumerate(args) if tok == "--mount"]
    assert mount_specs == [f"type=bind,src={tmp_path / 'results'},dst=/results"]


def _fake_invoke_returning(returncode: int):
    def _fake_invoke(
        args: list[str], timeout: int, *, capture_output: bool = True
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=b"out", stderr=b"err")

    return _fake_invoke


def test_run_state_exit_99_without_patch_is_a_command_result(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    """A patchless command exiting 99 must not be misread as a patch failure."""
    monkeypatch.setattr(docker_runner, "_invoke", _fake_invoke_returning(99))

    result = docker_runner.run_state(image="img", script="exit 99", host_results=tmp_path, timeout=10)

    assert result.exit_code == 99


def test_run_state_exit_99_with_patch_raises_patch_apply_failed(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(docker_runner, "_invoke", _fake_invoke_returning(99))
    patch_path = tmp_path / "patch.diff"
    patch_path.write_text("diff", encoding="utf-8")

    with pytest.raises(docker_runner.PatchApplyFailed) as exc_info:
        docker_runner.run_state(image="img", script="cmd", host_results=tmp_path, patch_path=patch_path, timeout=10)

    assert exc_info.value.stderr == b"err"


def test_buildx_build_forwards_optional_flags(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def _fake_invoke(
        args: list[str], timeout: int, *, capture_output: bool = True
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(docker_runner, "_invoke", _fake_invoke)

    docker_runner.buildx_build(
        dockerfile=tmp_path / "Dockerfile",
        context=tmp_path,
        tag="example:dev",
        build_args={"A": "B"},
        ssh="default",
        push=True,
        timeout=60,
        platform="linux/amd64",
    )

    [args] = calls
    assert args[args.index("--ssh") + 1] == "default"
    assert args[args.index("--platform") + 1] == "linux/amd64"
    assert "--push" in args
