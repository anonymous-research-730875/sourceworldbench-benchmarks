"""trace_one with the docker call stubbed out.

The stub writes a fake trace JSON into the bind-mounted output dir to
simulate a successful tracer run; this exercises bundle staging and
trace-artifact collection for a single state.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from sourceworldbench_benchmarks.docker_runner import RunResult
from sourceworldbench_benchmarks.execution_tracer.runner import runner as runner_mod
from sourceworldbench_benchmarks.execution_tracer.runner import trace_one
from sourceworldbench_benchmarks.execution_tracer.runner.spec import TRACE
from sourceworldbench_benchmarks.schema import StateDatapoint

_PATCH = "diff --git a/src/foo.py b/src/foo.py\n--- a/src/foo.py\n+++ b/src/foo.py\n@@\n-x\n+y\n"


def _make_datapoint(*, instance_id: str = "demo__abcd__base", patch: str | None = None) -> StateDatapoint:
    return StateDatapoint.model_validate(
        {
            "instance_id": instance_id,
            "repo": "demo/demo",
            "base_commit": "0" * 40,
            "patch": patch,
            "container": "demo:latest",
            "command": "/app/.venv/bin/pytest -q",
            "test_scope": ["tests/"],
        }
    )


def _stub_run_with_tracer(*, payload: dict | None = None):
    """Return a stand-in for `run_with_tracer` that drops a fake trace JSON."""

    def _stub(**kwargs):
        out_dir: Path = kwargs["trace_output_dir"]
        if payload is not None:
            (out_dir / TRACE.output_filename).write_text(json.dumps(payload), encoding="utf-8")
        return RunResult(exit_code=0, stdout=b"", stderr=b"")

    return _stub


@pytest.mark.parametrize(
    "instance_id, patch",
    [
        ("demo__abcd__base", None),
        ("demo__abcd__ruff", _PATCH),
    ],
)
def test_trace_one_collects_artifact_under_instance_dir(
    tmp_path: Path, monkeypatch: MonkeyPatch, instance_id: str, patch: str | None
) -> None:
    monkeypatch.setattr(
        runner_mod, "run_with_tracer", _stub_run_with_tracer(payload={"tests": {}})
    )
    dp = _make_datapoint(instance_id=instance_id, patch=patch)
    out_dir = tmp_path / "traces"

    result = trace_one(dp, tracer_spec=TRACE, out_dir=out_dir, timeout=10)

    assert result.ok
    assert result.trace_path is not None and result.trace_path.exists()
    assert result.trace_path.parent == out_dir / instance_id
    assert json.loads(result.trace_path.read_text()) == {"tests": {}}


def test_trace_one_marks_missing_output(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    # payload=None -> stub writes nothing -> trace collection sees no file
    monkeypatch.setattr(runner_mod, "run_with_tracer", _stub_run_with_tracer(payload=None))
    dp = _make_datapoint()

    result = trace_one(
        dp,
        tracer_spec=TRACE,
        out_dir=tmp_path / "traces",
        timeout=10,
    )

    assert not result.ok
    assert result.error == "no_trace_output"


def test_trace_one_forwards_repo_dir_into_script(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    seen_scripts: list[str] = []

    def _capture(**kwargs):
        seen_scripts.append(kwargs["script"])
        (kwargs["trace_output_dir"] / TRACE.output_filename).write_text("{}", encoding="utf-8")
        return RunResult(exit_code=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(runner_mod, "run_with_tracer", _capture)
    dp = _make_datapoint()

    trace_one(
        dp,
        tracer_spec=TRACE,
        out_dir=tmp_path / "traces",
        timeout=10,
        repo_dir="/testbed",
    )

    [script] = seen_scripts
    assert "export SWEBENCH_REPO_DIR=/testbed" in script


def test_trace_one_stages_bootstrap_and_tracer_into_bundle(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def _capture(**kwargs):
        bundle: Path = kwargs["bundle_dir"]
        # The tempdir holding `bundle` is cleaned up when trace_one returns,
        # so snapshot what we need NOW.
        seen["names"] = sorted(p.name for p in bundle.iterdir())
        seen["tracer_source"] = (bundle / "tracer.py").read_text()
        (kwargs["trace_output_dir"] / TRACE.output_filename).write_text("{}", encoding="utf-8")
        return RunResult(exit_code=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(runner_mod, "run_with_tracer", _capture)
    dp = _make_datapoint()

    trace_one(
        dp,
        tracer_spec=TRACE,
        out_dir=tmp_path / "traces",
        timeout=10,
    )

    assert seen["names"] == ["sitecustomize.py", "sourceworldbench_tracer_plugin.py", "tracer.py"]
    assert "SWEBENCH_TRACE_OUTPUT" in seen["tracer_source"]  # type: ignore[operator]
