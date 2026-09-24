import json
import subprocess
from pathlib import Path

import pytest

from sourceworldbench_benchmarks.collector import CollectSummary
from sourceworldbench_benchmarks.k8s import GcsRunStore, K8sRunConfig, K8sRunError, KubectlClient, download_k8s_collect
from tests.conftest import FakeGcs
from tests.k8s.helpers import RUN_DIR, RUNNER_IMAGE, RecordingKubectl, completed_process


def test_download_k8s_collect_refuses_while_worker_jobs_pending(tmp_path: Path, fake_gcs: FakeGcs) -> None:
    class PendingJobsKubectl(RecordingKubectl):
        """Fake a Kueue-suspended worker Job: no conditions, no active pods."""

        def __call__(self, args: list[str], input_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
            self.calls.append((args, input_bytes))
            if args[:4] == ["kubectl", "-n", "sourceworldbench", "get"] and args[4] == "jobs":
                payload = {
                    "items": [
                        {
                            "metadata": {"labels": {"sourceworldbench-benchmarks/component": "worker"}},
                            "status": {},
                        }
                    ]
                }
                return completed_process(args, json.dumps(payload).encode("utf-8"))
            return completed_process(args, b'{"items": []}')

    runner = PendingJobsKubectl()

    with pytest.raises(K8sRunError, match="not completed"):
        download_k8s_collect(
            out_path=tmp_path / "out.jsonl",
            config=K8sRunConfig(run_name="nf-001", runner_image=RUNNER_IMAGE),
            client=KubectlClient(runner=runner),
            store=GcsRunStore(gcs=fake_gcs),
        )

    assert not any("apply" in args for args, _ in runner.calls)


def test_download_k8s_collect_assembles_worker_outputs(tmp_path: Path) -> None:
    input_row = {
        "instance_id": "toy__deadbeef__rowA",
        "container": "registry.example.com/sourceworldbench/row@sha256:def456",
    }
    result_row = {"instance_id": "toy__deadbeef__rowA", "result": "ok"}
    run = {
        "run_name": "nf-001",
        "remote_run_dir": RUN_DIR,
        "rows": [{"instance_id": "toy__deadbeef__rowA", "needs_cluster": True}],
    }
    fake = FakeGcs(
        {
            f"{RUN_DIR}/run.json": json.dumps(run).encode("utf-8"),
            f"{RUN_DIR}/input/rows.jsonl": (json.dumps(input_row) + "\n").encode("utf-8"),
            f"{RUN_DIR}/workers/toy__deadbeef__rowA/result.json": (json.dumps(result_row) + "\n").encode("utf-8"),
        }
    )

    out_path = tmp_path / "out.jsonl"
    summary = download_k8s_collect(
        out_path=out_path,
        config=K8sRunConfig(run_name="nf-001", runner_image=RUNNER_IMAGE),
        client=KubectlClient(runner=RecordingKubectl()),
        store=GcsRunStore(gcs=fake),
    )

    assert summary == CollectSummary(ok=1, skip=0, fail=0)
    assert out_path.read_text(encoding="utf-8") == json.dumps(result_row) + "\n"
    assert not (tmp_path / "out.jsonl.failures").exists()


def test_download_k8s_collect_locates_workers_via_remote_root_not_run_json(tmp_path: Path) -> None:
    """When run.json's `remote_run_dir` points somewhere other than where the files
    live, worker outputs are still located by composing paths from --remote-root."""
    result_row = {"instance_id": "toy__deadbeef__rowA", "result": "ok"}
    run = {
        "run_name": "nf-001",
        "remote_run_dir": "/mnt/experiments/sourceworldbench/collect-runs/nf-001",
        "rows": [{"instance_id": "toy__deadbeef__rowA", "needs_cluster": True}],
    }
    fake = FakeGcs(
        {
            f"{RUN_DIR}/run.json": json.dumps(run).encode("utf-8"),
            f"{RUN_DIR}/input/rows.jsonl": b'{"instance_id": "toy__deadbeef__rowA"}\n',
            f"{RUN_DIR}/workers/toy__deadbeef__rowA/result.json": (json.dumps(result_row) + "\n").encode("utf-8"),
        }
    )

    out_path = tmp_path / "out.jsonl"
    summary = download_k8s_collect(
        out_path=out_path,
        config=K8sRunConfig(run_name="nf-001", runner_image=RUNNER_IMAGE),
        client=KubectlClient(runner=RecordingKubectl()),
        store=GcsRunStore(gcs=fake),
    )

    assert summary == CollectSummary(ok=1, skip=0, fail=0)
    assert out_path.read_text(encoding="utf-8") == json.dumps(result_row) + "\n"


def test_download_k8s_collect_writes_failure_artifacts(tmp_path: Path) -> None:
    remote_failure = (
        json.dumps(
            {
                "instance_id": "toy__deadbeef__rowA",
                "category": "row",
                "stage": "patch",
                "reason": "patch_apply",
                "message": "patch failed",
            }
        )
        + "\n"
    ).encode("utf-8")
    run = {
        "run_name": "nf-001",
        "remote_run_dir": RUN_DIR,
        "rows": [
            {"instance_id": "toy__deadbeef__rowA", "needs_cluster": True},
            {"instance_id": "toy__deadbeef__rowB", "needs_cluster": True},
        ],
    }
    fake = FakeGcs(
        {
            f"{RUN_DIR}/run.json": json.dumps(run).encode("utf-8"),
            f"{RUN_DIR}/input/rows.jsonl": (
                b'{"instance_id": "toy__deadbeef__rowA"}\n{"instance_id": "toy__deadbeef__rowB"}\n'
            ),
            f"{RUN_DIR}/workers/toy__deadbeef__rowA/artifacts/failure.json": remote_failure,
        }
    )

    out_path = tmp_path / "out.jsonl"
    summary = download_k8s_collect(
        out_path=out_path,
        config=K8sRunConfig(run_name="nf-001", runner_image=RUNNER_IMAGE),
        client=KubectlClient(runner=RecordingKubectl()),
        store=GcsRunStore(gcs=fake),
    )

    assert summary == CollectSummary(ok=0, skip=0, fail=2)
    assert out_path.read_text(encoding="utf-8") == ""
    failures_dir = tmp_path / "out.jsonl.failures"
    assert (failures_dir / "toy__deadbeef__rowA" / "failure.json").read_bytes() == remote_failure
    synthesized = json.loads((failures_dir / "toy__deadbeef__rowB" / "failure.json").read_text(encoding="utf-8"))
    assert synthesized["reason"] == "missing_worker_artifact"
    assert synthesized["category"] == "infrastructure"
    assert synthesized["instance_id"] == "toy__deadbeef__rowB"


def test_download_k8s_collect_handles_all_passthrough_run(tmp_path: Path) -> None:
    input_row = {"instance_id": "toy__deadbeef__rowA", "passed_tests": ["t1"]}
    run = {
        "run_name": "nf-001",
        "remote_run_dir": RUN_DIR,
        "rows": [{"instance_id": "toy__deadbeef__rowA", "needs_cluster": False}],
    }
    fake = FakeGcs(
        {
            f"{RUN_DIR}/run.json": json.dumps(run).encode("utf-8"),
            f"{RUN_DIR}/input/rows.jsonl": (json.dumps(input_row) + "\n").encode("utf-8"),
        }
    )

    out_path = tmp_path / "out.jsonl"
    summary = download_k8s_collect(
        out_path=out_path,
        config=K8sRunConfig(run_name="nf-001", runner_image=RUNNER_IMAGE),
        client=KubectlClient(runner=RecordingKubectl()),
        store=GcsRunStore(gcs=fake),
    )

    assert summary == CollectSummary(ok=1, skip=0, fail=0)
    assert json.loads(out_path.read_text(encoding="utf-8")) == input_row
    assert not (tmp_path / "out.jsonl.failures").exists()
