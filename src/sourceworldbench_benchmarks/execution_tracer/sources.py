"""On-demand source extraction for benchmark builds.

The legacy harness wrote a `repo_snapshot/` directory at trace time. The
refactored pipeline defers that: trace JSONs carry repo-relative file
paths, and this module fetches the corresponding source files from the
container at benchmark-build time. One `docker run` per state batches
all requested files via a `cp` loop into a bind-mounted output
directory.
"""

from __future__ import annotations

import logging
import shlex
import shutil
import tempfile
from collections.abc import Iterable
from pathlib import Path

from sourceworldbench_benchmarks.docker_runner import DockerError, run_state
from sourceworldbench_benchmarks.execution import wrap_command
from sourceworldbench_benchmarks.schema import StateDatapoint

logger = logging.getLogger(__name__)

CONTAINER_OUT_DIR = "/sourceworldbench-source-out"
_PATHS_FILE_NAME = "_paths.txt"


def _build_extract_command(repo_dir: str) -> str:
    """Shell snippet that copies every path in `_paths.txt` out to the mount.

    Runs *after* `wrap_command` has applied the state's patch. `cd`s to
    `repo_dir`, then for each repo-relative path: if the file exists,
    mirror its directory tree into `CONTAINER_OUT_DIR` and copy.
    """
    return (
        f"cd {shlex.quote(repo_dir)} && "
        f'while IFS= read -r p; do '
        f'[ -z "$p" ] && continue; '
        f'if [ -f "$p" ]; then '
        f'mkdir -p "{CONTAINER_OUT_DIR}/$(dirname "$p")"; '
        f'cp "$p" "{CONTAINER_OUT_DIR}/$p"; '
        f'fi; '
        f"done < {CONTAINER_OUT_DIR}/{_PATHS_FILE_NAME}"
    )


def fetch_sources(
    datapoint: StateDatapoint,
    paths: Iterable[str],
    *,
    repo_dir: str,
    timeout: int = 600,
) -> dict[str, str]:
    """Spin up `datapoint.container`, apply the state's patch, return file contents.

    `paths` are repo-relative (the same shape the tracer writes into
    trace JSON via `_rel_path`). Missing files are silently omitted —
    the benchmark builder already filters paths it can't resolve (stdlib
    or third-party files end up here too).
    """
    deduped = sorted({p for p in paths if p})
    if not deduped:
        return {}

    with tempfile.TemporaryDirectory(prefix="sourceworldbench_sources_") as tmp:
        tmp_dir = Path(tmp)
        out_dir = tmp_dir / "out"
        out_dir.mkdir()
        results_dir = tmp_dir / "results"
        results_dir.mkdir()

        (out_dir / _PATHS_FILE_NAME).write_text(
            "\n".join(deduped) + "\n", encoding="utf-8"
        )

        patch_path: Path | None = None
        if datapoint.patch is not None:
            patch_path = tmp_dir / "patch.diff"
            patch_path.write_text(datapoint.patch, encoding="utf-8")

        extract = _build_extract_command(repo_dir)
        script = wrap_command(extract, has_patch=datapoint.patch is not None)

        try:
            run_state(
                image=datapoint.container,
                script=script,
                host_results=results_dir,
                patch_path=patch_path,
                timeout=timeout,
                extra_mounts=[(out_dir, CONTAINER_OUT_DIR, False)],
            )
        except DockerError as exc:
            logger.warning(
                "fetch_sources: %s docker error: %s",
                datapoint.instance_id, exc,
            )
            return {}

        results: dict[str, str] = {}
        for rel in deduped:
            host_file = out_dir / rel
            if host_file.is_file():
                try:
                    results[rel] = host_file.read_text(
                        encoding="utf-8", errors="replace"
                    )
                except OSError as exc:
                    logger.warning(
                        "fetch_sources: %s could not read %s: %s",
                        datapoint.instance_id, rel, exc,
                    )
        return results


def materialize_to_dir(sources: dict[str, str], dest: Path) -> None:
    """Write `{rel_path: content}` into `dest/` preserving directory layout.

    Bridge for `build_samples_for_trace`, which still consumes a
    snapshot directory (`benchmark/builder.py:_read_repo_file`). Writes
    each file using UTF-8; pre-existing files in `dest` are overwritten.
    """
    dest.mkdir(parents=True, exist_ok=True)
    for rel, content in sources.items():
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def clear_dir(path: Path) -> None:
    """Remove a directory tree if it exists. Convenience for the CLI's per-row tempdirs."""
    if path.exists():
        shutil.rmtree(path)
