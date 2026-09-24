"""Shared pytest fixtures for sourceworldbench_benchmarks tests."""

import json
from pathlib import Path

import pytest


def _valid_partial_row(instance_id: str) -> dict:
    """A partial StateDatapoint dict with test fields left as None."""
    return {
        "instance_id": instance_id,
        "repo": "anonymous-research-730875/sourceworldbench-toy-repo",
        "base_commit": "a" * 40,
        "patch": "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n",
        "container": "xxx-docker.pkg.dev/xxx/ml/sourceworldbench/example:dev",
        "command": "true",
        "test_scope": ["tests/"],
    }


@pytest.fixture
def partial_row():
    """Factory returning a partial StateDatapoint dict."""
    return _valid_partial_row


@pytest.fixture
def tmp_partial_jsonl(tmp_path: Path):
    """Factory: writes a list of partial rows to a JSONL file and returns the path."""

    def _make(rows: list[dict], name: str = "partial.jsonl") -> Path:
        path = tmp_path / name
        with path.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
        return path

    return _make


class FakeGcs:
    """In-memory stand-in for `sourceworldbench_benchmarks.gcs.GcsFiles`.

    Stores objects in a dict keyed by gs:// URI and records write order so
    tests can assert completion-marker files upload last.
    """

    def __init__(self, objects: dict[str, bytes] | None = None) -> None:
        self.objects: dict[str, bytes] = dict(objects or {})
        self.write_order: list[str] = []

    def exists(self, uri: str) -> bool:
        return uri in self.objects

    def read_bytes(self, uri: str, *, missing_ok: bool = False) -> bytes | None:
        from sourceworldbench_benchmarks.gcs import GcsError

        if uri not in self.objects:
            if missing_ok:
                return None
            raise GcsError(f"object does not exist: {uri}")
        return self.objects[uri]

    def write_bytes(self, uri: str, data: bytes, *, if_generation_match: int | None = None) -> None:
        from sourceworldbench_benchmarks.gcs import GcsPreconditionFailed

        if if_generation_match == 0 and uri in self.objects:
            raise GcsPreconditionFailed(f"object already exists: {uri}")
        self.objects[uri] = data
        self.write_order.append(uri)

    def upload_file(self, local_path: Path, uri: str) -> None:
        self.write_bytes(uri, local_path.read_bytes())

    def download_file(self, uri: str, local_path: Path) -> None:
        data = self.read_bytes(uri)
        assert data is not None
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(data)


@pytest.fixture
def fake_gcs() -> FakeGcs:
    """An empty in-memory GCS fake."""
    return FakeGcs()
