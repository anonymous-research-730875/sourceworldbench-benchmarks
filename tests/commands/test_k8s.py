import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sourceworldbench_benchmarks.commands import k8s as k8s_commands
from sourceworldbench_benchmarks.image_builder import DEFAULT_PUSH_PLATFORM
from sourceworldbench_benchmarks.k8s import DEFAULT_REMOTE_ROOT, DEFAULT_RUNNER_IMAGE, K8sRunConfig, K8sRunSubmission
from tests.k8s.helpers import RUNNER_IMAGE


@pytest.fixture
def collect_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Run `k8s collect` with build/submit faked, returning (result, build_calls, submitted_configs)."""
    in_path = tmp_path / "in.jsonl"
    in_path.write_text("", encoding="utf-8")
    build_calls: list[dict] = []
    submitted_configs: list[K8sRunConfig] = []

    def fake_build(**kwargs) -> str:
        build_calls.append(kwargs)
        return "registry.example.com/sourceworldbench/runner@sha256:def456"

    def fake_submit(*, in_path: Path, config: K8sRunConfig, client=None, store=None) -> K8sRunSubmission:
        submitted_configs.append(config)
        return K8sRunSubmission(
            run_name="run-1",
            namespace="sourceworldbench",
            run_dir=f"{DEFAULT_REMOTE_ROOT}/run-1",
            submitted_rows=1,
            skipped_rows=0,
        )

    monkeypatch.setattr(k8s_commands, "build_k8s_runner_image", fake_build)
    monkeypatch.setattr(k8s_commands, "submit_k8s_run", fake_submit)

    def invoke(*extra_args: str):
        result = CliRunner().invoke(k8s_commands.app, ["collect", "--in", str(in_path), *extra_args])
        return result, build_calls, submitted_configs

    return invoke


def test_collect_build_pushes_then_submits_digest_pinned_image(collect_cli) -> None:
    result, build_calls, submitted_configs = collect_cli(
        "--build", "--runner-image", "registry.example.com/sourceworldbench/runner:dev"
    )

    assert result.exit_code == 0
    (build_call,) = build_calls
    assert build_call["push"] is True
    assert build_call["image"] == "registry.example.com/sourceworldbench/runner:dev"
    assert build_call["platform"] == DEFAULT_PUSH_PLATFORM
    assert build_call.get("capture_output", False) is False
    assert submitted_configs[0].runner_image == "registry.example.com/sourceworldbench/runner@sha256:def456"


def test_collect_does_not_build_by_default(collect_cli) -> None:
    result, build_calls, submitted_configs = collect_cli()

    assert result.exit_code == 0
    assert build_calls == []
    assert submitted_configs[0].runner_image == DEFAULT_RUNNER_IMAGE


def test_collect_build_rejects_digest_pinned_runner_image(collect_cli) -> None:
    result, build_calls, submitted_configs = collect_cli("--build", "--runner-image", RUNNER_IMAGE)

    assert result.exit_code == 2
    assert build_calls == []
    assert submitted_configs == []


@pytest.fixture
def trace_cli(tmp_path: Path, partial_row, tmp_partial_jsonl, monkeypatch: pytest.MonkeyPatch):
    """Run `k8s trace` with submit faked, capturing the instance_ids submit actually receives.

    The input holds three rows; each invocation returns (result, submitted_ids), where
    `submitted_ids` is the ordered list of instance_ids in the JSONL passed to submit.
    """
    in_path = tmp_partial_jsonl(
        [
            partial_row("toy__deadbeef__rowA"),
            partial_row("toy__deadbeef__rowB"),
            partial_row("toy__deadbeef__rowC"),
        ]
    )
    submitted_ids: list[list[str]] = []

    def fake_submit(*, in_path: Path, config: K8sRunConfig, client=None, store=None) -> K8sRunSubmission:
        rows = [json.loads(line) for line in in_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        submitted_ids.append([row["instance_id"] for row in rows])
        return K8sRunSubmission(
            run_name="run-1",
            namespace="sourceworldbench",
            run_dir=f"{DEFAULT_REMOTE_ROOT}/run-1",
            submitted_rows=1,
            skipped_rows=0,
        )

    monkeypatch.setattr(k8s_commands, "submit_k8s_run", fake_submit)

    def invoke(*extra_args: str):
        result = CliRunner().invoke(
            k8s_commands.app,
            ["trace", "--in", str(in_path), "--runner-image", RUNNER_IMAGE, *extra_args],
        )
        return result, submitted_ids

    return invoke


def test_trace_without_instance_id_submits_all_rows(trace_cli) -> None:
    result, submitted_ids = trace_cli()

    assert result.exit_code == 0
    assert submitted_ids == [["toy__deadbeef__rowA", "toy__deadbeef__rowB", "toy__deadbeef__rowC"]]


def test_trace_instance_id_filters_to_named_subset_in_order(trace_cli) -> None:
    result, submitted_ids = trace_cli("--instance-id", "toy__deadbeef__rowC", "--instance-id", "toy__deadbeef__rowA")

    assert result.exit_code == 0
    assert submitted_ids == [["toy__deadbeef__rowC", "toy__deadbeef__rowA"]]


def test_trace_unknown_instance_id_exits_without_submitting(trace_cli) -> None:
    result, submitted_ids = trace_cli("--instance-id", "toy__nope")

    assert result.exit_code == 2
    assert "not found" in result.output
    assert submitted_ids == []
