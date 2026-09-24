"""Tests for eval-image environment-patch detection (``swerebench.environment_patch``).

Docker is never invoked: the module's ``_run_docker`` and ``pull_image`` (imported from
``patch_apply``) are monkeypatched with canned :class:`subprocess.CompletedProcess`
results, so every branch is exercised deterministically.
"""

import subprocess

import pytest

from sourceworldbench_benchmarks.swerebench import environment_patch as ep
from sourceworldbench_benchmarks.swerebench.environment_patch import EnvironmentPatchError, detect_environment_patch

IMAGE = "swerebench/sweb.eval.x86_64.astropy_1776_astropy-17642"


def _proc(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["docker"], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def fake_docker(monkeypatch):
    """Stub pull_image (success) and capture the docker command _run_docker receives."""
    calls: dict[str, object] = {"pull_called": False, "run_args": None}

    def fake_pull(docker_image, *, timeout=600):
        calls["pull_called"] = True
        return None  # success

    def fake_run(args, *, timeout):
        calls["run_args"] = args
        return calls["proc"]

    monkeypatch.setattr(ep, "pull_image", fake_pull)
    monkeypatch.setattr(ep, "_run_docker", fake_run)
    calls["proc"] = _proc()  # default: clean tree; tests override
    return calls


def test_clean_working_tree_returns_none(fake_docker):
    fake_docker["proc"] = _proc(stdout=b"")
    assert detect_environment_patch(IMAGE) is None


def test_dirty_working_tree_flagged(fake_docker, caplog):
    fake_docker["proc"] = _proc(
        stdout=b" M astropy/units/quantity_helper/function_helpers.py\n M astropy/utils/masked/function_helpers.py\n"
    )
    with caplog.at_level("WARNING", logger=ep.logger.name):
        result = detect_environment_patch(IMAGE)
    assert result is EnvironmentPatchError.ENVIRONMENT_PATCH_PRESENT
    # The warning names the offending files so the cause is logged, not opaque.
    assert "function_helpers.py" in caplog.text
    assert "environment patch" in caplog.text.lower()


def test_whitespace_only_output_is_clean(fake_docker):
    fake_docker["proc"] = _proc(stdout=b"  \n \n")
    assert detect_environment_patch(IMAGE) is None


def test_inspect_nonzero_exit_is_inspect_failed(fake_docker):
    fake_docker["proc"] = _proc(returncode=128, stderr=b"fatal: not a git repository")
    assert detect_environment_patch(IMAGE) is EnvironmentPatchError.INSPECT_FAILED


def test_pull_failure_is_inspect_failed_without_running_container(monkeypatch):
    ran = {"run": False}

    monkeypatch.setattr(ep, "pull_image", lambda docker_image, *, timeout=600: "pull denied")
    monkeypatch.setattr(ep, "_run_docker", lambda *a, **k: ran.__setitem__("run", True) or _proc())

    assert detect_environment_patch(IMAGE) is EnvironmentPatchError.INSPECT_FAILED
    assert ran["run"] is False  # never tried to inspect a non-pulled image


def test_skip_pull_bypasses_pull(monkeypatch, fake_docker):
    def boom(*a, **k):
        raise AssertionError("pull_image must not be called when skip_pull=True")

    monkeypatch.setattr(ep, "pull_image", boom)
    fake_docker["proc"] = _proc(stdout=b"")
    assert detect_environment_patch(IMAGE, skip_pull=True) is None
    assert fake_docker["pull_called"] is False


def test_inspection_command_ignores_untracked_files(fake_docker):
    # Untracked files survive `git checkout -f`, so they must not count as a patch.
    detect_environment_patch(IMAGE)
    args = fake_docker["run_args"]
    assert args[-4:] == ["git", "status", "--porcelain", "--untracked-files=no"]
