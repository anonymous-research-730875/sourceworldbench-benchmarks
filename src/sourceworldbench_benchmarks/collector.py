import logging
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from sourceworldbench_benchmarks.execution import (
    DockerExecutionBackend,
    RowExecutionFailure,
    execute_row,
    write_failure_artifact,
)
from sourceworldbench_benchmarks.schema import StateDatapoint

logger = logging.getLogger(__name__)

# Used by k8s/collect.py to locate failure artifacts alongside its output file.
FAILURES_DIR_SUFFIX = ".failures"

CollectProgressState = Literal[
    "running_tests",
    "completed",
    "failed",
    "skipped",
]


@dataclass(frozen=True)
class CollectProgressEvent:
    """Point-in-time progress update for one row being collected."""

    state: CollectProgressState
    instance_id: str
    message: str | None = None


CollectProgressCallback = Callable[[CollectProgressEvent], None]


class CollectSummary(BaseModel):
    ok: int = 0
    skip: int = 0
    fail: int = 0


def _distribution_message(row: dict) -> str:
    """One-line `passed=… failed=…` summary from a filled row's outcome fields."""
    return (
        f"passed={len(row.get('passed_tests', []))} failed={len(row.get('failed_tests', []))} "
        f"skipped={len(row.get('skipped_tests', []))} error={len(row.get('errored_tests', []))} "
        f"discovery_errors={len(row.get('discovery_errors', []))}"
    )


def _emit_progress(
    progress: CollectProgressCallback | None,
    *,
    state: CollectProgressState,
    instance_id: str,
    message: str | None = None,
) -> None:
    if progress is not None:
        progress(CollectProgressEvent(state=state, instance_id=instance_id, message=message))


def fill_state(
    state: StateDatapoint,
    *,
    timeout: int,
    progress: CollectProgressCallback | None = None,
) -> StateDatapoint:
    """Run one state through Docker using the shared execution materializer."""
    _emit_progress(progress, state="running_tests", instance_id=state.instance_id)
    with tempfile.TemporaryDirectory(prefix="sourceworldbench_row_") as tmp:
        return execute_row(
            partial=state,
            backend=DockerExecutionBackend(),
            artifacts_dir=Path(tmp),
            timeout=timeout,
        )


def collect_states(
    states: list[StateDatapoint],
    *,
    timeout: int,
    workers: int = 1,
    failures_dir: Path,
    progress: CollectProgressCallback | None = None,
    on_filled: Callable[[StateDatapoint], None] | None = None,
) -> CollectSummary:
    """Drive `fill_state` across `states`.

    Calls `on_filled(filled_row)` after each successful execution so the caller
    can persist results incrementally. Failures are written under `failures_dir`.
    """
    if workers != 1:
        raise NotImplementedError("--workers > 1 is not implemented")

    failures_dir.mkdir(parents=True, exist_ok=True)
    ok = skip = fail = 0

    for state in states:
        try:
            filled = fill_state(state, timeout=timeout, progress=progress)
        except RowExecutionFailure as exc:
            write_failure_artifact(
                failures_dir / state.instance_id / "failure.json",
                instance_id=state.instance_id,
                failure=exc,
                raw_line=state.model_dump_json(),
            )
            flagged = state.model_copy(
                update={"metadata": {**state.metadata, "collection_error": True}},
            )
            if on_filled is not None:
                on_filled(flagged)
            logger.info("fail instance=%s reason=%s", state.instance_id, exc.reason)
            _emit_progress(progress, state="failed", instance_id=state.instance_id, message=exc.reason)
            fail += 1
            continue

        if on_filled is not None:
            on_filled(filled)
        logger.info(
            "ok instance=%s passed=%d failed=%d skipped=%d error=%d discovery_errors=%d",
            state.instance_id,
            len(filled.passed_tests or []),
            len(filled.failed_tests or []),
            len(filled.skipped_tests or []),
            len(filled.errored_tests or []),
            len(filled.discovery_errors or []),
        )
        _emit_progress(
            progress,
            state="completed",
            instance_id=state.instance_id,
            message=_distribution_message(filled.model_dump()),
        )
        ok += 1

    return CollectSummary(ok=ok, skip=skip, fail=fail)
