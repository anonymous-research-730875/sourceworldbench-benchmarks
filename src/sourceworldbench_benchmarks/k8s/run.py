"""Shared submission lifecycle for Kubernetes runs (collect and trace).

`prepare`/`submit`/`status`/`cancel` are worker-agnostic: the `worker` field on
`K8sRunConfig` decides whether a row becomes one Job (collect) or one Job per
tracer (trace). The two workers differ only in how their outputs are downloaded,
which lives in `collect.py` and `trace.py`.
"""

import json
import tempfile
from dataclasses import asdict
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from sourceworldbench_benchmarks.dataset_io import _row_from_hub
from sourceworldbench_benchmarks.execution import is_result_complete
from sourceworldbench_benchmarks.execution_tracer.runner.spec import BY_NAME
from sourceworldbench_benchmarks.k8s.client import KubectlClient
from sourceworldbench_benchmarks.k8s.config import (
    COMPONENT_LABEL,
    DEFAULT_RUNNER_IMAGE,
    K8sRunConfig,
    K8sRunError,
    K8sRunStatus,
    K8sRunSubmission,
    PreparedK8sRun,
    PreparedRow,
    WorkerUnit,
    _job_state,
    _remote_run_dir,
    _replace_config,
    _required_or_default_run_name,
    _required_run_name,
    _run_label_selector,
    _stable_hash,
    _utc_now,
    _worker_job_name,
    _write_json_file,
)
from sourceworldbench_benchmarks.k8s.manifests import render_worker_job_manifest
from sourceworldbench_benchmarks.k8s.remote_store import GcsRunStore
from sourceworldbench_benchmarks.parsers import get_parser
from sourceworldbench_benchmarks.schema import StateDatapoint


def prepare_k8s_run(*, in_path: Path, config: K8sRunConfig) -> PreparedK8sRun:
    """Validate input rows and create row/job metadata for a Kubernetes run."""
    run_name = _required_or_default_run_name(config, in_path)
    run_dir = _remote_run_dir(config, run_name)
    seen: set[str] = set()
    rows: list[PreparedRow] = []
    skipped = 0

    with in_path.open("r", encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, start=1):
            if not raw.strip():
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise K8sRunError(f"{in_path}:{line_number}: invalid JSON: {exc}") from exc
            payload = _row_from_hub(payload)
            try:
                partial = StateDatapoint.model_validate(payload)
            except ValidationError as exc:
                raise K8sRunError(f"{in_path}:{line_number}: invalid StateDatapoint: {exc}") from exc
            if partial.instance_id in seen:
                raise K8sRunError(f"{in_path}:{line_number}: duplicate instance_id {partial.instance_id!r}")
            seen.add(partial.instance_id)
            if config.validate_parsers:
                try:
                    get_parser(partial)
                except KeyError as exc:
                    raise K8sRunError(f"{in_path}:{line_number}: {exc}") from exc
            _validate_cluster_image(partial.container, allow_local=config.allow_local_images, field="container")
            # Trace runs every row (even already-filled ones); collect skips rows
            # whose test-outcome fields are already populated.
            needs_cluster = config.worker == "trace" or not is_result_complete(payload)
            if not needs_cluster:
                skipped += 1
            rows.append(
                PreparedRow(
                    original_index=line_number - 1,
                    partial=partial,
                    payload=payload,
                    raw_line=raw if raw.endswith("\n") else f"{raw}\n",
                    needs_cluster=needs_cluster,
                    units=_row_units(run_name, line_number, partial.instance_id, needs_cluster, config),
                )
            )

    if not rows:
        raise K8sRunError(f"{in_path}: no input rows")
    _ensure_unique_worker_job_names(rows)
    _validate_runner_image(config.runner_image, allow_local=config.allow_local_images)
    return PreparedK8sRun(run_name=run_name, run_dir=run_dir, rows=rows, skipped=skipped)


def _row_units(
    run_name: str, line_number: int, instance_id: str, needs_cluster: bool, config: K8sRunConfig
) -> tuple[WorkerUnit, ...]:
    """Build the worker Jobs for one row: none if passthrough, one per tracer for trace, else one."""
    if not needs_cluster:
        return ()
    if config.worker == "trace":
        names = config.tracers if config.tracers else tuple(BY_NAME)
        tracers: tuple[str | None, ...] = tuple(names)
    else:
        tracers = (None,)
    units = []
    for tracer in tracers:
        seed = f"{line_number}:{instance_id}" if tracer is None else f"{line_number}:{instance_id}:{tracer}"
        example_hash = _stable_hash(seed)
        units.append(
            WorkerUnit(job_name=_worker_job_name(run_name, example_hash), example_hash=example_hash, tracer=tracer)
        )
    return tuple(units)


def _ensure_unique_worker_job_names(rows: list[PreparedRow]) -> None:
    """Reject truncated-hash collisions before they become a confusing apply error."""
    by_job_name: dict[str, str] = {}
    for row in rows:
        for unit in row.units:
            instance_id = row.partial.instance_id
            label = instance_id if unit.tracer is None else f"{instance_id}:{unit.tracer}"
            other = by_job_name.setdefault(unit.job_name, label)
            if other != label:
                raise K8sRunError(
                    f"worker job name {unit.job_name!r} collides for "
                    f"{other!r} and {label!r}; re-order the input rows to resolve"
                )


def submit_k8s_run(
    *,
    in_path: Path,
    config: K8sRunConfig,
    client: KubectlClient | None = None,
    store: GcsRunStore | None = None,
) -> K8sRunSubmission:
    """Submit a detached Kubernetes run and return follow-up metadata.

    Order matters: the worker Jobs are server-side dry-run validated first, then
    the run directory is uploaded, then the Jobs are applied for real. If that
    final apply fails, the uploaded run directory is orphaned and some Jobs may
    exist; the raised error explains how to recover.
    """
    kubectl = client or KubectlClient()
    prepared = prepare_k8s_run(in_path=in_path, config=config)
    resolved_config = _replace_config(config, run_name=prepared.run_name)
    jobs_list = _render_worker_jobs_list(prepared=prepared, config=resolved_config)
    if jobs_list is not None:
        kubectl.apply(jobs_list, namespace=resolved_config.namespace, dry_run_server=True)

    run_store = store or GcsRunStore()
    with tempfile.TemporaryDirectory(prefix="sourceworldbench_k8s_run_") as tmp:
        local_run_dir = _write_local_run_dir(Path(tmp), prepared, resolved_config)
        run_store.upload_new_directory(local_run_dir, prepared.run_dir)

    if jobs_list is not None:
        try:
            kubectl.apply(jobs_list, namespace=resolved_config.namespace)
        except K8sRunError as exc:
            raise K8sRunError(
                f"{exc}; the run directory {prepared.run_dir} was already uploaded and some worker Jobs "
                f"may have been created: run `k8s cancel --run-name {prepared.run_name}` and re-submit "
                f"with a new --run-name"
            ) from exc
    return K8sRunSubmission(
        run_name=prepared.run_name,
        namespace=resolved_config.namespace,
        run_dir=prepared.run_dir,
        submitted_rows=prepared.submitted,
        skipped_rows=prepared.skipped,
    )


def _render_worker_jobs_list(*, prepared: PreparedK8sRun, config: K8sRunConfig) -> dict[str, Any] | None:
    """Render one Kueue-labeled worker Job per unit, or None for all-passthrough runs."""
    items = [
        render_worker_job_manifest(row=row, unit=unit, prepared=prepared, config=config)
        for row in prepared.rows
        for unit in row.units
    ]
    if not items:
        return None
    return {"apiVersion": "v1", "kind": "List", "items": items}


def get_k8s_run_status(*, config: K8sRunConfig, client: KubectlClient | None = None) -> K8sRunStatus:
    """Return Kubernetes Job and Pod counts for a submitted run."""
    run_name = _required_run_name(config)
    kubectl = client or KubectlClient()
    selector = _run_label_selector(run_name)
    job_payload = kubectl.get_json(["kubectl", "-n", config.namespace, "get", "jobs", "-l", selector, "-o", "json"])
    pod_payload = kubectl.get_json(["kubectl", "-n", config.namespace, "get", "pods", "-l", selector, "-o", "json"])
    return K8sRunStatus(
        run_name=run_name,
        namespace=config.namespace,
        job_counts=_count_jobs(job_payload),
        pod_counts=_count_pods(pod_payload),
    )


def cancel_k8s_run(*, config: K8sRunConfig, client: KubectlClient | None = None) -> None:
    """Cancel a Kubernetes run by deleting its worker Jobs (pods cascade).

    A later download returns partial results: finished rows are assembled,
    unfinished rows are reported as `missing_worker_artifact` failures.
    """
    run_name = _required_run_name(config)
    kubectl = client or KubectlClient()
    kubectl.delete_selector("jobs", namespace=config.namespace, selector=_run_label_selector(run_name))


def _run_in_progress(status: K8sRunStatus) -> bool:
    return status.job_counts.get("worker_pending", 0) > 0 or status.job_counts.get("worker_active", 0) > 0


def _write_local_failure(failures_dir: Path, instance_id: str, failure: bytes) -> None:
    failure_path = failures_dir / instance_id / "failure.json"
    failure_path.parent.mkdir(parents=True, exist_ok=True)
    failure_path.write_bytes(failure)


def _write_local_run_dir(tmp_dir: Path, prepared: PreparedK8sRun, config: K8sRunConfig) -> Path:
    local = tmp_dir / "run"
    (local / "input").mkdir(parents=True)
    (local / "input" / "rows.jsonl").write_text("".join(row.raw_line for row in prepared.rows), encoding="utf-8")
    _write_json_file(local / "run.json", _run_metadata(prepared, config))
    return local


def _run_metadata(prepared: PreparedK8sRun, config: K8sRunConfig) -> dict[str, Any]:
    """Build the `run.json` payload.

    `run_name`, `remote_run_dir`, and `rows` are the download contract; the
    remaining keys are provenance only.
    """
    return {
        "schema_version": 1,
        "run_name": prepared.run_name,
        "namespace": config.namespace,
        "submitted_at": _utc_now(),
        "input_row_count": prepared.total,
        "runner_image": config.runner_image,
        "runner_image_digest": _image_digest(config.runner_image),
        "runner_source_path": config.runner_source_path,
        "runner_mount_path": config.runner_mount_path,
        "runner_copy_mount_path": config.runner_copy_mount_path,
        "runner_command": config.runner_command,
        "command_timeout": config.command_timeout,
        "queue": config.queue,
        "priority": config.priority,
        "resources": {
            "requests": {"cpu": config.cpu, "memory": config.memory, "ephemeral-storage": config.ephemeral_storage},
            "limits": {"cpu": config.cpu, "memory": config.memory, "ephemeral-storage": config.ephemeral_storage},
        },
        "remote_root": config.remote_root,
        "remote_run_dir": prepared.run_dir,
        "sourceworldbench_version": _package_version(),
        "config": asdict(config),
        "rows": [
            {
                "instance_id": row.partial.instance_id,
                "input_index": row.original_index,
                "needs_cluster": row.needs_cluster,
                "units": [
                    {"job_name": unit.job_name, "example_hash": unit.example_hash, "tracer": unit.tracer}
                    for unit in row.units
                ],
            }
            for row in prepared.rows
        ],
    }


def _count_jobs(payload: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {
        "total": 0,
        "worker_pending": 0,
        "worker_active": 0,
        "worker_succeeded": 0,
        "worker_failed": 0,
    }
    for item in payload.get("items", []):
        counts["total"] += 1
        component = item.get("metadata", {}).get("labels", {}).get(COMPONENT_LABEL, "unknown")
        state = _job_state(item)
        key = f"{component}_{state}"
        counts[key] = counts.get(key, 0) + 1
    return counts


def _count_pods(payload: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {"total": 0}
    for item in payload.get("items", []):
        counts["total"] += 1
        phase = str(item.get("status", {}).get("phase", "Unknown")).lower()
        counts[phase] = counts.get(phase, 0) + 1
    return counts


def _validate_cluster_image(ref: str, *, allow_local: bool, field: str) -> None:
    if allow_local:
        return
    if "/" not in ref:
        raise K8sRunError(f"{field} image {ref!r} has no registry; use a cluster-pullable image reference")
    registry = ref.split("/", 1)[0]
    if registry == "localhost" or registry.startswith("localhost:"):
        raise K8sRunError(f"{field} image {ref!r} is local; pass --allow-local-images only for smoke tests")
    if "." not in registry and ":" not in registry:
        raise K8sRunError(f"{field} image {ref!r} has no registry; use a cluster-pullable image reference")


def _validate_runner_image(ref: str | None, *, allow_local: bool) -> None:
    """Reject mutable runner-image tags so `run.json` provenance stays digest-pinned.

    The default image tag and `allow_local_images` runs are exempt.
    """
    if ref is None:
        raise K8sRunError("--runner-image is required for Kubernetes runs")
    _validate_cluster_image(ref, allow_local=allow_local, field="runner-image")
    if not allow_local and "@sha256:" not in ref and ref != DEFAULT_RUNNER_IMAGE:
        raise K8sRunError(
            f"runner-image {ref!r} is mutable; use a digest-pinned reference or pass --allow-local-images"
        )


def _image_digest(ref: str | None) -> str | None:
    if ref is None:
        return None
    if "@sha256:" not in ref:
        return None
    return ref.rsplit("@", 1)[1]


def _package_version() -> str | None:
    try:
        return version("sourceworldbench-benchmarks")
    except PackageNotFoundError:
        return None
