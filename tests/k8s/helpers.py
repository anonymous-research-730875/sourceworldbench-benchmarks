"""Shared fakes and fixtures data for the Kubernetes test modules."""

import json
import subprocess

from sourceworldbench_benchmarks.k8s import DEFAULT_REMOTE_ROOT, PreparedRow, WorkerUnit
from sourceworldbench_benchmarks.schema import StateDatapoint

RUNNER_IMAGE = "registry.example.com/sourceworldbench/runner@sha256:abc123"
RUN_DIR = f"{DEFAULT_REMOTE_ROOT}/nf-001"


def completed_process(args: list[str], stdout: bytes = b"", stderr: bytes = b"") -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr=stderr)


class RecordingKubectl:
    """Fake kubectl runner that records args and stdin payloads.

    Reports empty Job/Pod lists, so a run reads as finished (no pending work).
    """

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], bytes | None]] = []

    def __call__(self, args: list[str], input_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((args, input_bytes))
        return completed_process(args, b'{"items": []}')

    def applied_manifests(self) -> list[dict]:
        manifests: list[dict] = []
        for args, payload in self.calls:
            if args[-3:] == ["apply", "-f", "-"] and payload is not None:
                manifests.append(json.loads(payload.decode("utf-8")))
        return manifests


def prepared_row(
    partial: StateDatapoint,
    *,
    index: int = 0,
    job_name: str = "sourceworldbench-collect-run-w-abc12345",
    units: tuple[WorkerUnit, ...] | None = None,
) -> PreparedRow:
    if units is None:
        units = (WorkerUnit(job_name=job_name, example_hash="abc123456789", tracer=None),)
    return PreparedRow(
        original_index=index,
        partial=partial,
        payload=partial.model_dump(),
        raw_line=partial.model_dump_json() + "\n",
        needs_cluster=True,
        units=units,
    )
