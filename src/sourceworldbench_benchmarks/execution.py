import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, ValidationError

from sourceworldbench_benchmarks.docker_runner import (
    CONTAINER_PATCH_PATH,
    DockerError,
    DockerTimeout,
    PatchApplyFailed,
)
from sourceworldbench_benchmarks.docker_runner import (
    run_state as run_docker_state,
)
from sourceworldbench_benchmarks.gcs import GcsFiles
from sourceworldbench_benchmarks.parsers import get_parser
from sourceworldbench_benchmarks.report import (
    ConflictingReportError,
    EmptyReportError,
    MalformedReportError,
    MissingReportError,
    ParsedReport,
    TestStatus,
)
from sourceworldbench_benchmarks.schema import STATE_OUTCOME_FIELDS, StateDatapoint

logger = logging.getLogger(__name__)

# Contract: benchmark commands write raw reports to /results. The execution
# layer copies those files into results_dir before passing results_dir to parsers.
# Kubernetes worker pods mount an emptyDir at exactly this path (k8s/manifests.py
# imports this constant).
COMMAND_RESULTS_DIR = Path("/results")

# Optional row-level metadata key holding a shell command to run in the checkout
# after the augmentation patch is applied and before the test command runs, so
# patched Cython/C/Fortran replaces the extensions baked into the container.
# Set and run unconditionally: only the repo's build system can tell whether a
# patch needs recompiling, so it is invoked every time and left to decide.
REBUILD_COMMAND_METADATA_KEY = "rebuild_command"

# pytest exit codes whose report is complete: 0 (all passed) and 1 (tests failed
# — legitimate results). Anything else (interrupted, INTERNALERROR, signalled)
# means the session died early and the report is a truncated prefix of the run.
_COMPLETE_SESSION_EXIT_CODES = frozenset({0, 1})


class ExecutionError(Exception):
    """Failure while executing one benchmark row."""

    def __init__(self, message: str, *, stdout: bytes = b"", stderr: bytes = b"") -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr


class ExecutionTimeout(ExecutionError):
    """A benchmark command exceeded its timeout."""


class PatchApplyFailure(ExecutionError):
    """A patch failed to apply before the benchmark command ran."""


class PatchRestoreFailure(ExecutionError):
    """The files a patch touches could not be restored to the row's base_commit."""


class RebuildFailure(ExecutionError):
    """The post-patch rebuild command failed."""


class AbortedSessionError(ExecutionError):
    """The test command did not finish its session, so its report is incomplete."""


class CheckoutResetFailure(ExecutionError):
    """`git reset --hard` on the benchmark checkout failed."""


class CheckoutVerificationFailure(ExecutionError):
    """The benchmark checkout could not be verified against the row's base_commit."""


class InputSelectionError(ExecutionError):
    """The input JSONL did not select exactly one valid row by instance_id."""


class ParserUnavailableError(ExecutionError):
    """The requested parser does not exist."""


class CommandResult(BaseModel):
    """Captured process result for one benchmark command.

    `exit_code == -1` means the command never produced an exit code; the
    result then only carries stdout/stderr for failure logs.

    `skipped_patch_paths` is non-empty only when the patch went through
    `_apply_patch`'s per-path fallback and some entries were skipped as
    untracked; the row then ran against a *partially* applied patch, so the
    paths are recorded in the run artifacts rather than only logged.

    `elapsed_seconds` times the command alone. `rebuild_seconds` times the
    post-patch rebuild, and is None when the row declared none or the backend
    runs it inside the container and cannot time it apart.
    """

    model_config = ConfigDict(frozen=True)

    exit_code: int
    stdout: bytes = b""
    stderr: bytes = b""
    elapsed_seconds: float
    rebuild_seconds: float | None = None
    skipped_patch_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class FailureDetails:
    """The persisted `failure.json` fields for one failed row.

    `category` says who should act: "row" means the row itself cannot produce
    results and re-running will not help, "runner" means the harness failed,
    and "infrastructure" is cluster-level — only synthesized by `k8s download`.
    """

    category: Literal["row", "runner", "infrastructure"]
    stage: Literal["select", "verify", "reset", "patch", "rebuild", "command", "parse", "materialize", "write"]
    reason: str
    message: str

    def artifact_payload(self, instance_id: str) -> dict[str, Any]:
        """Return the payload persisted as `failure.json`."""
        return {
            "instance_id": instance_id,
            "category": self.category,
            "stage": self.stage,
            "reason": self.reason,
            "message": self.message,
        }

    @classmethod
    def classify(cls, exc: Exception) -> Self:
        """Classify a row execution exception into stable artifact fields."""
        if isinstance(exc, PatchApplyFailure):
            return cls(category="row", stage="patch", reason="patch_apply", message=str(exc))
        if isinstance(exc, PatchRestoreFailure):
            return cls(category="row", stage="patch", reason="patch_restore", message=str(exc))
        if isinstance(exc, RebuildFailure):
            return cls(category="row", stage="rebuild", reason="rebuild", message=str(exc))
        if isinstance(exc, ExecutionTimeout):
            return cls(category="row", stage="command", reason="timeout", message=str(exc))
        if isinstance(exc, AbortedSessionError):
            return cls(category="row", stage="command", reason="aborted_session", message=str(exc))
        if isinstance(exc, MissingReportError):
            return cls(category="row", stage="parse", reason="missing_report", message=str(exc))
        if isinstance(exc, MalformedReportError):
            return cls(category="row", stage="parse", reason="malformed_report", message=str(exc))
        if isinstance(exc, EmptyReportError):
            return cls(category="row", stage="parse", reason="empty_report", message=str(exc))
        if isinstance(exc, ConflictingReportError):
            return cls(category="row", stage="parse", reason="conflicting_report", message=str(exc))
        if isinstance(exc, InputSelectionError):
            return cls(category="runner", stage="select", reason="selection", message=str(exc))
        if isinstance(exc, ParserUnavailableError):
            return cls(category="runner", stage="parse", reason="parser_unavailable", message=str(exc))
        if isinstance(exc, CheckoutVerificationFailure):
            return cls(category="runner", stage="verify", reason="checkout_verify", message=str(exc))
        if isinstance(exc, CheckoutResetFailure):
            return cls(category="runner", stage="reset", reason="checkout_reset", message=str(exc))
        if isinstance(exc, ExecutionError):
            return cls(category="runner", stage="command", reason="runner_error", message=str(exc))
        return cls(category="runner", stage="write", reason="runner_error", message=str(exc))


class RowExecutionFailure(Exception):
    """Structured failure for one row.

    The command result is present only when the command ran or failed while
    running; callers use it to persist stdout/stderr in failure artifacts.
    """

    def __init__(self, details: FailureDetails, *, result: CommandResult | None = None, cause: Exception) -> None:
        super().__init__(details.message)
        self.details = details
        self.result = result
        self.cause = cause

    @classmethod
    def from_exception(cls, exc: Exception, *, result: CommandResult | None = None) -> Self:
        return cls(FailureDetails.classify(exc), result=result, cause=exc)

    @property
    def reason(self) -> str:
        return self.details.reason


@dataclass(frozen=True)
class WorkerExecutionOutcome:
    """Outcome for one selected input row."""

    success: bool
    failure_path: Path | None = None
    failure_reason: str | None = None
    failure_category: str | None = None


class ExecutionBackend(Protocol):
    """Backend-specific mechanism for running one benchmark row.

    Implementations create `results_dir`, write the command's raw report files
    into it, and raise `ExecutionError` subclasses when the mechanism itself
    fails. A nonzero command exit code is not an error: the backend returns the
    `CommandResult` and the row's parser decides the outcome.
    """

    def run(self, row: StateDatapoint, results_dir: Path, timeout: int) -> CommandResult:
        """Run the row command and write raw reports into `results_dir`."""
        ...


# Scratch file holding the patch's paths during the per-path fallback below.
_CONTAINER_PATCH_PATHS = "/tmp/sourceworldbench-patch-paths"

# Apply the patch whole; only if that fails, apply it one path at a time and skip
# the paths git does not track. See `_apply_patch` for why, and for the two guards
# that upstream's version does not have. This is the `/bin/sh` twin of that
# function, for backends that apply the patch inside the container rather than
# through git in this process.
#
# Paths come from `git apply --numstat`, i.e. from git's own patch parser rather
# than a regex over `diff --git` lines. Without `-z` (POSIX `read` cannot split on
# NUL) git munges pathnames needing quoting, and `--include` treats its argument
# as a glob, so a path that is quoted or holds glob metacharacters is refused
# outright rather than silently applied as a no-op.
_GIT_APPLY = """if git apply --whitespace=nowarn {path}; then
    :
elif ! git apply --numstat {path} > {paths} 2>/dev/null; then
    exit 99
else
    sourceworldbench_applied=0
    while IFS='\t' read -r _added _deleted sourceworldbench_path; do
        [ -n "$sourceworldbench_path" ] || continue
        case "$sourceworldbench_path" in '"'*|*'*'*|*'?'*|*'['*) exit 99 ;; esac
        if git apply --whitespace=nowarn --include "$sourceworldbench_path" {path}; then
            sourceworldbench_applied=$((sourceworldbench_applied + 1))
        elif git ls-files --error-unmatch -- "$sourceworldbench_path" >/dev/null 2>&1; then
            echo "sourceworldbench: patch conflicts with tracked path: $sourceworldbench_path" >&2
            exit 99
        else
            case "$sourceworldbench_path" in ..|../*|*/../*|*/..) exit 99 ;; esac
            rm -f -- "$sourceworldbench_path"
            if git apply --whitespace=nowarn --include "$sourceworldbench_path" {path}; then
                sourceworldbench_applied=$((sourceworldbench_applied + 1))
            else
                echo "sourceworldbench: entry for untracked path still fails after deleting it:" \\
                    "$sourceworldbench_path" >&2
                exit 99
            fi
        fi
    done < {paths}
    [ "$sourceworldbench_applied" -gt 0 ] || exit 99
fi"""
# Wrap the rebuild in `bash -c` so callers can pass a full pipeline (e.g.
# `. conda.sh && conda run ...`) without worrying about the outer `sh`'s quoting.
_REBUILD = "if ! bash -c {cmd}; then exit 98; fi"


def wrap_command(command: str, *, has_patch: bool, rebuild_command: str | None = None) -> str:
    """Apply the mounted patch, optionally rebuild, then run the benchmark command.

    A failed `git apply` exits 99 so `docker_runner.run_state` can tell patch
    failure apart from command failure. A patched command that itself exits 99
    is still reported as a patch failure — a known limitation.

    Unlike `_apply_patch`, skipped paths are only reported on stderr (captured in
    the row's `logs/command.stderr`), since this script has no way to hand
    structured data back to the caller.

    `rebuild_command` is only wrapped in when a patch is also applied; without
    a patch there is nothing to rebuild for.
    """
    parts: list[str] = []
    if has_patch:
        parts.append(_GIT_APPLY.format(path=CONTAINER_PATCH_PATH, paths=_CONTAINER_PATCH_PATHS))
        if rebuild_command:
            parts.append(_REBUILD.format(cmd=shlex.quote(rebuild_command)))
    parts.append(command)
    return "; ".join(parts)


class DockerExecutionBackend:
    """Run one benchmark row in a fresh container of the row's image.

    This is the backend local `collect` uses; `results_dir` is bind-mounted at
    `/results` inside the container.
    """

    def run(self, row: StateDatapoint, results_dir: Path, timeout: int) -> CommandResult:
        """Run the row command through Docker and write raw reports into `results_dir`."""
        results_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="sourceworldbench_docker_row_") as tmp:
            tmp_dir = Path(tmp)
            patch_path: Path | None = None
            if row.patch is not None:
                patch_path = tmp_dir / "patch.diff"
                patch_path.write_text(row.patch, encoding="utf-8")

            started_at = time.monotonic()
            try:
                result = run_docker_state(
                    image=row.container,
                    script=wrap_command(
                        row.command,
                        has_patch=patch_path is not None,
                        rebuild_command=row.metadata.get(REBUILD_COMMAND_METADATA_KEY),
                    ),
                    host_results=results_dir,
                    patch_path=patch_path,
                    timeout=timeout,
                )
            except PatchApplyFailed as exc:
                raise PatchApplyFailure(
                    "git apply failed inside the container",
                    stdout=exc.stdout,
                    stderr=exc.stderr,
                ) from exc
            except DockerTimeout as exc:
                raise ExecutionTimeout(str(exc), stdout=exc.stdout, stderr=exc.stderr) from exc
            except DockerError as exc:
                raise ExecutionError(str(exc), stdout=exc.stdout, stderr=exc.stderr) from exc

            # Sentinel exit code from wrap_command's rebuild step (see _REBUILD).
            if patch_path is not None and result.exit_code == 98 and row.metadata.get(REBUILD_COMMAND_METADATA_KEY):
                raise RebuildFailure(
                    "rebuild command failed inside the container",
                    stdout=result.stdout,
                    stderr=result.stderr,
                )

            # `rebuild_seconds` stays None: the rebuild runs inside the container script.
            return CommandResult(
                exit_code=result.exit_code,
                stdout=result.stdout,
                stderr=result.stderr,
                elapsed_seconds=time.monotonic() - started_at,
            )


class CheckoutExecutionBackend:
    """Run one benchmark row directly in a prepared repository checkout.

    The checkout HEAD is checked against the row's `base_commit` when
    `verify_checkout` is set. This is the backend Kubernetes worker pods use:
    the command runs in-place with `SOURCEWORLDBENCH_RESULTS_DIR` in its environment, and
    the worktree is reset beforehand when `reset_checkout` is set. Git helpers
    and the row command run
    with a scoped global git config marking all directories safe — the worker
    may run as a different uid than the checkout owner — and without the
    PyInstaller bootloader's `LD_LIBRARY_PATH`, so the benchmark image's own
    shared libraries win over the ones bundled with the worker binary.

    `command_results_dir` is where the row command writes raw reports; it
    defaults to `COMMAND_RESULTS_DIR` (the worker pod contract) and is
    injectable for runs outside a container.
    """

    def __init__(
        self,
        *,
        repo_dir: Path,
        reset_checkout: bool = False,
        verify_checkout: bool = False,
        command_results_dir: Path = COMMAND_RESULTS_DIR,
    ) -> None:
        self.repo_dir = repo_dir.resolve()
        self.reset_checkout = reset_checkout
        self.verify_checkout = verify_checkout
        self.command_results_dir = command_results_dir

    def run(
        self,
        row: StateDatapoint,
        results_dir: Path,
        timeout: int,
        *,
        extra_env: dict[str, str] | None = None,
    ) -> CommandResult:
        """Apply the optional patch, run the command, and copy raw reports to `results_dir`.

        `extra_env` is layered onto the command environment after the standard
        git/LD fixups; the trace worker uses it to activate a tracer for the
        run. It is empty on the normal collect path.
        """
        deadline = time.perf_counter() + timeout
        checkout = self.repo_dir
        results_dir.mkdir(parents=True, exist_ok=True)
        command_output_dir = self.command_results_dir.resolve()
        command_output_dir.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(prefix="sourceworldbench_row_") as tmp:
            tmp_dir = Path(tmp)
            # git refuses to operate on a repo owned by another user unless the
            # path is marked safe in a global/system config file (`-c` and
            # GIT_CONFIG_* pairs are deliberately ignored for safe.directory).
            gitconfig = tmp_dir / "gitconfig"
            gitconfig.write_text("[safe]\n\tdirectory = *\n", encoding="utf-8")
            env = os.environ.copy()
            # The PyInstaller bootloader points LD_LIBRARY_PATH at its bundled
            # build-image libraries; a child python resolving those over the
            # benchmark image's own breaks on version skew. The bootloader
            # saves the original value in LD_LIBRARY_PATH_ORIG.
            ld_orig = env.pop("LD_LIBRARY_PATH_ORIG", None)
            if ld_orig is not None:
                env["LD_LIBRARY_PATH"] = ld_orig
            else:
                env.pop("LD_LIBRARY_PATH", None)
            env["GIT_CONFIG_GLOBAL"] = str(gitconfig)
            env["SOURCEWORLDBENCH_RESULTS_DIR"] = str(command_output_dir)
            if extra_env:
                env |= extra_env

            # TODO: no caller sets verify_checkout, so this check never runs
            if self.verify_checkout and row.base_commit is not None:
                _verify_checkout_base(checkout=checkout, base_commit=row.base_commit, deadline=deadline, env=env)
            _empty_directory(results_dir)
            if command_output_dir != results_dir:
                _empty_directory(command_output_dir)
            # TODO: no caller sets reset_checkout, so the reset never runs
            if self.reset_checkout:
                _reset_checkout(checkout=checkout, deadline=deadline, env=env)

            skipped_patch_paths: list[str] = []
            rebuild_seconds: float | None = None
            if row.patch:
                # Some images from SWE-fficiency sit a "Fix environment" commit ahead of `base_commit`
                # (https://github.com/swefficiency/swefficiency/blob/12d32a2d6800824a7d84bdb6797b5708e7b7957f/swefficiency/harness/test_spec.py#L302);
                # patches are computed from base, so restore the touched files first before the apply step
                if row.base_commit is not None:
                    _restore_patch_files(
                        checkout=checkout,
                        base_commit=row.base_commit,
                        patch=row.patch,
                        deadline=deadline,
                        env=env,
                    )
                patch_path = tmp_dir / "patch.diff"
                patch_path.write_text(row.patch, encoding="utf-8")
                skipped_patch_paths = _apply_patch(
                    checkout=checkout,
                    patch_path=patch_path,
                    deadline=deadline,
                    env=env,
                )
                if skipped_patch_paths:
                    logger.warning(
                        "instance=%s applied partially: %d entr%s skipped as untracked (%s)",
                        row.instance_id,
                        len(skipped_patch_paths),
                        "y" if len(skipped_patch_paths) == 1 else "ies",
                        ", ".join(skipped_patch_paths),
                    )
                # Unconditional: the build system decides what needs recompiling.
                rebuild_command = row.metadata.get(REBUILD_COMMAND_METADATA_KEY)
                if rebuild_command:
                    rebuild_seconds = _run_rebuild(
                        checkout=checkout,
                        rebuild_command=str(rebuild_command),
                        deadline=deadline,
                        env=env,
                    )

            started_at = time.perf_counter()
            proc = _invoke(
                ["/bin/sh", "-c", row.command],
                cwd=checkout,
                timeout=_remaining_timeout(deadline),
                timeout_message=f"command timed out after {timeout}s",
                env=env,
            )
            elapsed = time.perf_counter() - started_at

        if command_output_dir != results_dir:
            _copy_directory_contents(command_output_dir, results_dir)

        return CommandResult(
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            elapsed_seconds=elapsed,
            rebuild_seconds=rebuild_seconds,
            skipped_patch_paths=tuple(skipped_patch_paths),
        )


def execute_row(
    *,
    partial: StateDatapoint,
    backend: ExecutionBackend,
    artifacts_dir: Path,
    timeout: int,
) -> StateDatapoint:
    """Execute one StateDatapoint and return the filled row.

    Use this from code that already has a validated row and owns where final
    output should be written. The caller supplies the backend-specific
    `ExecutionBackend`; this function owns parser lookup, failure classification,
    and result materialization.
    """
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    results_dir = artifacts_dir / "raw-results"
    logs_dir = artifacts_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    try:
        parser = get_parser(partial)
    except KeyError as exc:
        raise RowExecutionFailure.from_exception(ParserUnavailableError(str(exc))) from exc

    try:
        logger.info("running row for instance=%s", partial.instance_id)
        command_result = backend.run(
            row=partial,
            results_dir=results_dir,
            timeout=timeout,
        )
        (logs_dir / "command.stdout").write_bytes(command_result.stdout)
        (logs_dir / "command.stderr").write_bytes(command_result.stderr)
    except ExecutionError as exc:
        raise RowExecutionFailure.from_exception(
            exc,
            result=CommandResult(exit_code=-1, stdout=exc.stdout, stderr=exc.stderr, elapsed_seconds=0.0),
        ) from exc

    if command_result.exit_code not in _COMPLETE_SESSION_EXIT_CODES:
        exc = AbortedSessionError(
            f"test command exited with {command_result.exit_code}, so its report is incomplete",
            stdout=command_result.stdout,
            stderr=command_result.stderr,
        )
        raise RowExecutionFailure.from_exception(exc, result=command_result) from exc

    try:
        report = parser(results_dir)
    except (MissingReportError, MalformedReportError, EmptyReportError, ConflictingReportError) as exc:
        raise RowExecutionFailure.from_exception(exc, result=command_result) from exc

    filled = StateDatapoint.model_validate(partial.model_dump() | _result_fields_from_report(report))
    metadata = {
        "instance_id": partial.instance_id,
        "exit_code": command_result.exit_code,
        "elapsed_seconds": command_result.elapsed_seconds,
        # Null unless a rebuild ran and the backend timed it — see `CommandResult`.
        "rebuild_seconds": command_result.rebuild_seconds,
        # Empty unless the patch applied only partially — see `CommandResult`.
        "skipped_patch_paths": list(command_result.skipped_patch_paths),
    }
    _write_text_atomic(artifacts_dir / "metadata.json", json.dumps(metadata, sort_keys=True) + "\n")
    return filled


def execute_worker_row(
    *,
    input_path: Path,
    instance_id: str,
    backend: ExecutionBackend,
    out_path: Path,
    artifacts_dir: Path,
    timeout: int,
) -> WorkerExecutionOutcome:
    """Select one input row, execute it if needed, and write worker output files.

    Use this at a process boundary such as the `execute` CLI. It owns row
    selection, complete-row passthrough, final result writes, and failure
    artifact writes. Every row failure becomes a `failure.json` plus a
    `WorkerExecutionOutcome`; only a failure to write the worker outputs
    themselves (OSError) propagates. Code that already has a `StateDatapoint`
    should call `execute_row` instead.
    """
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    raw_line = ""
    try:
        raw_line, partial = select_input_row(input_path=input_path, instance_id=instance_id)
        if is_result_complete(partial.model_dump()):
            _write_text_atomic(out_path, partial.model_dump_json() + "\n")
            return WorkerExecutionOutcome(success=True)
        filled = execute_row(
            partial=partial,
            backend=backend,
            artifacts_dir=artifacts_dir,
            timeout=timeout,
        )
        _write_text_atomic(out_path, filled.model_dump_json() + "\n")
        return WorkerExecutionOutcome(success=True)
    except RowExecutionFailure as exc:
        failure = exc
    except Exception as exc:
        failure = RowExecutionFailure.from_exception(exc)

    failure_path = artifacts_dir / "failure.json"
    write_failure_artifact(
        failure_path,
        instance_id=instance_id,
        failure=failure,
        raw_line=raw_line,
    )
    return WorkerExecutionOutcome(
        success=False,
        failure_path=failure_path,
        failure_reason=failure.reason,
        failure_category=failure.details.category,
    )


def execute_worker_row_gcs(
    *,
    input_uri: str,
    instance_id: str,
    backend: ExecutionBackend,
    out_uri: str,
    artifacts_uri: str,
    timeout: int,
    gcs: GcsFiles,
) -> WorkerExecutionOutcome:
    """Run one row with gs:// input and outputs: stage locally, upload at the end.

    Artifacts upload first; the completion marker — `result.json` on success,
    `failure.json` on failure — uploads last, because `k8s download` treats the
    presence of either as "row finished".
    """
    with tempfile.TemporaryDirectory(prefix="sourceworldbench_worker_") as tmp:
        tmp_dir = Path(tmp)
        input_path = tmp_dir / "rows.jsonl"
        gcs.download_file(input_uri, input_path)
        out_path = tmp_dir / "result.json"
        artifacts_dir = tmp_dir / "artifacts"
        outcome = execute_worker_row(
            input_path=input_path,
            instance_id=instance_id,
            backend=backend,
            out_path=out_path,
            artifacts_dir=artifacts_dir,
            timeout=timeout,
        )
        artifacts_uri = artifacts_uri.rstrip("/")
        failure_json = artifacts_dir / "failure.json"
        for path in sorted(p for p in artifacts_dir.rglob("*") if p.is_file() and p != failure_json):
            gcs.upload_file(path, f"{artifacts_uri}/{path.relative_to(artifacts_dir)}")
        if outcome.success:
            gcs.upload_file(out_path, out_uri)
        else:
            gcs.upload_file(failure_json, f"{artifacts_uri}/failure.json")
        return outcome


def select_input_row(*, input_path: Path, instance_id: str) -> tuple[str, StateDatapoint]:
    """Select exactly one row by stable `instance_id` from a complete JSONL input.

    Every line must be a valid `StateDatapoint`: an invalid line anywhere fails
    selection even when a matching row exists.
    """
    matches: list[tuple[str, StateDatapoint]] = []
    with input_path.open("r", encoding="utf-8") as handle:
        for raw in handle:
            if not raw.strip():
                continue
            try:
                partial = StateDatapoint.model_validate_json(raw)
            except ValidationError as exc:
                raise InputSelectionError(f"invalid input row while selecting {instance_id}: {exc}") from exc
            if partial.instance_id == instance_id:
                matches.append((raw if raw.endswith("\n") else f"{raw}\n", partial))
    if len(matches) != 1:
        raise InputSelectionError(f"expected exactly one row for instance_id={instance_id}, found {len(matches)}")
    return matches[0]


def is_result_complete(payload: dict[str, Any]) -> bool:
    """Return True when all current result fields are already filled."""
    return all(payload.get(field) is not None for field in STATE_OUTCOME_FIELDS)


def write_failure_artifact(
    failure_path: Path,
    *,
    instance_id: str,
    failure: RowExecutionFailure,
    raw_line: str,
) -> None:
    """Write the failure files consumed by local output and `k8s download`.

    Writes `failure.json` (the schema shared with `k8s download`),
    `partial.json`, `error.txt`, and `logs/command.{stdout,stderr}` when a
    command result exists.
    """
    artifacts_dir = failure_path.parent
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = artifacts_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    if failure.result is not None:
        (logs_dir / "command.stdout").write_bytes(failure.result.stdout)
        (logs_dir / "command.stderr").write_bytes(failure.result.stderr)

    (artifacts_dir / "partial.json").write_text(raw_line, encoding="utf-8")
    (artifacts_dir / "error.txt").write_text(
        f"{type(failure.cause).__name__}: {failure.cause}\n",
        encoding="utf-8",
    )
    failure_payload = failure.details.artifact_payload(instance_id)
    _write_text_atomic(failure_path, json.dumps(failure_payload, sort_keys=True) + "\n")


def _result_fields_from_report(report: ParsedReport) -> dict[str, list[str]]:
    by_status: dict[TestStatus, list[str]] = {status: [] for status in TestStatus}
    for outcome in report.outcomes:
        by_status[outcome.status].append(outcome.name)
    return {
        "passed_tests": sorted(by_status[TestStatus.PASSED]),
        "failed_tests": sorted(by_status[TestStatus.FAILED]),
        "skipped_tests": sorted(by_status[TestStatus.SKIPPED]),
        "errored_tests": sorted(by_status[TestStatus.ERROR]),
        "discovery_errors": sorted(report.discovery_errors),
    }


# Old-side paths (`--- a/<path>`), as in upstream's DIFF_MODIFIED_FILE_REGEX
# (https://github.com/swefficiency/swefficiency/blob/12d32a2d6800824a7d84bdb6797b5708e7b7957f/swefficiency/harness/test_spec.py#L49).
_PATCH_OLD_FILE = re.compile(r"^--- a/(.*)$", re.MULTILINE)


def _restore_patch_files(
    *, checkout: Path, base_commit: str, patch: str, deadline: float, env: dict[str, str] | None = None
) -> None:
    """Restore the files a patch touches to `base_commit`.

    Takes inspiration from what SWE-fficiency harness does before applying.
    (https://github.com/swefficiency/swefficiency/blob/12d32a2d6800824a7d84bdb6797b5708e7b7957f/swefficiency/harness/test_spec.py#L615)
    """
    files = _PATCH_OLD_FILE.findall(patch)
    if not files:
        return
    proc = _invoke(
        ["git", "checkout", base_commit, "--", *files],
        cwd=checkout,
        timeout=_remaining_timeout(deadline),
        timeout_message="restoring patched files to the base commit timed out",
        env=env,
    )
    if proc.returncode != 0:
        raise PatchRestoreFailure(
            f"could not restore the patched files to base commit {base_commit}",
            stdout=proc.stdout,
            stderr=proc.stderr,
        )


# `--include` takes a glob, so a path holding one of these cannot be addressed
# exactly and the per-path fallback refuses to guess.
_UNSUPPORTED_PATCH_PATH = re.compile(r"[*?\[]")


def _apply_patch(*, checkout: Path, patch_path: Path, deadline: float, env: dict[str, str] | None = None) -> list[str]:
    """Apply `patch_path` in `checkout`; return the paths skipped as untracked.

    The whole patch is applied first, so a patch that applies cleanly keeps
    `git apply`'s all-or-nothing semantics exactly. Only when that fails does the
    per-path fallback run, because an unappliable *entry* is not the same thing as
    an unappliable patch: prediction patches are diffed from a built container
    after `git add -A`, so they can declare as new a file the image's own build
    already generated, and that one entry currently rejects the real source edits
    with it.

    The fallback asks git which paths are its own, as upstream's harness does
    (https://github.com/swefficiency/swefficiency/blob/12d32a2d6800824a7d84bdb6797b5708e7b7957f/swefficiency/harness/run_validation.py#L165):
    a failing path that is *tracked* is a genuine conflict and fails the row; an
    *untracked* one is not repository content but a build artifact, so it is
    deleted and the entry applied in full — the rebuild step regenerates such
    artifacts, and a partially applied patch would test a state that is neither
    base nor prediction. An entry that still fails after the collider is gone
    fails the row rather than being dropped.
    """
    proc = _invoke(
        ["git", "apply", "--whitespace=nowarn", str(patch_path)],
        cwd=checkout,
        timeout=_remaining_timeout(deadline),
        timeout_message="patch apply timed out",
        env=env,
    )
    if proc.returncode == 0:
        return []
    return _apply_patch_per_path(
        checkout=checkout,
        patch_path=patch_path,
        deadline=deadline,
        env=env,
        whole_patch_failure=proc,
    )


def _apply_patch_per_path(
    *,
    checkout: Path,
    patch_path: Path,
    deadline: float,
    env: dict[str, str] | None,
    whole_patch_failure: subprocess.CompletedProcess[bytes],
) -> list[str]:
    """Apply one path of `patch_path` at a time; see `_apply_patch` for the contract."""
    paths = _patch_paths(checkout=checkout, patch_path=patch_path, deadline=deadline, env=env)
    if not paths or any(_UNSUPPORTED_PATCH_PATH.search(path) for path in paths):
        raise PatchApplyFailure(
            "patch failed to apply",
            stdout=whole_patch_failure.stdout,
            stderr=whole_patch_failure.stderr,
        )

    for path in paths:
        proc = _invoke(
            ["git", "apply", "--whitespace=nowarn", "--include", path, str(patch_path)],
            cwd=checkout,
            timeout=_remaining_timeout(deadline),
            timeout_message="patch apply timed out",
            env=env,
        )
        if proc.returncode == 0:
            continue
        if _is_tracked(checkout=checkout, path=path, deadline=deadline, env=env):
            raise PatchApplyFailure(
                f"patch failed to apply: conflict on tracked path {path}",
                stdout=proc.stdout,
                stderr=proc.stderr,
            )
        _delete_untracked_collider(checkout=checkout, path=path)
        retry = _invoke(
            ["git", "apply", "--whitespace=nowarn", "--include", path, str(patch_path)],
            cwd=checkout,
            timeout=_remaining_timeout(deadline),
            timeout_message="patch apply timed out",
            env=env,
        )
        if retry.returncode != 0:
            raise PatchApplyFailure(
                f"patch failed to apply: entry for untracked path {path} still fails after deleting it",
                stdout=retry.stdout,
                stderr=retry.stderr,
            )
    return []


def _delete_untracked_collider(*, checkout: Path, path: str) -> None:
    """Remove the untracked file blocking a patch entry, never stepping outside `checkout`."""
    target = (checkout / path).resolve()
    if not target.is_relative_to(checkout.resolve()):
        raise PatchApplyFailure(f"patch failed to apply: entry path escapes the checkout: {path}")
    try:
        target.unlink()
    except OSError as exc:
        raise PatchApplyFailure(f"patch failed to apply: cannot delete untracked collider {path}: {exc}") from exc


def _patch_paths(*, checkout: Path, patch_path: Path, deadline: float, env: dict[str, str] | None = None) -> list[str]:
    """The paths a patch touches, according to git's own patch parser.

    `--numstat -z` is used rather than a regex over `diff --git` lines so paths
    holding spaces survive verbatim, and so a patch git cannot parse at all yields
    no paths rather than a plausible-looking guess. Records are
    `added<TAB>deleted<TAB>path<NUL>`; a rename yields a single record naming the
    new path.
    """
    proc = _invoke(
        ["git", "apply", "--numstat", "-z", str(patch_path)],
        cwd=checkout,
        timeout=_remaining_timeout(deadline),
        timeout_message="listing the patch's paths timed out",
        env=env,
    )
    if proc.returncode != 0:
        return []
    paths: list[str] = []
    for record in proc.stdout.split(b"\x00"):
        if not record:
            continue
        fields = record.split(b"\t", 2)
        if len(fields) != 3:
            return []
        paths.append(fields[2].decode("utf-8", errors="replace"))
    return paths


def _is_tracked(*, checkout: Path, path: str, deadline: float, env: dict[str, str] | None = None) -> bool:
    """Whether git tracks `path` — the line between repository content and build output."""
    proc = _invoke(
        ["git", "ls-files", "--error-unmatch", "--", path],
        cwd=checkout,
        timeout=_remaining_timeout(deadline),
        timeout_message="checking whether a patched path is tracked timed out",
        env=env,
    )
    return proc.returncode == 0


def _run_rebuild(*, checkout: Path, rebuild_command: str, deadline: float, env: dict[str, str] | None = None) -> float:
    """Run a post-patch rebuild command in the checkout via `/bin/sh -c`.

    Recompiles the patched sources so they replace the extensions baked in the
    container. Run for every augmentation row that declares a command. Failure
    raises `RebuildFailure`, classified as a row-level failure (the row's own
    rebuild instruction was wrong or the patched source doesn't compile).

    Returns its wall time: the rebuild can outweigh the test command, and no
    other artifact separates the two.
    """
    started_at = time.perf_counter()
    proc = _invoke(
        ["/bin/sh", "-c", rebuild_command],
        cwd=checkout,
        timeout=_remaining_timeout(deadline),
        timeout_message="rebuild command timed out",
        env=env,
    )
    elapsed = time.perf_counter() - started_at
    if proc.returncode != 0:
        raise RebuildFailure(
            f"rebuild command failed with exit code {proc.returncode}",
            stdout=proc.stdout,
            stderr=proc.stderr,
        )
    return elapsed


def _reset_checkout(*, checkout: Path, deadline: float, env: dict[str, str] | None = None) -> None:
    proc = _invoke(
        ["git", "reset", "--hard"],
        cwd=checkout,
        timeout=_remaining_timeout(deadline),
        timeout_message="checkout reset timed out",
        env=env,
    )
    if proc.returncode != 0:
        raise CheckoutResetFailure(
            "git reset --hard failed",
            stdout=proc.stdout,
            stderr=proc.stderr,
        )


def _verify_checkout_base(
    *, checkout: Path, base_commit: str, deadline: float, env: dict[str, str] | None = None
) -> None:
    proc = _invoke(
        ["git", "rev-parse", "HEAD"],
        cwd=checkout,
        timeout=_remaining_timeout(deadline),
        timeout_message="checkout verification timed out",
        env=env,
    )
    if proc.returncode != 0:
        raise CheckoutVerificationFailure(
            "could not determine checkout HEAD",
            stdout=proc.stdout,
            stderr=proc.stderr,
        )
    observed = proc.stdout.decode("utf-8", errors="replace").strip()
    if observed != base_commit:
        raise CheckoutVerificationFailure(f"checkout HEAD {observed} does not match base_commit {base_commit}")


def _invoke(
    args: list[str],
    *,
    cwd: Path,
    timeout: float,
    timeout_message: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            args,
            cwd=cwd,
            env=env,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExecutionTimeout(
            timeout_message,
            stdout=exc.output or b"",
            stderr=exc.stderr or b"",
        ) from exc
    except FileNotFoundError as exc:
        raise ExecutionError(f"executable not found: {args[0]}") from exc


def _remaining_timeout(deadline: float) -> float:
    remaining = deadline - time.perf_counter()
    if remaining <= 0:
        raise ExecutionTimeout("execution timed out")
    return remaining


def _empty_directory(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)


def _copy_directory_contents(source: Path, destination: Path) -> None:
    """Copy file contents recursively without preserving metadata.

    Metadata-preserving copies (`shutil.copy2`, `shutil.copytree`) fail on
    filesystems that reject chmod/utime for non-root users (e.g. FUSE mounts);
    only the contents matter here. Callers empty `destination` beforehand, so
    existing entries only need to be merged over, never removed.
    """
    destination.mkdir(parents=True, exist_ok=True)
    for child in sorted(source.rglob("*")):
        target = destination / child.relative_to(source)
        if child.is_dir() and not child.is_symlink():
            target.mkdir(parents=True, exist_ok=True)
        else:
            shutil.copyfile(child, target)


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
