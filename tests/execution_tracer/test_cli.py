"""Smoke tests for the sourceworldbench-benchmarks execution-tracer Typer CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sourceworldbench_benchmarks.docker_runner import RunResult
from sourceworldbench_benchmarks.execution_tracer import cli as cli_mod
from sourceworldbench_benchmarks.execution_tracer.cli import app
from sourceworldbench_benchmarks.execution_tracer.runner.runner import TraceResult

runner = CliRunner()


_VALID_PAYLOAD = {
    "instance_id": "demo__demo-1__base",
    "repo": "demo/demo",
    "base_commit": "a" * 40,
    "patch": "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@\n-1\n+2\n",
    "container": "demo:latest",
    "command": "pytest",
    "test_scope": ["tests/"],
}


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_trace_invokes_trace_one_for_each_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    in_path = tmp_path / "in.jsonl"
    _write_jsonl(in_path, [_VALID_PAYLOAD, dict(_VALID_PAYLOAD, instance_id="demo__demo-2__base")])
    out_dir = tmp_path / "out"

    called: list[str] = []

    def _fake_trace_one(datapoint, *, tracer_spec, out_dir, timeout, repo_dir, trace_paths, command_override=None):
        called.append(datapoint.instance_id)
        assert tracer_spec.name == "trace"
        assert repo_dir == "/app"
        assert timeout == 1800
        return TraceResult(
            instance_id=datapoint.instance_id,
            tracer=tracer_spec.name,
            run_result=RunResult(exit_code=0, stdout=b"", stderr=b""),
            trace_path=out_dir / datapoint.instance_id / "trace.json",
        )

    monkeypatch.setattr(cli_mod, "trace_one", _fake_trace_one)

    result = runner.invoke(
        app, ["trace", "--in", str(in_path), "--out-dir", str(out_dir), "--tracer", "trace"]
    )
    assert result.exit_code == 0, result.output
    assert called == ["demo__demo-1__base", "demo__demo-2__base"]


def test_trace_rejects_unknown_tracer(tmp_path: Path) -> None:
    in_path = tmp_path / "in.jsonl"
    _write_jsonl(in_path, [_VALID_PAYLOAD])

    result = runner.invoke(
        app,
        ["trace", "--in", str(in_path), "--out-dir", str(tmp_path / "out"), "--tracer", "bogus"],
    )
    assert result.exit_code != 0
    assert "unknown tracer" in result.output or "unknown tracer" in (result.stderr or "")


@pytest.mark.parametrize(
    ("argv", "expected_paths"),
    [([], []), (["--trace-scope", "patch"], ["x"]), (["--trace-scope", "repo"], [])],
)
def test_trace_scope_decides_the_paths_handed_to_the_tracer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str], expected_paths: list[str]
) -> None:
    """Same scope values, same default, and same per-row resolution as the `execute-trace` worker."""
    in_path = tmp_path / "in.jsonl"
    _write_jsonl(in_path, [_VALID_PAYLOAD])

    seen: list[list[str]] = []

    def _fake_trace_one(datapoint, *, trace_paths, **_kwargs):
        seen.append(trace_paths)
        return TraceResult(
            instance_id=datapoint.instance_id,
            tracer="trace",
            run_result=RunResult(exit_code=0, stdout=b"", stderr=b""),
            trace_path=Path("/dev/null"),
        )

    monkeypatch.setattr(cli_mod, "trace_one", _fake_trace_one)

    result = runner.invoke(
        app,
        ["trace", "--in", str(in_path), "--out-dir", str(tmp_path / "out"), "--tracer", "trace", *argv],
    )
    assert result.exit_code == 0, result.output
    assert seen == [expected_paths]


def test_trace_rejects_unknown_trace_scope(tmp_path: Path) -> None:
    in_path = tmp_path / "in.jsonl"
    _write_jsonl(in_path, [_VALID_PAYLOAD])

    result = runner.invoke(
        app,
        ["trace", "--in", str(in_path), "--out-dir", str(tmp_path / "out"), "--trace-scope", "bogus"],
    )
    assert result.exit_code != 0
    assert "unknown --trace-scope" in result.output or "unknown --trace-scope" in (result.stderr or "")


def test_trace_skips_invalid_rows_and_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    in_path = tmp_path / "in.jsonl"
    in_path.write_text(
        "\n".join(
            [
                "not-json",
                json.dumps({"instance_id": "missing-required-fields"}),
                json.dumps(_VALID_PAYLOAD),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    def _fake_trace_one(datapoint, **_kwargs):
        return TraceResult(
            instance_id=datapoint.instance_id,
            tracer="trace",
            run_result=RunResult(exit_code=0, stdout=b"", stderr=b""),
            trace_path=Path("/dev/null"),
        )

    monkeypatch.setattr(cli_mod, "trace_one", _fake_trace_one)

    result = runner.invoke(
        app, ["trace", "--in", str(in_path), "--out-dir", str(tmp_path / "out")]
    )
    # One row succeeded, but two were skipped → exit 1.
    assert result.exit_code == 1


def test_trace_reports_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    in_path = tmp_path / "in.jsonl"
    _write_jsonl(in_path, [_VALID_PAYLOAD])

    def _fake_trace_one(datapoint, **_kwargs):
        return TraceResult(
            instance_id=datapoint.instance_id,
            tracer="trace",
            run_result=RunResult(exit_code=-1, stdout=b"", stderr=b""),
            trace_path=None,
            error="DockerError",
        )

    monkeypatch.setattr(cli_mod, "trace_one", _fake_trace_one)

    result = runner.invoke(
        app, ["trace", "--in", str(in_path), "--out-dir", str(tmp_path / "out")]
    )
    assert result.exit_code == 1


def test_build_samples_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`build-samples` orchestrates: pair rows → locate trace → merge outcomes → fetch sources → build."""
    from sourceworldbench_benchmarks.execution_tracer import cli as cli_mod
    from sourceworldbench_benchmarks.execution_tracer import sources as sources_mod
    from sourceworldbench_benchmarks.execution_tracer.benchmark.samples import (
        BenchmarkSample,
        SampleContext,
    )

    base_row = dict(
        _VALID_PAYLOAD,
        instance_id="demo__demo-1__base",
        patch=None,
        passed_tests=[],
        failed_tests=["tests/t.py::test_a"],
        skipped_tests=[],
        errored_tests=[],
        discovery_errors=[],
    )
    aug_row = dict(
        _VALID_PAYLOAD,
        instance_id="demo__demo-1__ruff",
        passed_tests=["tests/t.py::test_a"],
        failed_tests=[],
        skipped_tests=[],
        errored_tests=[],
        discovery_errors=[],
    )
    in_path = tmp_path / "filled.jsonl"
    _write_jsonl(in_path, [base_row, aug_row])

    traces_dir = tmp_path / "traces"
    for instance_id in (base_row["instance_id"], aug_row["instance_id"]):
        instance_dir = traces_dir / instance_id
        instance_dir.mkdir(parents=True)
        (instance_dir / "trace_output.json").write_text(
            json.dumps(
                {
                    "tracer_version": "0.4.0",
                    "instance_id": instance_id,
                    "tests": {
                        "tests/t.py::test_a": {
                            "functions": [{"file": "src/x.py", "lineno": 1}],
                        }
                    },
                }
            )
        )

    # No actual docker; stub out fetch_sources entirely.
    fetch_calls: list[str] = []

    def _fake_fetch(dp, paths, *, repo_dir, timeout):
        fetch_calls.append(dp.instance_id)
        assert "src/x.py" in set(paths)
        return {"src/x.py": "def f(): return 1\n"}

    monkeypatch.setattr(cli_mod, "fetch_sources", _fake_fetch, raising=False)
    # The CLI does `from sourceworldbench_benchmarks.execution_tracer.sources import fetch_sources` at
    # call time, so patch the module attribute too.
    monkeypatch.setattr(sources_mod, "fetch_sources", _fake_fetch)

    # And stub out the builder: it parses source files, which we don't want
    # to wrestle with in a unit test. Just produce one fake sample per side.
    from sourceworldbench_benchmarks.execution_tracer.benchmark import builder as builder_mod

    def _fake_build(trace_path, snapshot_dir, meta, *, tasks, side, context_strategy):
        sample = BenchmarkSample(
            sample_id=f"{meta['instance_id']}::{side}",
            task="outcome",
            instance_id=meta["instance_id"],
            test_nodeid="tests/t.py::test_a",
            repo=meta["repo"],
            base_commit=meta["base_commit"],
            context=SampleContext(
                test_file_path="tests/t.py",
                test_file_content="",
                source_files={"src/x.py": "def f(): return 1\n"},
            ),
            system_prompt="",
            user_prompt="",
            ground_truth="PASSED",
            metric="exact",
        )
        return [sample]

    monkeypatch.setattr(builder_mod, "build_samples_for_trace", _fake_build)

    out_dir = tmp_path / "samples"
    result = runner.invoke(
        app,
        [
            "build-samples",
            "--in", str(in_path),
            "--traces", str(traces_dir),
            "--out-dir", str(out_dir),
            "--outcomes", "datapoint",
        ],
    )
    assert result.exit_code == 0, result.output
    assert set(fetch_calls) == {base_row["instance_id"], aug_row["instance_id"]}
    manifest = json.loads((out_dir / "manifest.json").read_text())
    assert manifest["total_samples"] == 2
    assert manifest["instances_ok"] == 2
    # Outcome merging happened: each trace JSON now carries the state's own outcome.
    base_trace = json.loads(
        (traces_dir / base_row["instance_id"] / "trace_output.json").read_text()
    )
    assert base_trace["tests"]["tests/t.py::test_a"]["outcome"] == "failed"
    aug_trace = json.loads(
        (traces_dir / aug_row["instance_id"] / "trace_output.json").read_text()
    )
    assert aug_trace["tests"]["tests/t.py::test_a"]["outcome"] == "passed"


def test_build_samples_rejects_unsupported_outcomes(tmp_path: Path) -> None:
    in_path = tmp_path / "filled.jsonl"
    _write_jsonl(in_path, [_VALID_PAYLOAD])

    result = runner.invoke(
        app,
        [
            "build-samples",
            "--in", str(in_path),
            "--traces", str(tmp_path / "traces"),
            "--out-dir", str(tmp_path / "samples"),
            "--outcomes", "swebench-log",
        ],
    )
    assert result.exit_code != 0


def test_adapt_swebench_writes_jsonl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    from sourceworldbench_benchmarks.schema import StateDatapoint

    fake_module = types.ModuleType("sourceworldbench_benchmarks.execution_tracer.adapters.swebench")

    def _fake_adapt(dataset_name, *, split, instance_ids, namespace="swebench"):
        assert dataset_name == "stub/dataset"
        assert split == "test"
        assert instance_ids == ["demo__a", "demo__b"]
        assert namespace == "swebench"
        for iid in instance_ids:
            yield StateDatapoint.model_validate({**_VALID_PAYLOAD, "instance_id": f"{iid}__base"})

    fake_module.adapt_swebench_dataset = _fake_adapt
    monkeypatch.setitem(sys.modules, "sourceworldbench_benchmarks.execution_tracer.adapters.swebench", fake_module)

    out_path = tmp_path / "out.jsonl"
    result = runner.invoke(
        app,
        [
            "adapt-swebench",
            "--dataset",
            "stub/dataset",
            "--split",
            "test",
            "--instance-ids",
            "demo__a,demo__b",
            "--out",
            str(out_path),
        ],
    )
    assert result.exit_code == 0, result.output
    lines = [json.loads(ln) for ln in out_path.read_text().splitlines() if ln]
    assert [r["instance_id"] for r in lines] == ["demo__a__base", "demo__b__base"]
