import json
from pathlib import Path

import pytest

from sourceworldbench_benchmarks.collector import CollectSummary
from sourceworldbench_benchmarks.k8s import GcsRunStore, K8sRunConfig, K8sRunError, KubectlClient, download_k8s_trace
from tests.conftest import FakeGcs
from tests.k8s.helpers import RUN_DIR, RecordingKubectl

UNITS = [{"tracer": "trace"}, {"tracer": "walltime"}]


def test_download_k8s_trace_merges_per_tracer_outputs_and_routes_rows_without_output(tmp_path: Path) -> None:
    run = {
        "run_name": "nf-001",
        "rows": [{"instance_id": "toy__ok", "units": UNITS}, {"instance_id": "toy__missing", "units": UNITS}],
    }
    fake = FakeGcs(
        {
            f"{RUN_DIR}/run.json": json.dumps(run).encode("utf-8"),
            f"{RUN_DIR}/workers/toy__ok/trace/artifacts/trace_output.json": b'{"tests": {}}',
            f"{RUN_DIR}/workers/toy__ok/walltime/artifacts/walltime_output.json": b'{"tests": {}}',
        }
    )
    out_dir = tmp_path / "out"

    summary = download_k8s_trace(
        out_dir=out_dir,
        config=K8sRunConfig(run_name="nf-001"),
        client=KubectlClient(runner=RecordingKubectl()),
        store=GcsRunStore(gcs=fake),
    )

    assert summary == CollectSummary(ok=1, skip=0, fail=1)
    assert (out_dir / "toy__ok" / "trace_output.json").exists()
    assert (out_dir / "toy__ok" / "walltime_output.json").exists()
    assert not (out_dir / "toy__missing").exists()
    assert (out_dir.with_name("out.failures") / "toy__missing" / "failure.json").exists()


def test_download_k8s_trace_keeps_rows_of_a_single_tracer_run(tmp_path: Path) -> None:
    units = [{"tracer": "memprof"}]
    run = {
        "run_name": "nf-001",
        "rows": [{"instance_id": "toy__ok", "units": units}, {"instance_id": "toy__missing", "units": units}],
    }
    fake = FakeGcs(
        {
            f"{RUN_DIR}/run.json": json.dumps(run).encode("utf-8"),
            f"{RUN_DIR}/workers/toy__ok/memprof/artifacts/memprof_output.json": b'{"tests": {}}',
        }
    )
    out_dir = tmp_path / "out"

    summary = download_k8s_trace(
        out_dir=out_dir,
        config=K8sRunConfig(run_name="nf-001"),
        client=KubectlClient(runner=RecordingKubectl()),
        store=GcsRunStore(gcs=fake),
    )

    assert summary == CollectSummary(ok=1, skip=0, fail=1)
    assert (out_dir / "toy__ok" / "memprof_output.json").exists()
    assert (out_dir.with_name("out.failures") / "toy__missing" / "failure.json").exists()


def test_download_k8s_trace_preserves_preexisting_local_data_when_row_fails(tmp_path: Path) -> None:
    """A row that produced no trace output must not clobber an earlier download of that instance."""
    run = {"run_name": "nf-001", "rows": [{"instance_id": "toy__missing", "units": UNITS}]}
    fake = FakeGcs({f"{RUN_DIR}/run.json": json.dumps(run).encode("utf-8")})
    out_dir = tmp_path / "out"
    earlier = out_dir / "toy__missing" / "trace_output.json"
    earlier.parent.mkdir(parents=True)
    earlier.write_text('{"tests": {}}', encoding="utf-8")

    summary = download_k8s_trace(
        out_dir=out_dir,
        config=K8sRunConfig(run_name="nf-001"),
        client=KubectlClient(runner=RecordingKubectl()),
        store=GcsRunStore(gcs=fake),
    )

    assert summary == CollectSummary(ok=0, skip=0, fail=1)
    assert earlier.read_text(encoding="utf-8") == '{"tests": {}}'


def test_download_k8s_trace_rejects_run_json_without_units(tmp_path: Path) -> None:
    run = {"run_name": "nf-001", "rows": [{"instance_id": "toy__ok"}]}
    fake = FakeGcs({f"{RUN_DIR}/run.json": json.dumps(run).encode("utf-8")})

    with pytest.raises(K8sRunError, match="does not record worker units"):
        download_k8s_trace(
            out_dir=tmp_path / "out",
            config=K8sRunConfig(run_name="nf-001"),
            client=KubectlClient(runner=RecordingKubectl()),
            store=GcsRunStore(gcs=fake),
        )
