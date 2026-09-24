import json
import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sourceworldbench_benchmarks import execution
from sourceworldbench_benchmarks.cli import app
from sourceworldbench_benchmarks.execution import CheckoutExecutionBackend, execute_row, execute_worker_row
from sourceworldbench_benchmarks.report import ParsedReport, TestResult, TestStatus
from sourceworldbench_benchmarks.schema import StateDatapoint
from tests.conftest import FakeGcs


def _run_git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _run_git_raw(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return proc.stdout


def _stub_parser(results_dir: Path) -> ParsedReport:
    payload = json.loads((results_dir / "stub.json").read_text(encoding="utf-8"))
    return ParsedReport(
        outcomes=[TestResult(name=item["name"], status=TestStatus(item["status"])) for item in payload["tests"]],
        discovery_errors=[],
    )


def _checkout_backend(repo: Path, tmp_path: Path) -> CheckoutExecutionBackend:
    """A backend whose command-results dir is redirected away from the real /results."""
    return CheckoutExecutionBackend(repo_dir=repo, command_results_dir=tmp_path / "command-results")


def _make_repo_datapoint(tmp_path: Path) -> tuple[Path, StateDatapoint]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(repo, "init")
    _run_git(repo, "config", "user.email", "test@example.com")
    _run_git(repo, "config", "user.name", "Test User")
    (repo / "value.txt").write_text("old\n", encoding="utf-8")
    _run_git(repo, "add", "value.txt")
    _run_git(repo, "commit", "-m", "base")
    base_commit = _run_git(repo, "rev-parse", "HEAD")
    (repo / "value.txt").write_text("new\n", encoding="utf-8")
    patch = _run_git_raw(repo, "diff")
    _run_git(repo, "reset", "--hard")
    command = (
        'python -c "import json, os, pathlib; '
        "value = pathlib.Path('value.txt').read_text().strip(); "
        "status = 'PASSED' if value == 'new' else 'FAILED'; "
        "out = pathlib.Path(os.environ['SOURCEWORLDBENCH_RESULTS_DIR']); out.mkdir(parents=True, exist_ok=True); "
        "(out / 'stub.json').write_text(json.dumps({'tests': [{'name': 'test_value', 'status': status}]}))\""
    )
    return repo, StateDatapoint(
        instance_id="repo__row__x",
        repo="owner/repo",
        base_commit=base_commit,
        patch=patch,
        container="registry.example.com/sourceworldbench/row@sha256:abc",
        command=command,
        test_scope=["tests/"],
    )


def test_execute_row_runs_patched_state(monkeypatch, tmp_path: Path) -> None:
    repo, partial = _make_repo_datapoint(tmp_path)
    monkeypatch.setattr(execution, "get_parser", lambda _partial: _stub_parser)

    filled = execute_row(
        partial=partial,
        backend=_checkout_backend(repo, tmp_path),
        artifacts_dir=tmp_path / "artifacts",
        timeout=30,
    )

    assert filled.passed_tests == ["test_value"]
    assert filled.failed_tests == []
    assert (tmp_path / "artifacts" / "raw-results" / "stub.json").exists()


def test_execute_row_runs_base_state_without_patch(monkeypatch, tmp_path: Path) -> None:
    repo, partial = _make_repo_datapoint(tmp_path)
    partial = partial.model_copy(update={"patch": None})
    monkeypatch.setattr(execution, "get_parser", lambda _partial: _stub_parser)

    filled = execute_row(
        partial=partial,
        backend=_checkout_backend(repo, tmp_path),
        artifacts_dir=tmp_path / "artifacts",
        timeout=30,
    )

    assert filled.passed_tests == []
    assert filled.failed_tests == ["test_value"]


def test_checkout_backend_marks_all_git_directories_safe(monkeypatch, tmp_path: Path) -> None:
    """The worker may run as a different uid than the checkout owner; git must not refuse with 'dubious ownership'."""
    repo, partial = _make_repo_datapoint(tmp_path)
    command = (
        'python -c "import json, os, pathlib, subprocess; '
        "safe = subprocess.run(['git', 'config', '--get', 'safe.directory'], capture_output=True, text=True); "
        "status = 'PASSED' if safe.stdout.strip() == '*' else 'FAILED'; "
        "out = pathlib.Path(os.environ['SOURCEWORLDBENCH_RESULTS_DIR']); out.mkdir(parents=True, exist_ok=True); "
        "(out / 'stub.json').write_text(json.dumps({'tests': [{'name': 'test_safe_dir', 'status': status}]}))\""
    )
    partial = partial.model_copy(update={"patch": None, "command": command})
    monkeypatch.setattr(execution, "get_parser", lambda _partial: _stub_parser)

    filled = execute_row(
        partial=partial,
        backend=_checkout_backend(repo, tmp_path),
        artifacts_dir=tmp_path / "artifacts",
        timeout=30,
    )

    assert filled.passed_tests == ["test_safe_dir"]


@pytest.mark.parametrize(
    ("exit_code", "expected_reason"),
    [
        (0, None),
        (1, None),
        (3, "aborted_session"),
        (139, "aborted_session"),
    ],
)
def test_execute_row_rejects_reports_from_sessions_that_did_not_finish(
    monkeypatch, tmp_path: Path, exit_code: int, expected_reason: str | None
) -> None:
    """A report is only trustworthy for pytest exit 0/1. Failing tests (1) are results and
    must pass through; an aborted session (3 INTERNALERROR, 139 SIGSEGV) leaves a truncated
    report that would silently compare different test sets across base and fixed."""
    repo, partial = _make_repo_datapoint(tmp_path)
    command = (
        'python -c "import json, os, pathlib, sys; '
        "out = pathlib.Path(os.environ['SOURCEWORLDBENCH_RESULTS_DIR']); out.mkdir(parents=True, exist_ok=True); "
        "(out / 'stub.json').write_text(json.dumps({'tests': [{'name': 'test_value', 'status': 'PASSED'}]})); "
        f'sys.exit({exit_code})"'
    )
    partial = partial.model_copy(update={"patch": None, "command": command})
    monkeypatch.setattr(execution, "get_parser", lambda _partial: _stub_parser)

    if expected_reason is None:
        filled = execute_row(
            partial=partial,
            backend=_checkout_backend(repo, tmp_path),
            artifacts_dir=tmp_path / "artifacts",
            timeout=30,
        )
        assert filled.passed_tests == ["test_value"]
        return

    with pytest.raises(execution.RowExecutionFailure) as excinfo:
        execute_row(
            partial=partial,
            backend=_checkout_backend(repo, tmp_path),
            artifacts_dir=tmp_path / "artifacts",
            timeout=30,
        )
    assert excinfo.value.reason == expected_reason
    assert excinfo.value.details.category == "row"


def test_checkout_backend_layers_extra_env_and_leaves_it_absent_by_default(tmp_path: Path) -> None:
    """The trace worker's tracer activation rides `extra_env`; the collect path must not see it."""
    repo, partial = _make_repo_datapoint(tmp_path)
    command = (
        'python -c "import os, pathlib; '
        "out = pathlib.Path(os.environ['SOURCEWORLDBENCH_RESULTS_DIR']); out.mkdir(parents=True, exist_ok=True); "
        "(out / 'marker.txt').write_text(os.environ.get('SOURCEWORLDBENCH_TEST_MARKER', 'ABSENT'))\""
    )
    partial = partial.model_copy(update={"patch": None, "command": command})
    backend = _checkout_backend(repo, tmp_path)

    with_env = tmp_path / "with"
    backend.run(partial, with_env, timeout=30, extra_env={"SOURCEWORLDBENCH_TEST_MARKER": "PRESENT"})
    assert (with_env / "marker.txt").read_text(encoding="utf-8") == "PRESENT"

    without_env = tmp_path / "without"
    backend.run(partial, without_env, timeout=30)
    assert (without_env / "marker.txt").read_text(encoding="utf-8") == "ABSENT"


@pytest.mark.parametrize(
    ("ld_orig", "expected"),
    [("/usr/lib/original", "/usr/lib/original"), (None, "")],
    ids=["restores-original", "removes-when-no-original"],
)
def test_checkout_backend_undoes_pyinstaller_ld_library_path(
    monkeypatch, tmp_path: Path, ld_orig: str | None, expected: str
) -> None:
    """The PyInstaller bootloader's LD_LIBRARY_PATH must not leak into the row command,
    where its bundled build-image libraries would shadow the benchmark image's own."""
    repo, partial = _make_repo_datapoint(tmp_path)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEIfake")
    if ld_orig is None:
        monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)
    else:
        monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", ld_orig)
    command = (
        'python -c "import json, os, pathlib; '
        f"status = 'PASSED' if os.environ.get('LD_LIBRARY_PATH', '') == '{expected}' "
        "and 'LD_LIBRARY_PATH_ORIG' not in os.environ else 'FAILED'; "
        "out = pathlib.Path(os.environ['SOURCEWORLDBENCH_RESULTS_DIR']); out.mkdir(parents=True, exist_ok=True); "
        "(out / 'stub.json').write_text(json.dumps({'tests': [{'name': 'test_ld_path', 'status': status}]}))\""
    )
    partial = partial.model_copy(update={"patch": None, "command": command})
    monkeypatch.setattr(execution, "get_parser", lambda _partial: _stub_parser)

    filled = execute_row(
        partial=partial,
        backend=_checkout_backend(repo, tmp_path),
        artifacts_dir=tmp_path / "artifacts",
        timeout=30,
    )

    assert filled.passed_tests == ["test_ld_path"]


def test_execute_worker_row_writes_failure_for_patch_apply(monkeypatch, tmp_path: Path) -> None:
    repo, partial = _make_repo_datapoint(tmp_path)
    # value.txt exists at base_commit, so the restore succeeds and `git apply` is what rejects the patch.
    bad_patch = "diff --git a/value.txt b/value.txt\n--- a/value.txt\n+++ b/value.txt\n@@ -1 +1 @@\n-nonsense\n+b\n"
    partial = partial.model_copy(update={"patch": bad_patch})
    monkeypatch.setattr(execution, "get_parser", lambda _partial: _stub_parser)
    in_path = tmp_path / "input.jsonl"
    in_path.write_text(partial.model_dump_json() + "\n", encoding="utf-8")

    outcome = execute_worker_row(
        input_path=in_path,
        instance_id=partial.instance_id,
        backend=_checkout_backend(repo, tmp_path),
        out_path=tmp_path / "execute.json",
        artifacts_dir=tmp_path / "execute-artifacts",
        timeout=30,
    )
    assert not outcome.success
    failure = json.loads((tmp_path / "execute-artifacts" / "failure.json").read_text(encoding="utf-8"))
    assert failure["category"] == "row"
    assert failure["reason"] == "patch_apply"


# A build artifact the container generated: on disk, deliberately untracked, and
# swept into the prediction diff by the agent harness' `git add -A`. `git apply`
# rejects the whole patch over it — including the real edit to value.txt.
_GENERATED_FILE_ENTRY = (
    "diff --git a/generated.c b/generated.c\n"
    "new file mode 100644\n"
    "index 0000000..1234567\n"
    "--- /dev/null\n"
    "+++ b/generated.c\n"
    "@@ -0,0 +1 @@\n"
    "+/* autogenerated, do not edit */\n"
)


def test_patch_entry_replaces_an_untracked_file_already_in_the_checkout(monkeypatch, tmp_path: Path) -> None:
    """The colliding build artifact is deleted and the patch applies in full."""
    repo, partial = _make_repo_datapoint(tmp_path)
    (repo / "generated.c").write_text("/* built when the image was made */\n", encoding="utf-8")
    partial = partial.model_copy(update={"patch": partial.patch + _GENERATED_FILE_ENTRY})
    monkeypatch.setattr(execution, "get_parser", lambda _partial: _stub_parser)

    filled = execute_row(
        partial=partial,
        backend=_checkout_backend(repo, tmp_path),
        artifacts_dir=tmp_path / "artifacts",
        timeout=30,
    )

    assert filled.passed_tests == ["test_value"]
    # The patch wins over the stale generated file: the row runs the predicted state.
    assert (repo / "generated.c").read_text(encoding="utf-8") == "/* autogenerated, do not edit */\n"
    metadata = json.loads((tmp_path / "artifacts" / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["skipped_patch_paths"] == []


def test_fully_applied_patch_records_no_skipped_paths(monkeypatch, tmp_path: Path) -> None:
    """The whole-patch path stays all-or-nothing: nothing is skipped and nothing is reported."""
    repo, partial = _make_repo_datapoint(tmp_path)
    monkeypatch.setattr(execution, "get_parser", lambda _partial: _stub_parser)

    execute_row(
        partial=partial,
        backend=_checkout_backend(repo, tmp_path),
        artifacts_dir=tmp_path / "artifacts",
        timeout=30,
    )

    metadata = json.loads((tmp_path / "artifacts" / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["skipped_patch_paths"] == []


def test_rebuild_time_is_recorded_apart_from_the_command_time(monkeypatch, tmp_path: Path) -> None:
    """The rebuild can outweigh the tests, so the artifacts must not lump the two together."""
    repo, partial = _make_repo_datapoint(tmp_path)
    partial = partial.model_copy(update={"metadata": {execution.REBUILD_COMMAND_METADATA_KEY: "sleep 0.4"}})
    monkeypatch.setattr(execution, "get_parser", lambda _partial: _stub_parser)

    execute_row(
        partial=partial,
        backend=_checkout_backend(repo, tmp_path),
        artifacts_dir=tmp_path / "artifacts",
        timeout=30,
    )

    metadata = json.loads((tmp_path / "artifacts" / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["rebuild_seconds"] >= 0.4
    # `elapsed_seconds` covers the command alone: the sleep must not land in it.
    assert metadata["elapsed_seconds"] < metadata["rebuild_seconds"]


def test_a_row_declaring_no_rebuild_records_no_rebuild_time(monkeypatch, tmp_path: Path) -> None:
    repo, partial = _make_repo_datapoint(tmp_path)
    monkeypatch.setattr(execution, "get_parser", lambda _partial: _stub_parser)

    execute_row(
        partial=partial,
        backend=_checkout_backend(repo, tmp_path),
        artifacts_dir=tmp_path / "artifacts",
        timeout=30,
    )

    metadata = json.loads((tmp_path / "artifacts" / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["rebuild_seconds"] is None


def test_conflict_on_a_tracked_path_still_fails_the_row(monkeypatch, tmp_path: Path) -> None:
    """Skipping is only ever for untracked paths; a tracked conflict must not be tolerated."""
    repo, partial = _make_repo_datapoint(tmp_path)
    # value.txt is tracked, and this hunk's context does not match it.
    conflicting = "diff --git a/value.txt b/value.txt\n--- a/value.txt\n+++ b/value.txt\n@@ -1 +1 @@\n-nonsense\n+b\n"
    partial = partial.model_copy(update={"patch": conflicting + _GENERATED_FILE_ENTRY})
    monkeypatch.setattr(execution, "get_parser", lambda _partial: _stub_parser)
    in_path = tmp_path / "input.jsonl"
    in_path.write_text(partial.model_dump_json() + "\n", encoding="utf-8")

    outcome = execute_worker_row(
        input_path=in_path,
        instance_id=partial.instance_id,
        backend=_checkout_backend(repo, tmp_path),
        out_path=tmp_path / "execute.json",
        artifacts_dir=tmp_path / "execute-artifacts",
        timeout=30,
    )

    assert not outcome.success
    failure = json.loads((tmp_path / "execute-artifacts" / "failure.json").read_text(encoding="utf-8"))
    assert failure["reason"] == "patch_apply"
    assert "value.txt" in failure["message"]


def test_artifact_only_patch_applies_by_replacing_the_collider(monkeypatch, tmp_path: Path) -> None:
    """Even a patch whose only entry collides with a build artifact applies in full."""
    repo, partial = _make_repo_datapoint(tmp_path)
    (repo / "generated.c").write_text("/* built when the image was made */\n", encoding="utf-8")
    partial = partial.model_copy(update={"patch": _GENERATED_FILE_ENTRY})
    monkeypatch.setattr(execution, "get_parser", lambda _partial: _stub_parser)
    in_path = tmp_path / "input.jsonl"
    in_path.write_text(partial.model_dump_json() + "\n", encoding="utf-8")

    outcome = execute_worker_row(
        input_path=in_path,
        instance_id=partial.instance_id,
        backend=_checkout_backend(repo, tmp_path),
        out_path=tmp_path / "execute.json",
        artifacts_dir=tmp_path / "execute-artifacts",
        timeout=30,
    )

    assert outcome.success
    assert (repo / "generated.c").read_text(encoding="utf-8") == "/* autogenerated, do not edit */\n"


def _run_container_apply(repo: Path, patch: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Run the in-container apply script (`_GIT_APPLY`) against a real repo via /bin/sh.

    The script is what backends that apply the patch inside the container use, so it
    is exercised here directly rather than through docker.
    """
    patch_path = tmp_path / "container-patch.diff"
    patch_path.write_text(patch, encoding="utf-8")
    script = execution._GIT_APPLY.format(path=patch_path, paths=tmp_path / "container-patch-paths")
    return subprocess.run(
        ["/bin/sh", "-c", f"{script}; echo COMMAND-RAN"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


def test_container_apply_replaces_an_untracked_collision_and_runs_the_command(tmp_path: Path) -> None:
    repo, partial = _make_repo_datapoint(tmp_path)
    (repo / "generated.c").write_text("/* built when the image was made */\n", encoding="utf-8")

    proc = _run_container_apply(repo, str(partial.patch) + _GENERATED_FILE_ENTRY, tmp_path)

    assert proc.returncode == 0
    assert "COMMAND-RAN" in proc.stdout
    assert (repo / "value.txt").read_text(encoding="utf-8") == "new\n"
    assert (repo / "generated.c").read_text(encoding="utf-8") == "/* autogenerated, do not edit */\n"


def test_container_apply_fails_on_a_tracked_conflict(tmp_path: Path) -> None:
    repo, _partial = _make_repo_datapoint(tmp_path)
    conflicting = "diff --git a/value.txt b/value.txt\n--- a/value.txt\n+++ b/value.txt\n@@ -1 +1 @@\n-nonsense\n+b\n"

    proc = _run_container_apply(repo, conflicting, tmp_path)

    assert proc.returncode == 99
    assert "COMMAND-RAN" not in proc.stdout


def test_container_apply_handles_an_artifact_only_patch(tmp_path: Path) -> None:
    repo, _partial = _make_repo_datapoint(tmp_path)
    (repo / "generated.c").write_text("/* built when the image was made */\n", encoding="utf-8")

    proc = _run_container_apply(repo, _GENERATED_FILE_ENTRY, tmp_path)

    assert proc.returncode == 0
    assert "COMMAND-RAN" in proc.stdout
    assert (repo / "generated.c").read_text(encoding="utf-8") == "/* autogenerated, do not edit */\n"


def test_checkout_backend_restores_patched_files_ahead_of_base(monkeypatch, tmp_path: Path) -> None:
    """Some images sit an environment-fix commit ahead of base_commit; the base-relative patch must still apply."""
    repo, partial = _make_repo_datapoint(tmp_path)
    (repo / "value.txt").write_text("env-fixed\n", encoding="utf-8")
    _run_git(repo, "commit", "-am", "Fix environment")
    monkeypatch.setattr(execution, "get_parser", lambda _partial: _stub_parser)

    filled = execute_row(
        partial=partial,
        backend=_checkout_backend(repo, tmp_path),
        artifacts_dir=tmp_path / "artifacts",
        timeout=30,
    )

    assert filled.passed_tests == ["test_value"]


def test_execute_worker_row_writes_failure_for_patch_restore(monkeypatch, tmp_path: Path) -> None:
    """A patch naming a file the base commit does not have fails at the restore, reported apart from patch_apply."""
    repo, partial = _make_repo_datapoint(tmp_path)
    bad_patch = "diff --git a/missing.txt b/missing.txt\n--- a/missing.txt\n+++ b/missing.txt\n@@ -1 +1 @@\n-a\n+b\n"
    partial = partial.model_copy(update={"patch": bad_patch})
    monkeypatch.setattr(execution, "get_parser", lambda _partial: _stub_parser)
    in_path = tmp_path / "input.jsonl"
    in_path.write_text(partial.model_dump_json() + "\n", encoding="utf-8")

    outcome = execute_worker_row(
        input_path=in_path,
        instance_id=partial.instance_id,
        backend=_checkout_backend(repo, tmp_path),
        out_path=tmp_path / "execute.json",
        artifacts_dir=tmp_path / "execute-artifacts",
        timeout=30,
    )
    assert not outcome.success
    failure = json.loads((tmp_path / "execute-artifacts" / "failure.json").read_text(encoding="utf-8"))
    assert failure["category"] == "row"
    assert failure["stage"] == "patch"
    assert failure["reason"] == "patch_restore"


def test_execute_cli_writes_failure_for_missing_parser(tmp_path: Path) -> None:
    repo, partial = _make_repo_datapoint(tmp_path)
    partial = partial.model_copy(update={"instance_id": "unknown__deadbeef__parser"})
    in_path = tmp_path / "input.jsonl"
    in_path.write_text(partial.model_dump_json() + "\n", encoding="utf-8")

    execute_result = CliRunner().invoke(
        app,
        [
            "execute",
            "--input",
            str(in_path),
            "--instance-id",
            partial.instance_id,
            "--repo-dir",
            str(repo),
            "--out",
            str(tmp_path / "execute.json"),
            "--artifacts-dir",
            str(tmp_path / "execute-artifacts"),
            "--timeout",
            "30",
        ],
    )

    assert execute_result.exit_code == 1, execute_result.output
    failure = json.loads((tmp_path / "execute-artifacts" / "failure.json").read_text(encoding="utf-8"))
    assert failure["category"] == "runner"
    assert failure["stage"] == "parse"
    assert failure["reason"] == "parser_unavailable"


def test_execute_writes_failure_artifact_when_row_selection_fails(tmp_path: Path) -> None:
    input_path = tmp_path / "input.jsonl"
    input_path.write_text("", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    artifacts_dir = tmp_path / "artifacts"

    result = CliRunner().invoke(
        app,
        [
            "execute",
            "--input",
            str(input_path),
            "--instance-id",
            "missing",
            "--repo-dir",
            str(repo),
            "--out",
            str(tmp_path / "execute.json"),
            "--artifacts-dir",
            str(artifacts_dir),
            "--timeout",
            "30",
        ],
    )

    assert result.exit_code == 1, result.output
    failure = json.loads((artifacts_dir / "failure.json").read_text(encoding="utf-8"))
    assert failure["category"] == "runner"
    assert failure["stage"] == "select"
    assert failure["reason"] == "selection"


def test_execute_writes_failure_artifact_when_result_write_fails(tmp_path: Path) -> None:
    _repo, partial = _make_repo_datapoint(tmp_path)
    partial = partial.model_copy(
        update={
            "passed_tests": [],
            "failed_tests": [],
            "skipped_tests": [],
            "errored_tests": [],
            "discovery_errors": [],
        }
    )
    input_path = tmp_path / "input.jsonl"
    input_path.write_text(partial.model_dump_json() + "\n", encoding="utf-8")
    out_path = tmp_path / "execute.json"
    out_path.mkdir()
    artifacts_dir = tmp_path / "artifacts"

    result = CliRunner().invoke(
        app,
        [
            "execute",
            "--input",
            str(input_path),
            "--instance-id",
            partial.instance_id,
            "--repo-dir",
            str(tmp_path / "repo"),
            "--out",
            str(out_path),
            "--artifacts-dir",
            str(artifacts_dir),
            "--timeout",
            "30",
        ],
    )

    assert result.exit_code == 1, result.output
    failure = json.loads((artifacts_dir / "failure.json").read_text(encoding="utf-8"))
    assert failure["category"] == "runner"
    assert failure["stage"] == "write"
    assert failure["reason"] == "runner_error"
    assert "Is a directory" in (artifacts_dir / "error.txt").read_text(encoding="utf-8")


def test_execute_reports_unwritable_artifacts_dir_clearly(tmp_path: Path) -> None:
    """A worker that cannot write its outputs at all must say so in one clear line, not a traceback."""
    blocker = tmp_path / "blocker"
    blocker.write_text("", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "execute",
            "--input",
            str(tmp_path / "input.jsonl"),
            "--instance-id",
            "repo__row__x",
            "--repo-dir",
            str(tmp_path),
            "--out",
            str(tmp_path / "execute.json"),
            "--artifacts-dir",
            str(blocker / "artifacts"),
            "--timeout",
            "30",
        ],
    )

    assert result.exit_code == 1, result.output
    assert "cannot write worker outputs" in result.stderr
    assert "uid=" in result.stderr


def _gcs_worker_inputs(partial_row) -> tuple[FakeGcs, dict[str, str]]:
    """A FakeGcs holding one input row plus the worker's gs:// URIs."""
    row = partial_row("toy__deadbeef__rowA")
    fake = FakeGcs({"gs://bucket/run/input/rows.jsonl": (json.dumps(row) + "\n").encode("utf-8")})
    uris = {
        "input_uri": "gs://bucket/run/input/rows.jsonl",
        "out_uri": "gs://bucket/run/workers/toy__deadbeef__rowA/result.json",
        "artifacts_uri": "gs://bucket/run/workers/toy__deadbeef__rowA/artifacts",
    }
    return fake, uris


def test_execute_worker_row_gcs_uploads_result_last(monkeypatch, partial_row) -> None:
    """`k8s download` treats result.json as the completion marker, so every
    artifact must be durable before it appears."""
    monkeypatch.setattr(execution, "get_parser", lambda _partial: _stub_parser)
    fake, uris = _gcs_worker_inputs(partial_row)

    outcome = execution.execute_worker_row_gcs(
        instance_id="toy__deadbeef__rowA",
        backend=_SucceedingBackend(),
        timeout=30,
        gcs=fake,
        **uris,
    )

    assert outcome.success
    assert fake.write_order[-1] == uris["out_uri"]
    result = json.loads(fake.objects[uris["out_uri"]].decode("utf-8"))
    assert result["instance_id"] == "toy__deadbeef__rowA"
    assert result["passed_tests"] == ["test_ok"]


def test_execute_worker_row_gcs_uploads_failure_last(monkeypatch, partial_row) -> None:
    monkeypatch.setattr(execution, "get_parser", lambda _partial: _stub_parser)
    fake, uris = _gcs_worker_inputs(partial_row)

    outcome = execution.execute_worker_row_gcs(
        instance_id="toy__deadbeef__rowA",
        backend=_FailingBackend(),
        timeout=30,
        gcs=fake,
        **uris,
    )

    assert not outcome.success
    assert outcome.failure_category == "row"
    failure_uri = f"{uris['artifacts_uri']}/failure.json"
    assert fake.write_order[-1] == failure_uri
    assert uris["out_uri"] not in fake.objects
    failure = json.loads(fake.objects[failure_uri].decode("utf-8"))
    assert failure["instance_id"] == "toy__deadbeef__rowA"
    assert failure["reason"] == "patch_apply"
    assert f"{uris['artifacts_uri']}/partial.json" in fake.objects
    assert f"{uris['artifacts_uri']}/error.txt" in fake.objects


class _SucceedingBackend:
    """Backend writing the stub-parser report with one passing test."""

    def run(self, row, results_dir: Path, timeout: int) -> execution.CommandResult:
        results_dir.mkdir(parents=True, exist_ok=True)
        (results_dir / "stub.json").write_text(
            json.dumps({"tests": [{"name": "test_ok", "status": "PASSED"}]}),
            encoding="utf-8",
        )
        return execution.CommandResult(exit_code=0, elapsed_seconds=0.1)


class _FailingBackend:
    """Backend raising a patch-apply failure (a category "row" failure)."""

    def run(self, row, results_dir: Path, timeout: int) -> execution.CommandResult:
        raise execution.PatchApplyFailure("patch did not apply")


def test_execute_cli_rejects_mixed_gs_and_local_paths(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "execute",
            "--input",
            "gs://bucket/run/input/rows.jsonl",
            "--instance-id",
            "toy__deadbeef__rowA",
            "--repo-dir",
            str(tmp_path),
            "--out",
            str(tmp_path / "result.json"),
            "--artifacts-dir",
            "gs://bucket/run/workers/toy__deadbeef__rowA/artifacts",
            "--timeout",
            "30",
        ],
    )

    assert result.exit_code == 2
    assert "all gs:// URIs or all local paths" in result.stderr


def test_copy_directory_contents_needs_no_destination_metadata_writes(monkeypatch, tmp_path: Path) -> None:
    """gcsfuse rejects chmod/utime for non-root users, so the artifact copy
    must transfer file contents without preserving metadata."""
    source = tmp_path / "source"
    (source / "nested").mkdir(parents=True)
    (source / "results.xml").write_text("<xml/>", encoding="utf-8")
    (source / "nested" / "log.txt").write_text("log", encoding="utf-8")
    destination = tmp_path / "destination"

    def reject(*args: object, **kwargs: object) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "chmod", reject)
    monkeypatch.setattr(os, "utime", reject)

    execution._copy_directory_contents(source, destination)

    assert (destination / "results.xml").read_text(encoding="utf-8") == "<xml/>"
    assert (destination / "nested" / "log.txt").read_text(encoding="utf-8") == "log"


def test_k8s_cli_lists_collect_subcommands() -> None:
    result = CliRunner().invoke(app, ["k8s", "--help"])

    assert result.exit_code == 0
    assert "build" in result.output
    assert "collect" in result.output
    assert "download" in result.output
    assert "status" in result.output
    assert "cancel" in result.output
