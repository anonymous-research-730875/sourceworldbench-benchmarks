import shlex
from typing import Any

from sourceworldbench_benchmarks.execution import COMMAND_RESULTS_DIR
from sourceworldbench_benchmarks.k8s.config import (
    APP_LABEL,
    COMPONENT_LABEL,
    EXAMPLE_HASH_LABEL,
    INSTANCE_LABEL,
    RUN_LABEL,
    K8sRunConfig,
    K8sRunError,
    PreparedK8sRun,
    PreparedRow,
    WorkerUnit,
    _sanitize_name,
    _trim_label_value,
)

# Two deadlines with different clocks. The pod deadline starts when the pod runs,
# so it stays tight: command budget plus pull/checkout/upload slack. The Job
# deadline starts at Kueue admission and also counts queue time — a tight value
# would kill admitted-but-unscheduled jobs before they ever run — so it is only
# a generous backstop.
_POD_DEADLINE_SLACK_SECONDS = 2400
_JOB_DEADLINE_SLACK_SECONDS = 43_200

# The worker container's name, referenced by the downward-API entries that read
# its own resource limits back into the pod.
WORKER_CONTAINER_NAME = "execute"


def _worker_args(
    config: K8sRunConfig, *, rows_uri: str, instance_id: str, worker_dir: str, tracer: str | None
) -> list[str]:
    """Build the runner CLI args for one worker Job, per `config.worker`."""
    common = ["--input", rows_uri, "--instance-id", instance_id, "--repo-dir", config.repo_dir]
    timeout = str(config.command_timeout)
    if config.worker == "trace":
        if tracer is None:
            raise K8sRunError("trace worker units must name a tracer")
        args = [
            "execute-trace",
            *common,
            "--tracer",
            tracer,
            "--artifacts-dir",
            f"{worker_dir}/artifacts",
            "--timeout",
            timeout,
            "--trace-scope",
            config.trace_scope,
        ]
        if config.workload_column:
            args.append("--workload")
            if config.workload_column == "command_workload_amplified":
                args.append("--amplified")
        return args
    return [
        "execute",
        *common,
        "--out",
        f"{worker_dir}/result.json",
        "--artifacts-dir",
        f"{worker_dir}/artifacts",
        "--timeout",
        timeout,
    ]


def _pod_deadline_seconds(config: K8sRunConfig) -> int:
    """Bound a running pod: command budget plus pull/checkout/upload slack."""
    return config.command_timeout + _POD_DEADLINE_SLACK_SECONDS


def _job_deadline_seconds(config: K8sRunConfig) -> int:
    """Backstop for the whole Job, generous because its clock also counts queue time."""
    return config.command_timeout + _JOB_DEADLINE_SLACK_SECONDS


def _worker_env(
    *,
    row: PreparedRow,
    unit: WorkerUnit,
    prepared: PreparedK8sRun,
    config: K8sRunConfig,
) -> list[dict[str, Any]]:
    """Build the worker container's env: where it ran, what it ran, and what it was given.

    `trace_worker.host_info` reads these back into every tracer output. The CPU
    limit is in millicores and the memory limit in bytes, both taken from the
    container's own `limits` — a pod under gVisor cannot read either from its
    cgroups, and `os.cpu_count()` reports the sandbox's allotment, not the limit.
    """
    return [
        {"name": "SOURCEWORLDBENCH_NODE_NAME", "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}}},
        {"name": "SOURCEWORLDBENCH_POD_NAME", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}},
        {"name": "SOURCEWORLDBENCH_POD_UID", "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}}},
        {"name": "SOURCEWORLDBENCH_JOB_NAME", "value": unit.job_name},
        {"name": "SOURCEWORLDBENCH_RUN_NAME", "value": prepared.run_name},
        {"name": "SOURCEWORLDBENCH_CONTAINER_IMAGE", "value": row.partial.container},
        {"name": "SOURCEWORLDBENCH_RUNNER_IMAGE", "value": config.runner_image or ""},
        {
            "name": "SOURCEWORLDBENCH_CPU_LIMIT_MILLICORES",
            "valueFrom": {
                "resourceFieldRef": {"containerName": WORKER_CONTAINER_NAME, "resource": "limits.cpu", "divisor": "1m"}
            },
        },
        {
            "name": "SOURCEWORLDBENCH_MEMORY_LIMIT_BYTES",
            "valueFrom": {
                "resourceFieldRef": {
                    "containerName": WORKER_CONTAINER_NAME,
                    "resource": "limits.memory",
                    "divisor": "1",
                }
            },
        },
    ]


def _worker_dir(prepared: PreparedK8sRun, *, instance_id: str, tracer: str | None) -> str:
    base = f"{prepared.run_dir}/workers/{instance_id}"
    return f"{base}/{tracer}" if tracer is not None else base


def render_worker_job_manifest(
    *,
    row: PreparedRow,
    unit: WorkerUnit,
    prepared: PreparedK8sRun,
    config: K8sRunConfig,
) -> dict[str, Any]:
    """Render a single worker Job for one unit (a row, or a row×tracer slice)."""
    runner_image = config.runner_image
    labels = _base_labels(prepared.run_name, "worker")
    labels |= {
        EXAMPLE_HASH_LABEL: unit.example_hash,
        INSTANCE_LABEL: _trim_label_value(_sanitize_name(row.partial.instance_id)),
    }
    # Kueue labels go on the Job only, and an empty queue drops them: the webhook
    # rejects an empty value, while an absent label leaves the Job unmanaged.
    job_labels = labels | (
        {
            "kueue.x-k8s.io/queue-name": config.queue,
            "kueue.x-k8s.io/priority-class": config.priority,
        }
        if config.queue
        else {}
    )
    worker_dir = _worker_dir(prepared, instance_id=row.partial.instance_id, tracer=unit.tracer)
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": unit.job_name,
            "labels": job_labels,
        },
        "spec": {
            "completions": 1,
            "parallelism": 1,
            "backoffLimit": 0,
            "activeDeadlineSeconds": _job_deadline_seconds(config),
            # Finished Jobs self-clean after an hour: row data lives on GCS, so the
            # Job objects are only needed for `k8s status` tallies and quick log access.
            "ttlSecondsAfterFinished": 3600,
            "template": {
                "metadata": {
                    "labels": labels,
                    # Eviction would lose the row outright (`backoffLimit: 0`).
                    "annotations": {"cluster-autoscaler.kubernetes.io/safe-to-evict": "false"},
                },
                "spec": {
                    "restartPolicy": "Never",
                    "activeDeadlineSeconds": _pod_deadline_seconds(config),
                    # No runtime_class means no runtimeClassName field at all: the pod
                    # runs on the node's default runtime instead of a sandbox.
                    **({"runtimeClassName": config.runtime_class} if config.runtime_class else {}),
                    # Pinning a machine shape keeps a run's timings comparable.
                    **({"nodeSelector": dict(config.node_selector)} if config.node_selector else {}),
                    # The environment images run as uid 65532; pin it so images that
                    # set another USER cannot dodge the cluster's non-root policy.
                    "securityContext": {"runAsNonRoot": True, "runAsUser": 65532, "runAsGroup": 65532},
                    "serviceAccountName": config.worker_service_account,
                    "imagePullSecrets": [{"name": config.image_pull_secret}],
                    "initContainers": [
                        {
                            "name": "copy-sourceworldbench-runner",
                            "image": runner_image,
                            "command": [
                                "/bin/sh",
                                "-c",
                                # Plain -r, not -a: non-root workers cannot preserve
                                # ownership/times on the emptyDir mount.
                                (
                                    f"cp -r {shlex.quote(config.runner_source_path.rstrip('/') + '/.')} "
                                    f"{shlex.quote(config.runner_copy_mount_path.rstrip('/') + '/')}"
                                ),
                            ],
                            "volumeMounts": [
                                {"name": "sourceworldbench-runner", "mountPath": config.runner_copy_mount_path}
                            ],
                        }
                    ],
                    "containers": [
                        {
                            "name": WORKER_CONTAINER_NAME,
                            "image": row.partial.container,
                            "command": [config.runner_command],
                            # Placement and provenance for the artifacts, since the pod
                            # objects are deleted an hour after the Job ends. The limits
                            # come from the API server rather than the pod's own view:
                            # under gVisor the cgroup files do not report them.
                            "env": _worker_env(row=row, unit=unit, prepared=prepared, config=config),
                            "args": _worker_args(
                                config,
                                rows_uri=f"{prepared.run_dir}/input/rows.jsonl",
                                instance_id=row.partial.instance_id,
                                worker_dir=worker_dir,
                                tracer=unit.tracer,
                            ),
                            "resources": {
                                "requests": {
                                    "cpu": config.cpu,
                                    "memory": config.memory,
                                    "ephemeral-storage": config.ephemeral_storage,
                                },
                                "limits": {
                                    "cpu": config.cpu,
                                    "memory": config.memory,
                                    "ephemeral-storage": config.ephemeral_storage,
                                },
                            },
                            "volumeMounts": [
                                {"name": "sourceworldbench-runner", "mountPath": config.runner_mount_path},
                                {"name": "work", "mountPath": "/tmp/sourceworldbench-work"},
                                {"name": "command-results", "mountPath": str(COMMAND_RESULTS_DIR)},
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "sourceworldbench-runner", "emptyDir": {}},
                        {"name": "work", "emptyDir": {}},
                        {"name": "command-results", "emptyDir": {}},
                    ],
                },
            },
        },
    }


def _base_labels(run_name: str, component: str) -> dict[str, str]:
    return {
        "app.kubernetes.io/name": APP_LABEL,
        RUN_LABEL: run_name,
        COMPONENT_LABEL: component,
    }
