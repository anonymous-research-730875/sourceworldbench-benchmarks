import json
from pathlib import Path
from typing import Any

from sourceworldbench_benchmarks.gcs import GcsError, GcsFiles, GcsPreconditionFailed
from sourceworldbench_benchmarks.k8s.config import K8sRunError

RUN_METADATA_FILE = "run.json"


class GcsRunStore:
    """Access a collect run's remote directory (a gs://bucket/prefix) over the GCS API."""

    def __init__(self, *, gcs: GcsFiles | None = None) -> None:
        self._gcs = gcs or GcsFiles()

    def upload_new_directory(self, source_dir: Path, remote_dir: str) -> None:
        """Upload a local run directory's files to a new remote run directory.

        `run.json` is uploaded first with a no-overwrite precondition: it is the
        atomic claim on the run name, so concurrent submits with the same
        --run-name cannot share a run directory. Raises when the directory (its
        run.json) already exists.
        """
        remote_dir = remote_dir.rstrip("/")
        run_json = source_dir / RUN_METADATA_FILE
        if not run_json.is_file():
            raise K8sRunError(f"run directory {source_dir} is missing {RUN_METADATA_FILE}")
        try:
            self._gcs.write_bytes(f"{remote_dir}/{RUN_METADATA_FILE}", run_json.read_bytes(), if_generation_match=0)
        except GcsPreconditionFailed:
            raise K8sRunError(
                f"remote run directory already exists: {remote_dir}; choose a new --run-name"
            ) from None
        except GcsError as exc:
            raise K8sRunError(f"failed to upload run directory {remote_dir}: {exc}") from exc
        try:
            for path in sorted(p for p in source_dir.rglob("*") if p.is_file() and p != run_json):
                self._gcs.upload_file(path, f"{remote_dir}/{path.relative_to(source_dir)}")
        except GcsError as exc:
            raise K8sRunError(f"failed to upload run directory {remote_dir}: {exc}") from exc

    def read_json(self, remote_path: str) -> dict[str, Any] | None:
        """Read a JSON file from the remote run directory, returning None if absent."""
        raw = self.read_bytes(remote_path, missing_ok=True)
        if raw is None:
            return None
        return json.loads(raw.decode("utf-8"))

    def read_bytes(self, remote_path: str, *, missing_ok: bool = False) -> bytes | None:
        """Read a file from the remote run directory."""
        try:
            return self._gcs.read_bytes(remote_path, missing_ok=missing_ok)
        except GcsError as exc:
            raise K8sRunError(f"failed to read {remote_path}: {exc}") from exc
