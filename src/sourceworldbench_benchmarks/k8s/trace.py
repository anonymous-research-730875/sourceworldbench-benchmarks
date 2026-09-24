"""Download side of `k8s trace`: gather each row's per-tracer outputs.

Submission/status/cancel are shared with collect and live in `run.py`
(`submit_k8s_run` with a `worker="trace"` config, `get_k8s_run_status`,
`cancel_k8s_run`); only the download differs. `k8s trace` fans each row out
into one Job per tracer, so each row's outputs live under
`workers/<id>/<tracer>/artifacts/`. Download gathers those files and, when the
row has a `trace_output.json`, runs `merge_trace_outputs` on them — the same
merge local `trace` does in-pod, relocated here so a failed tracer never costs
the row the tracers that succeeded.
"""

import json
import shutil
from pathlib import Path
from typing import Any

from sourceworldbench_benchmarks.collector import FAILURES_DIR_SUFFIX, CollectSummary
from sourceworldbench_benchmarks.execution import FailureDetails
from sourceworldbench_benchmarks.execution_tracer.runner.spec import BY_NAME
from sourceworldbench_benchmarks.execution_tracer.traces import merge_trace_outputs
from sourceworldbench_benchmarks.k8s.client import KubectlClient
from sourceworldbench_benchmarks.k8s.config import K8sRunConfig, K8sRunError, _remote_run_dir, _required_run_name
from sourceworldbench_benchmarks.k8s.remote_store import GcsRunStore
from sourceworldbench_benchmarks.k8s.run import _run_in_progress, _write_local_failure, get_k8s_run_status

MERGE_TARGET = BY_NAME["trace"].output_filename


def download_k8s_trace(
    *,
    out_dir: Path,
    config: K8sRunConfig,
    client: KubectlClient | None = None,
    store: GcsRunStore | None = None,
) -> CollectSummary:
    """Assemble a terminal `k8s trace` run's outputs under `out_dir/<instance_id>/`.

    For each row, gathers every tracer's output. When `trace_output.json` is
    among them the other outputs are merged into it — the same on-disk layout
    local `trace` writes, so a downloaded run is a drop-in input for
    `--push`/`build-samples`. A row with no output from any tracer is recorded
    under `<out_dir>.failures/<instance_id>/`. Refuses while any Job is
    pending/active.
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
    if any("units" not in row for row in run["rows"]):
        raise K8sRunError(
            f"run.json for run {run_name} does not record worker units; "
            f"it was submitted with an incompatible sourceworldbench-benchmarks version — re-submit the run"
        )
    return _download_trace_outputs(store=run_store, run=run, run_dir=run_dir, out_dir=out_dir)


def _download_trace_outputs(
    *,
    store: GcsRunStore,
    run: dict[str, Any],
    run_dir: str,
    out_dir: Path,
) -> CollectSummary:
    failures_dir = out_dir.with_name(out_dir.name + FAILURES_DIR_SUFFIX)
    ok = 0
    fail = 0
    out_dir.mkdir(parents=True, exist_ok=True)
    for row in run["rows"]:
        instance_id = row["instance_id"]
        # Stage into a hidden scratch dir so a failed row never touches
        # pre-existing data at out_dir/<instance_id>.
        staging = out_dir / f".{instance_id}.downloading"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir()
        downloaded: list[str] = []
        for unit in row["units"]:
            spec = BY_NAME[unit["tracer"]]
            artifacts = f"{run_dir}/workers/{instance_id}/{spec.name}/artifacts"
            for name in (spec.output_filename, f"{spec.output_filename}.gz"):
                data = store.read_bytes(f"{artifacts}/{name}", missing_ok=True)
                if data is not None:
                    (staging / name).write_bytes(data)
                    downloaded.append(spec.output_filename)
                    break
        if not downloaded:
            shutil.rmtree(staging, ignore_errors=True)
            _write_local_failure(failures_dir, instance_id, _no_output_failure(instance_id))
            fail += 1
            continue
        if MERGE_TARGET in downloaded:
            merge_trace_outputs(staging)
        dest = out_dir / instance_id
        if dest.exists():
            shutil.rmtree(dest)
        staging.rename(dest)
        ok += 1
    return CollectSummary(ok=ok, skip=0, fail=fail)


def _no_output_failure(instance_id: str) -> bytes:
    details = FailureDetails(
        category="infrastructure",
        stage="materialize",
        reason="missing_worker_artifact",
        message="no tracer unit produced output for this row",
    )
    return (json.dumps(details.artifact_payload(instance_id), sort_keys=True) + "\n").encode("utf-8")
