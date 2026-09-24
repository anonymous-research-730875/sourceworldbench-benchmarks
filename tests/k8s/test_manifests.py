import subprocess
import tempfile
from pathlib import Path

import pytest

from sourceworldbench_benchmarks.k8s import (
    DEFAULT_REMOTE_ROOT,
    K8sRunConfig,
    PreparedK8sRun,
    PreparedRow,
    WorkerUnit,
    render_worker_job_manifest,
)
from sourceworldbench_benchmarks.k8s.manifests import _JOB_DEADLINE_SLACK_SECONDS, _POD_DEADLINE_SLACK_SECONDS
from sourceworldbench_benchmarks.schema import StateDatapoint
from tests.k8s.helpers import RUNNER_IMAGE, prepared_row


def _trace_row(partial: StateDatapoint, tracer: str) -> PreparedRow:
    return PreparedRow(
        original_index=0,
        partial=partial,
        payload=partial.model_dump(),
        raw_line=partial.model_dump_json() + "\n",
        units=(
            WorkerUnit(
                job_name=f"sourceworldbench-collect-run-w-{tracer[:8]}", example_hash=f"{tracer}hash", tracer=tracer
            ),
        ),
        needs_cluster=True,
    )


def test_render_worker_job_manifest_uses_benchmark_image_and_runner_injection(partial_row) -> None:
    partial = StateDatapoint.model_validate(partial_row("toy__deadbeef__rowA"))
    row = prepared_row(partial)
    prepared = PreparedK8sRun(
        run_name="run",
        run_dir=f"{DEFAULT_REMOTE_ROOT}/run",
        rows=[row],
        skipped=0,
    )

    manifest = render_worker_job_manifest(
        row=row,
        unit=row.units[0],
        prepared=prepared,
        config=K8sRunConfig(run_name="run", runner_image=RUNNER_IMAGE),
    )

    spec = manifest["spec"]["template"]["spec"]
    assert spec["initContainers"][0]["image"] == RUNNER_IMAGE
    assert spec["initContainers"][0]["command"][-1] == "cp -r /opt/sourceworldbench-runner/. /runner-copy/"
    assert spec["initContainers"][0]["volumeMounts"] == [
        {"name": "sourceworldbench-runner", "mountPath": "/runner-copy"}
    ]
    assert spec["containers"][0]["image"] == partial.container
    assert spec["containers"][0]["command"] == ["/opt/sourceworldbench-runner/bin/sourceworldbench-benchmarks"]
    assert spec["containers"][0]["args"] == [
        "execute",
        "--input",
        f"{DEFAULT_REMOTE_ROOT}/run/input/rows.jsonl",
        "--instance-id",
        partial.instance_id,
        "--repo-dir",
        "/app",
        "--out",
        f"{DEFAULT_REMOTE_ROOT}/run/workers/toy__deadbeef__rowA/result.json",
        "--artifacts-dir",
        f"{DEFAULT_REMOTE_ROOT}/run/workers/toy__deadbeef__rowA/artifacts",
        "--timeout",
        "7200",
    ]
    assert {"name": "sourceworldbench-runner", "mountPath": "/opt/sourceworldbench-runner"} in spec["containers"][0][
        "volumeMounts"
    ]
    assert {"name": "command-results", "mountPath": "/results"} in spec["containers"][0]["volumeMounts"]
    assert spec["containers"][0]["env"] == [
        {"name": "SOURCEWORLDBENCH_NODE_NAME", "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}}},
        {"name": "SOURCEWORLDBENCH_POD_NAME", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}},
        {"name": "SOURCEWORLDBENCH_POD_UID", "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}}},
        {"name": "SOURCEWORLDBENCH_JOB_NAME", "value": row.units[0].job_name},
        {"name": "SOURCEWORLDBENCH_RUN_NAME", "value": "run"},
        {"name": "SOURCEWORLDBENCH_CONTAINER_IMAGE", "value": partial.container},
        {"name": "SOURCEWORLDBENCH_RUNNER_IMAGE", "value": RUNNER_IMAGE},
        {
            "name": "SOURCEWORLDBENCH_CPU_LIMIT_MILLICORES",
            "valueFrom": {"resourceFieldRef": {"containerName": "execute", "resource": "limits.cpu", "divisor": "1m"}},
        },
        {
            "name": "SOURCEWORLDBENCH_MEMORY_LIMIT_BYTES",
            "valueFrom": {
                "resourceFieldRef": {"containerName": "execute", "resource": "limits.memory", "divisor": "1"}
            },
        },
    ]
    assert spec["runtimeClassName"] == "gvisor"
    assert "nodeSelector" not in spec
    assert spec["securityContext"] == {"runAsNonRoot": True, "runAsUser": 65532, "runAsGroup": 65532}
    assert manifest["metadata"]["labels"]["kueue.x-k8s.io/queue-name"] == "default"
    pod_labels = manifest["spec"]["template"]["metadata"]["labels"]
    assert "kueue.x-k8s.io/queue-name" not in pod_labels
    assert "kueue.x-k8s.io/priority-class" not in pod_labels


def test_render_worker_job_manifest_omits_kueue_labels_for_empty_queue(partial_row) -> None:
    """An empty queue drops both Kueue labels: the webhook rejects an empty value."""
    partial = StateDatapoint.model_validate(partial_row("toy__deadbeef__rowA"))
    row = prepared_row(partial)
    prepared = PreparedK8sRun(
        run_name="run",
        run_dir=f"{DEFAULT_REMOTE_ROOT}/run",
        rows=[row],
        skipped=0,
    )

    manifest = render_worker_job_manifest(
        row=row,
        unit=row.units[0],
        prepared=prepared,
        config=K8sRunConfig(run_name="run", runner_image=RUNNER_IMAGE, queue=""),
    )

    job_labels = manifest["metadata"]["labels"]
    assert "kueue.x-k8s.io/queue-name" not in job_labels
    assert "kueue.x-k8s.io/priority-class" not in job_labels
    # The run's own labels still identify the Job for status/cancel/download.
    assert job_labels["sourceworldbench-benchmarks/run-name"] == "run"


def test_render_worker_job_manifest_pins_nodes_when_node_selector_is_set(partial_row) -> None:
    """A node selector reaches the pod spec."""
    partial = StateDatapoint.model_validate(partial_row("toy__deadbeef__rowA"))
    row = prepared_row(partial)
    prepared = PreparedK8sRun(run_name="run", run_dir=f"{DEFAULT_REMOTE_ROOT}/run", rows=[row], skipped=0)

    manifest = render_worker_job_manifest(
        row=row,
        unit=row.units[0],
        prepared=prepared,
        config=K8sRunConfig(
            run_name="run",
            runner_image=RUNNER_IMAGE,
            node_selector=(("node.kubernetes.io/instance-type", "n2-standard-16"),),
        ),
    )

    assert manifest["spec"]["template"]["spec"]["nodeSelector"] == {
        "node.kubernetes.io/instance-type": "n2-standard-16"
    }


def test_render_worker_job_manifest_uses_emptydir_and_gcs_service_account(partial_row) -> None:
    """Workers reach GCS over the API: volumes are all emptyDir and the pod runs under
    the GCS service account."""
    partial = StateDatapoint.model_validate(partial_row("toy__deadbeef__rowA"))
    row = prepared_row(partial)
    prepared = PreparedK8sRun(
        run_name="run",
        run_dir=f"{DEFAULT_REMOTE_ROOT}/run",
        rows=[row],
        skipped=0,
    )

    manifest = render_worker_job_manifest(
        row=row,
        unit=row.units[0],
        prepared=prepared,
        config=K8sRunConfig(run_name="run", runner_image=RUNNER_IMAGE),
    )

    spec = manifest["spec"]["template"]["spec"]
    assert spec["volumes"] == [
        {"name": "sourceworldbench-runner", "emptyDir": {}},
        {"name": "work", "emptyDir": {}},
        {"name": "command-results", "emptyDir": {}},
    ]
    annotations = manifest["spec"]["template"]["metadata"]["annotations"]
    assert annotations == {"cluster-autoscaler.kubernetes.io/safe-to-evict": "false"}
    assert spec["serviceAccountName"] == "gke-cloud-storage"


def test_render_worker_job_manifest_omits_runtime_class_when_unset(partial_row) -> None:
    """`runtime_class=None` must drop the field entirely (no sandbox), not render null."""
    partial = StateDatapoint.model_validate(partial_row("toy__deadbeef__rowA"))
    row = prepared_row(partial)
    prepared = PreparedK8sRun(
        run_name="run",
        run_dir=f"{DEFAULT_REMOTE_ROOT}/run",
        rows=[row],
        skipped=0,
    )

    manifest = render_worker_job_manifest(
        row=row,
        unit=row.units[0],
        prepared=prepared,
        config=K8sRunConfig(run_name="run", runner_image=RUNNER_IMAGE, runtime_class=None),
    )

    spec = manifest["spec"]["template"]["spec"]
    assert "runtimeClassName" not in spec
    assert spec["securityContext"] == {"runAsNonRoot": True, "runAsUser": 65532, "runAsGroup": 65532}


def test_runner_copy_strategy_preserves_absolute_runner_path(partial_row) -> None:
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="sourceworldbench-runner-") as root:
        root_path = Path(root)
        source = root_path / "source"
        copy_mount = root_path / "copy"
        runtime_mount = root_path / "runtime"
        (source / "bin").mkdir(parents=True)
        (runtime_mount / "bin").mkdir(parents=True)
        python_shim = runtime_mount / "bin" / "python-shim"
        python_shim.symlink_to("/bin/sh")
        runner_script = source / "bin" / "sourceworldbench-benchmarks"
        runner_script.write_text(
            f'#!{python_shim}\necho runner-started "$@"\n',
            encoding="utf-8",
        )
        runner_script.chmod(0o755)
        partial = StateDatapoint.model_validate(partial_row("toy__deadbeef__rowA"))
        row = prepared_row(partial)
        prepared = PreparedK8sRun(
            run_name="run",
            run_dir=f"{DEFAULT_REMOTE_ROOT}/run",
            rows=[row],
            skipped=0,
        )
        config = K8sRunConfig(
            run_name="run",
            runner_image=RUNNER_IMAGE,
            runner_source_path=str(source),
            runner_copy_mount_path=str(copy_mount),
            runner_mount_path=str(runtime_mount),
            runner_command=str(runtime_mount / "bin" / "sourceworldbench-benchmarks"),
        )
        manifest = render_worker_job_manifest(row=row, unit=row.units[0], prepared=prepared, config=config)
        copy_command = manifest["spec"]["template"]["spec"]["initContainers"][0]["command"][-1]

        subprocess.run(["/bin/sh", "-c", copy_command], check=True)
        subprocess.run(["cp", "-a", f"{copy_mount}/.", str(runtime_mount)], check=True)
        proc = subprocess.run(
            [str(runtime_mount / "bin" / "sourceworldbench-benchmarks"), "execute", "--help"],
            capture_output=True,
            text=True,
            check=True,
        )

        assert "runner-started execute --help" in proc.stdout


@pytest.mark.parametrize("tracer", ["trace", "memprof", "memrss", "walltime", "cprofile"])
def test_render_trace_unit_args_timeout_and_deadline(partial_row, tracer: str) -> None:
    partial = StateDatapoint.model_validate(partial_row("toy__deadbeef__rowA"))
    row = _trace_row(partial, tracer)
    prepared = PreparedK8sRun(run_name="run", run_dir=f"{DEFAULT_REMOTE_ROOT}/run", rows=[row], skipped=0)
    config = K8sRunConfig(run_name="run", runner_image=RUNNER_IMAGE, command_timeout=1000, worker="trace")

    manifest = render_worker_job_manifest(row=row, unit=row.units[0], prepared=prepared, config=config)

    args = manifest["spec"]["template"]["spec"]["containers"][0]["args"]
    assert args == [
        "execute-trace",
        "--input",
        f"{DEFAULT_REMOTE_ROOT}/run/input/rows.jsonl",
        "--instance-id",
        "toy__deadbeef__rowA",
        "--repo-dir",
        "/app",
        "--tracer",
        tracer,
        "--artifacts-dir",
        f"{DEFAULT_REMOTE_ROOT}/run/workers/toy__deadbeef__rowA/{tracer}/artifacts",
        "--timeout",
        "1000",
        "--trace-scope",
        "repo",
    ]
    assert "--out" not in args
    assert manifest["spec"]["activeDeadlineSeconds"] == 1000 + _JOB_DEADLINE_SLACK_SECONDS
    assert manifest["spec"]["template"]["spec"]["activeDeadlineSeconds"] == 1000 + _POD_DEADLINE_SLACK_SECONDS


def test_render_trace_unit_args_carry_patch_trace_scope(partial_row) -> None:
    partial = StateDatapoint.model_validate(partial_row("toy__deadbeef__rowA"))
    row = _trace_row(partial, "cprofile")
    prepared = PreparedK8sRun(run_name="run", run_dir=f"{DEFAULT_REMOTE_ROOT}/run", rows=[row], skipped=0)
    config = K8sRunConfig(run_name="run", runner_image=RUNNER_IMAGE, worker="trace", trace_scope="patch")

    manifest = render_worker_job_manifest(row=row, unit=row.units[0], prepared=prepared, config=config)

    args = manifest["spec"]["template"]["spec"]["containers"][0]["args"]
    assert args[args.index("--trace-scope") + 1] == "patch"
