from pathlib import Path
from typing import Any


class GcsError(Exception):
    """A GCS operation failed (auth, network, or API error)."""


class GcsPreconditionFailed(GcsError):
    """A conditional write failed because the object already exists."""


def is_gs_uri(value: str) -> bool:
    """Return True for `gs://bucket/key` style URIs."""
    return value.startswith("gs://")


def split_gs_uri(uri: str) -> tuple[str, str]:
    """Split `gs://bucket/key` into `(bucket, key)`."""
    if not is_gs_uri(uri):
        raise GcsError(f"not a gs:// URI: {uri}")
    bucket, _, key = uri.removeprefix("gs://").partition("/")
    if not bucket or not key:
        raise GcsError(f"gs:// URI must name a bucket and an object: {uri}")
    return bucket, key


class GcsFiles:
    """Blob-level GCS IO behind gs:// URIs.

    The only module talking to `google.cloud.storage`; everything else (run
    store, worker staging, tests) sees gs:// URIs and the local `GcsError`
    types. The client library is imported lazily so local-path code paths
    never pay for it.
    """

    def __init__(self, client: Any | None = None) -> None:
        if client is None:
            from google.cloud import storage

            try:
                client = storage.Client()
            except Exception as exc:
                raise GcsError(f"cannot create GCS client (no usable credentials?): {exc}") from exc
        self._client = client

    def _blob(self, uri: str) -> Any:
        bucket, key = split_gs_uri(uri)
        return self._client.bucket(bucket).blob(key)

    def exists(self, uri: str) -> bool:
        """Return True when the object exists."""
        return self._run(uri, lambda: self._blob(uri).exists())

    def read_bytes(self, uri: str, *, missing_ok: bool = False) -> bytes | None:
        """Download an object's content; None when absent and `missing_ok`."""
        from google.api_core.exceptions import NotFound

        try:
            return self._blob(uri).download_as_bytes()
        except NotFound:
            if missing_ok:
                return None
            raise GcsError(f"object does not exist: {uri}") from None
        except Exception as exc:
            raise GcsError(f"GCS read failed for {uri}: {exc}") from exc

    def write_bytes(self, uri: str, data: bytes, *, if_generation_match: int | None = None) -> None:
        """Upload bytes; `if_generation_match=0` fails when the object exists."""
        from google.api_core.exceptions import PreconditionFailed

        try:
            self._blob(uri).upload_from_string(data, if_generation_match=if_generation_match)
        except PreconditionFailed as exc:
            raise GcsPreconditionFailed(f"object already exists: {uri}") from exc
        except Exception as exc:
            raise GcsError(f"GCS write failed for {uri}: {exc}") from exc

    def upload_file(self, local_path: Path, uri: str) -> None:
        """Upload one local file to a gs:// URI."""
        self._run(uri, lambda: self._blob(uri).upload_from_filename(str(local_path)))

    def download_file(self, uri: str, local_path: Path) -> None:
        """Download one object to a local path, creating parent directories."""
        data = self.read_bytes(uri)
        assert data is not None
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(data)

    def _run(self, uri: str, operation: Any) -> Any:
        try:
            return operation()
        except GcsError:
            raise
        except Exception as exc:
            raise GcsError(f"GCS operation failed for {uri}: {exc}") from exc
