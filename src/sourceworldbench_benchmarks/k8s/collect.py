"""Download side of `k8s collect`: assemble filled rows into output JSONL.

Submission/status/cancel are shared with trace and live in `run.py`
(`submit_k8s_run` with a `worker="execute"` config, `get_k8s_run_status`,
`cancel_k8s_run`); only the download differs. Each row runs as a single worker
Job whose `result.json` (or `failure.json`) this module gathers back into one
local JSONL file.
"""

import json
from pathlib import Path
from typing import Any

from sourceworldbench_benchmarks.collector import FAILURES_DIR_SUFFIX, CollectSummary
from sourceworldbench_benchmarks.execution import FailureDetails
from sourceworldbench_benchmarks.k8s.client import KubectlClient
from sourceworldbench_benchmarks.k8s.config import K8sRunConfig, K8sRunError, _remote_run_dir, _required_run_name
from sourceworldbench_benchmarks.k8s.remote_store import GcsRunStore
from sourceworldbench_benchmarks.k8s.run import _run_in_progress, _write_local_failure, get_k8s_run_status


def download_k8s_collect(
    *,
    out_path: Path,
    config: K8sRunConfig,
    client: KubectlClient | None = None,
    store: GcsRunStore | None = None,
) -> CollectSummary:
    """Assemble a terminal Kubernetes collect run's output JSONL locally.

    Refuses while any worker Job is still pending (including Kueue-suspended)
    or active. Failed rows are omitted from the output; their failure details
    are written under `<out>.failures/<instance_id>/failure.json` — the
    worker's remote `failure.json` when it wrote one, otherwise a synthesized
    `missing_worker_artifact` failure.
    """
    run_name = _required_run_name(config)
    kubectl = client or KubectlClient()
    run_dir = _remote_run_dir(config, run_name)
    current = get_k8s_run_status(config=config, client=kubectl)
    if _run_in_progress(current):
        raise K8sRunError(f"run {run_name} is not completed yet; check with k8s status --run-name {run_name}")
    run_store = store or GcsRunStore()
    run = run_store.read_json(f"{run_dir}/run.json")
    if run is None or "rows" not in run:
        raise K8sRunError(f"run.json is not available for run {run_name}")
    return _download_worker_outputs(store=run_store, run=run, run_dir=run_dir, out_path=out_path)


def _download_worker_outputs(
    *,
    store: GcsRunStore,
    run: dict[str, Any],
    run_dir: str,
    out_path: Path,
) -> CollectSummary:
    """Triage each row in order: passthrough, result.json, failure.json, synthesized failure.

    Remote paths are composed from `run_dir` (derived from --remote-root), not
    from run.json's `remote_run_dir`, so a run whose `remote_run_dir` points
    elsewhere stays downloadable by passing its remote root as a gs:// prefix.
    """
    input_payload = store.read_bytes(f"{run_dir}/input/rows.jsonl")
    if input_payload is None:
        raise K8sRunError(f"input rows are missing for run {run['run_name']}")
    rows_by_id = _input_rows_by_id(input_payload)
    failures_dir = out_path.with_name(out_path.name + FAILURES_DIR_SUFFIX)
    filled_lines: list[str] = []
    ok = 0
    fail = 0
    for row in run["rows"]:
        instance_id = row["instance_id"]
        if not row["needs_cluster"]:
            filled_lines.append(json.dumps(rows_by_id[instance_id], sort_keys=True) + "\n")
            ok += 1
            continue

        result = store.read_bytes(f"{run_dir}/workers/{instance_id}/result.json", missing_ok=True)
        if result is not None:
            content = result.decode("utf-8")
            filled_lines.append(content if content.endswith("\n") else f"{content}\n")
            ok += 1
            continue

        artifacts_prefix = f"{run_dir}/workers/{instance_id}/artifacts"
        failure = store.read_bytes(f"{artifacts_prefix}/failure.json", missing_ok=True)
        if failure is None:
            failure = _missing_worker_artifact_failure(instance_id)
        _write_local_failure(failures_dir, instance_id, failure)
        failure_dir = failures_dir / instance_id
        try:
            message = json.loads(failure).get("message", "")
        except Exception:
            message = ""
        (failure_dir / "error.txt").write_text(message + "\n", encoding="utf-8")
        stdout = store.read_bytes(f"{artifacts_prefix}/logs/command.stdout", missing_ok=True)
        stderr = store.read_bytes(f"{artifacts_prefix}/logs/command.stderr", missing_ok=True)
        if stdout is not None or stderr is not None:
            logs_dir = failure_dir / "logs"
            logs_dir.mkdir(exist_ok=True)
            if stdout is not None:
                (logs_dir / "command.stdout").write_bytes(stdout)
            if stderr is not None:
                (logs_dir / "command.stderr").write_bytes(stderr)
        fail += 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("".join(filled_lines), encoding="utf-8")
    return CollectSummary(ok=ok, skip=0, fail=fail)


def _missing_worker_artifact_failure(instance_id: str) -> bytes:
    details = FailureDetails(
        category="infrastructure",
        stage="materialize",
        reason="missing_worker_artifact",
        message="worker reached a terminal state without result.json or failure.json",
    )
    return (json.dumps(details.artifact_payload(instance_id), sort_keys=True) + "\n").encode("utf-8")


def _input_rows_by_id(payload: bytes) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for raw in payload.decode("utf-8").splitlines():
        if not raw.strip():
            continue
        row = json.loads(raw)
        rows[str(row["instance_id"])] = row
    return rows
