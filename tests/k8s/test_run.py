import json
import re
import subprocess
from pathlib import Path

import pytest

from sourceworldbench_benchmarks.k8s import (
    DEFAULT_RUNNER_IMAGE,
    GcsRunStore,
    K8sRunConfig,
    K8sRunError,
    KubectlClient,
    WorkerUnit,
    cancel_k8s_run,
    get_k8s_run_status,
    prepare_k8s_run,
    submit_k8s_run,
)
from sourceworldbench_benchmarks.k8s.run import _ensure_unique_worker_job_names
from sourceworldbench_benchmarks.schema import StateDatapoint
from tests.conftest import FakeGcs
from tests.k8s.helpers import RUN_DIR, RUNNER_IMAGE, RecordingKubectl, completed_process, prepared_row


def test_prepare_k8s_run_rejects_duplicate_instance_ids(tmp_path: Path, partial_row, tmp_partial_jsonl) -> None:
    in_path = tmp_partial_jsonl(
        [partial_row("sourceworldbench-toy-repo__rs1__ruff"), partial_row("sourceworldbench-toy-repo__rs1__ruff")]
    )

    with pytest.raises(K8sRunError, match="duplicate instance_id"):
        prepare_k8s_run(in_path=in_path, config=K8sRunConfig(run_name="dup", runner_image=RUNNER_IMAGE))


def test_prepare_k8s_run_rejects_local_images(tmp_path: Path, partial_row, tmp_partial_jsonl) -> None:
    row = partial_row("sourceworldbench-toy-repo__rs1__ruff")
    row["container"] = "sourceworldbench/example:dev"
    in_path = tmp_partial_jsonl([row])

    with pytest.raises(K8sRunError, match="has no registry"):
        prepare_k8s_run(in_path=in_path, config=K8sRunConfig(run_name="local", runner_image=RUNNER_IMAGE))


def test_prepare_k8s_run_accepts_default_runner_image(partial_row, tmp_partial_jsonl) -> None:
    in_path = tmp_partial_jsonl([partial_row("sourceworldbench-toy-repo__rs1__ruff")])

    prepared = prepare_k8s_run(
        in_path=in_path,
        config=K8sRunConfig(run_name="run", validate_parsers=False),
    )

    assert prepared.submitted == 1


def test_prepare_k8s_run_rejects_custom_mutable_runner_image(partial_row, tmp_partial_jsonl) -> None:
    in_path = tmp_partial_jsonl([partial_row("sourceworldbench-toy-repo__rs1__ruff")])

    with pytest.raises(K8sRunError, match="mutable"):
        prepare_k8s_run(
            in_path=in_path,
            config=K8sRunConfig(run_name="run", runner_image="registry.example.com/sourceworldbench/runner:latest"),
        )


def test_prepare_k8s_run_worker_job_names_are_bounded_and_deterministic(partial_row, tmp_partial_jsonl) -> None:
    in_path = tmp_partial_jsonl([partial_row("toy__deadbeef__rowA"), partial_row("toy__deadbeef__rowB")])
    config = K8sRunConfig(
        run_name="sourceworldbench-k8s-5-hf-partial-20260609-abcdef-with-a-very-long-suffix",
        runner_image=RUNNER_IMAGE,
        validate_parsers=False,
    )

    first = prepare_k8s_run(in_path=in_path, config=config)
    second = prepare_k8s_run(in_path=in_path, config=config)

    names = [unit.job_name for row in first.rows for unit in row.units]
    assert names == [unit.job_name for row in second.rows for unit in row.units]
    assert len(set(names)) == len(names)
    for name in names:
        assert len(name) <= 52
        assert re.fullmatch(r"sourceworldbench-collect-.*-w-[0-9a-f]{8}", name)


def test_prepare_k8s_trace_fans_each_row_into_one_job_per_tracer(partial_row, tmp_partial_jsonl) -> None:
    """Trace runs every row (even already-filled), one Job per tracer."""
    complete = partial_row("toy__deadbeef__rowA") | {
        "passed_tests": ["t"],
        "failed_tests": [],
        "skipped_tests": [],
        "errored_tests": [],
        "discovery_errors": [],
    }
    in_path = tmp_partial_jsonl([complete])

    prepared = prepare_k8s_run(
        in_path=in_path,
        config=K8sRunConfig(run_name="run", runner_image=RUNNER_IMAGE, worker="trace", validate_parsers=False),
    )

    row = prepared.rows[0]
    assert row.needs_cluster is True
    tracers = ["cprofile", "memprof", "memrss", "trace", "walltime"]
    assert sorted(unit.tracer for unit in row.units if unit.tracer) == tracers
    assert len({unit.job_name for unit in row.units}) == 5
    assert prepared.submitted == 5


def test_ensure_unique_worker_job_names_rejects_collisions(partial_row) -> None:
    """Truncated-hash collisions cannot be staged through the public API."""
    row_a = prepared_row(
        StateDatapoint.model_validate(partial_row("toy__deadbeef__rowA")),
        index=0,
        job_name="sourceworldbench-collect-x-w-aa",
    )
    row_b = prepared_row(
        StateDatapoint.model_validate(partial_row("toy__deadbeef__rowB")),
        index=1,
        job_name="sourceworldbench-collect-x-w-aa",
    )

    with pytest.raises(K8sRunError, match=r"collides.*'toy__deadbeef__rowA'.*'toy__deadbeef__rowB'"):
        _ensure_unique_worker_job_names([row_a, row_b])


def test_ensure_unique_worker_job_names_rejects_same_row_tracer_collisions(partial_row) -> None:
    """Two tracer units of one row colliding on the truncated hash are rejected too."""
    row = prepared_row(
        StateDatapoint.model_validate(partial_row("toy__deadbeef__rowA")),
        units=(
            WorkerUnit(job_name="sourceworldbench-trace-x-w-aa", example_hash="aa1", tracer="trace"),
            WorkerUnit(job_name="sourceworldbench-trace-x-w-aa", example_hash="aa2", tracer="walltime"),
        ),
    )

    with pytest.raises(K8sRunError, match=r"collides.*'toy__deadbeef__rowA:trace'.*'toy__deadbeef__rowA:walltime'"):
        _ensure_unique_worker_job_names([row])


def test_submit_k8s_run_uploads_run_and_applies_worker_jobs_list(
    tmp_path: Path, partial_row, tmp_partial_jsonl, fake_gcs: FakeGcs
) -> None:
    in_path = tmp_partial_jsonl([partial_row("toy__deadbeef__rowA")])
    runner = RecordingKubectl()
    client = KubectlClient(runner=runner)

    submission = submit_k8s_run(
        in_path=in_path,
        config=K8sRunConfig(run_name="nf-001", runner_image=RUNNER_IMAGE, validate_parsers=False),
        client=client,
        store=GcsRunStore(gcs=fake_gcs),
    )

    assert submission.run_name == "nf-001"
    assert submission.submitted_rows == 1
    dry_run_args, dry_run_payload = runner.calls[0]
    assert dry_run_args == ["kubectl", "-n", "sourceworldbench", "apply", "--dry-run=server", "-f", "-"]
    dry_run_manifest = json.loads(dry_run_payload.decode("utf-8"))
    assert dry_run_manifest["kind"] == "List"

    manifests = runner.applied_manifests()
    jobs_list = manifests[-1]
    assert jobs_list["kind"] == "List"
    assert [item["kind"] for item in jobs_list["items"]] == ["Job"]
    worker = jobs_list["items"][0]
    assert worker["metadata"]["labels"]["sourceworldbench-benchmarks/component"] == "worker"
    assert worker["metadata"]["labels"]["kueue.x-k8s.io/queue-name"] == "default"
    assert worker["metadata"]["labels"]["kueue.x-k8s.io/priority-class"] == "low"
    assert re.fullmatch(r"sourceworldbench-collect-.*-w-[0-9a-f]{8}", worker["metadata"]["name"])

    assert set(fake_gcs.objects) == {f"{RUN_DIR}/run.json", f"{RUN_DIR}/input/rows.jsonl"}
    assert fake_gcs.write_order[0] == f"{RUN_DIR}/run.json"
    run_json = json.loads(fake_gcs.objects[f"{RUN_DIR}/run.json"].decode("utf-8"))
    assert run_json["runner_image"] == RUNNER_IMAGE
    assert run_json["runner_image_digest"] == "sha256:abc123"
    assert run_json["remote_run_dir"] == RUN_DIR
    assert run_json["rows"][0]["units"][0]["job_name"] == worker["metadata"]["name"]
    assert run_json["rows"][0]["units"][0]["tracer"] is None


def test_submit_k8s_run_dry_run_failure_does_not_upload(partial_row, tmp_partial_jsonl, fake_gcs: FakeGcs) -> None:
    in_path = tmp_partial_jsonl([partial_row("toy__deadbeef__rowA")])

    class FailingDryRunKubectl(RecordingKubectl):
        """Fake admission rejecting the worker Jobs before the run upload starts."""

        def __call__(self, args: list[str], input_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
            self.calls.append((args, input_bytes))
            if args == ["kubectl", "-n", "sourceworldbench", "apply", "--dry-run=server", "-f", "-"]:
                return subprocess.CompletedProcess(args=args, returncode=1, stdout=b"", stderr=b"invalid Job")
            return completed_process(args, b'{"items": []}')

    runner = FailingDryRunKubectl()

    with pytest.raises(K8sRunError, match="invalid Job"):
        submit_k8s_run(
            in_path=in_path,
            config=K8sRunConfig(run_name="nf-001", runner_image=RUNNER_IMAGE, validate_parsers=False),
            client=KubectlClient(runner=runner),
            store=GcsRunStore(gcs=fake_gcs),
        )

    assert [args for args, _ in runner.calls] == [
        ["kubectl", "-n", "sourceworldbench", "apply", "--dry-run=server", "-f", "-"]
    ]
    assert fake_gcs.objects == {}


def test_submit_k8s_run_all_passthrough_skips_apply(partial_row, tmp_partial_jsonl, fake_gcs: FakeGcs) -> None:
    row = partial_row("toy__deadbeef__rowA")
    row |= {
        "passed_tests": ["t1"],
        "failed_tests": [],
        "skipped_tests": [],
        "errored_tests": [],
        "discovery_errors": [],
    }
    in_path = tmp_partial_jsonl([row])
    runner = RecordingKubectl()

    submission = submit_k8s_run(
        in_path=in_path,
        config=K8sRunConfig(run_name="nf-001", runner_image=RUNNER_IMAGE, validate_parsers=False),
        client=KubectlClient(runner=runner),
        store=GcsRunStore(gcs=fake_gcs),
    )

    assert submission.submitted_rows == 0
    assert submission.skipped_rows == 1
    assert runner.applied_manifests() == []
    assert not any("--dry-run=server" in args for args, _ in runner.calls)
    assert f"{RUN_DIR}/run.json" in fake_gcs.objects


def test_submit_k8s_run_apply_failure_mentions_orphaned_run_dir(
    partial_row, tmp_partial_jsonl, fake_gcs: FakeGcs
) -> None:
    in_path = tmp_partial_jsonl([partial_row("toy__deadbeef__rowA")])

    class FailingApplyKubectl(RecordingKubectl):
        """Fake a quota or admission failure on the real (non-dry-run) worker Jobs apply."""

        def __call__(self, args: list[str], input_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
            if args == ["kubectl", "-n", "sourceworldbench", "apply", "-f", "-"]:
                self.calls.append((args, input_bytes))
                return subprocess.CompletedProcess(args=args, returncode=1, stdout=b"", stderr=b"quota exceeded")
            return super().__call__(args, input_bytes)

    with pytest.raises(K8sRunError, match=rf"{re.escape(RUN_DIR)}.*k8s cancel"):
        submit_k8s_run(
            in_path=in_path,
            config=K8sRunConfig(run_name="nf-001", runner_image=RUNNER_IMAGE, validate_parsers=False),
            client=KubectlClient(runner=FailingApplyKubectl()),
            store=GcsRunStore(gcs=fake_gcs),
        )


def test_submit_k8s_run_uses_default_runner_image(partial_row, tmp_partial_jsonl, fake_gcs: FakeGcs) -> None:
    in_path = tmp_partial_jsonl([partial_row("toy__deadbeef__rowA")])
    runner = RecordingKubectl()
    client = KubectlClient(runner=runner)

    submission = submit_k8s_run(
        in_path=in_path,
        config=K8sRunConfig(run_name="nf-001", validate_parsers=False),
        client=client,
        store=GcsRunStore(gcs=fake_gcs),
    )

    assert submission.submitted_rows == 1
    manifests = runner.applied_manifests()
    worker = manifests[-1]["items"][0]["spec"]["template"]["spec"]
    assert worker["initContainers"][0]["image"] == DEFAULT_RUNNER_IMAGE


def test_submit_k8s_run_fails_when_remote_run_dir_exists(partial_row, tmp_partial_jsonl) -> None:
    """The run.json upload precondition is the atomic claim on the run name."""
    in_path = tmp_partial_jsonl([partial_row("toy__deadbeef__rowA")])
    fake = FakeGcs({f"{RUN_DIR}/run.json": b"{}"})
    runner = RecordingKubectl()
    client = KubectlClient(runner=runner)

    with pytest.raises(K8sRunError, match="remote run directory already exists"):
        submit_k8s_run(
            in_path=in_path,
            config=K8sRunConfig(run_name="nf-001", runner_image=RUNNER_IMAGE, validate_parsers=False),
            client=client,
            store=GcsRunStore(gcs=fake),
        )

    assert runner.applied_manifests() == []
    assert set(fake.objects) == {f"{RUN_DIR}/run.json"}


def test_get_k8s_run_status_uses_only_kubernetes_resources() -> None:
    runner = RecordingKubectl()
    client = KubectlClient(runner=runner)

    status = get_k8s_run_status(config=K8sRunConfig(run_name="nf-001"), client=client)

    assert status.job_counts["total"] == 0
    assert status.pod_counts["total"] == 0
    commands = [args for args, _ in runner.calls]
    assert commands == [
        [
            "kubectl",
            "-n",
            "sourceworldbench",
            "get",
            "jobs",
            "-l",
            "sourceworldbench-benchmarks/run-name=nf-001",
            "-o",
            "json",
        ],
        [
            "kubectl",
            "-n",
            "sourceworldbench",
            "get",
            "pods",
            "-l",
            "sourceworldbench-benchmarks/run-name=nf-001",
            "-o",
            "json",
        ],
    ]


def test_get_k8s_run_status_counts_jobs_without_pods_as_pending() -> None:
    class StatusKubectl:
        """Fake Kubernetes status for a Job that has not created a pod yet."""

        def __call__(self, args: list[str], input_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
            if args[4] == "jobs":
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

    status = get_k8s_run_status(
        config=K8sRunConfig(run_name="nf-001"),
        client=KubectlClient(runner=StatusKubectl()),
    )

    assert status.job_counts["worker_pending"] == 1
    assert status.job_counts["worker_active"] == 0
    assert status.pod_counts["total"] == 0


def test_cancel_k8s_run_deletes_jobs_only() -> None:
    runner = RecordingKubectl()

    cancel_k8s_run(
        config=K8sRunConfig(run_name="nf-001", runner_image=RUNNER_IMAGE),
        client=KubectlClient(runner=runner),
    )

    assert [args for args, _ in runner.calls] == [
        [
            "kubectl",
            "-n",
            "sourceworldbench",
            "delete",
            "jobs",
            "-l",
            "sourceworldbench-benchmarks/run-name=nf-001",
            "--ignore-not-found=true",
        ]
    ]
