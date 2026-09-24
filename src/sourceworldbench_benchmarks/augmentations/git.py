"""Git helpers for augmentation worktrees."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

_COMMIT_BLOCK = "===COMMIT==="
_LOG_FORMAT = f"{_COMMIT_BLOCK}\n%H\n%P\n%B"


def git(repo: Path, *args: str, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a git command against `repo`."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=capture,
        text=True,
    )


def is_git_worktree(repo: Path) -> bool:
    """Return True when `repo` is inside a git working tree."""
    result = git(repo, "rev-parse", "--is-inside-work-tree", check=False)
    return result.returncode == 0 and result.stdout.strip() == "true"


def status_porcelain(repo: Path) -> str:
    """Return porcelain git status output for `repo`."""
    return git(repo, "status", "--porcelain").stdout


def current_checkout(repo: Path) -> str:
    """Return the current branch name, or HEAD commit when detached."""
    branch = git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if branch.returncode == 0:
        return branch.stdout.strip()
    return rev_parse(repo, "HEAD")


def rev_parse(repo: Path, revision: str) -> str:
    """Resolve `revision` to a commit hash."""
    return git(repo, "rev-parse", "--verify", revision).stdout.strip()


def checkout(repo: Path, revision: str) -> None:
    """Check out `revision` in `repo`."""
    git(repo, "checkout", "--quiet", revision, capture=False)


def capture_diff(repo: Path, scope: Sequence[str]) -> str:
    """Return a diff for all changed files, optionally restricted to `scope`."""
    git(repo, "add", "--all")
    args = ["diff", "--staged", "--no-color"]
    if scope:
        args += ["--", *scope]
    return git(repo, *args).stdout


def changed_paths(repo: Path) -> list[str]:
    """Return tracked and untracked paths changed in `repo`."""
    tracked = git(repo, "diff", "--name-only").stdout.splitlines()
    untracked = git(repo, "ls-files", "--others", "--exclude-standard").stdout.splitlines()
    return [path for path in (*tracked, *untracked) if path]


def restore_worktree(repo: Path) -> None:
    """Reset tracked changes and remove untracked files from `repo`."""
    git(repo, "reset", "--quiet", "HEAD", "--", ".")
    git(repo, "checkout", "--", ".")
    git(repo, "clean", "-fd")


def _log_commits(repo: Path, branch: str = "main") -> list[dict]:
    """Return commits on `branch`, oldest first.

    Each entry has ``sha``, ``message``, and ``parents`` (list of parent SHAs).
    """
    proc = git(
        repo,
        "log",
        branch,
        "--reverse",
        f"--format={_LOG_FORMAT}",
    )
    commits: list[dict] = []
    for block in proc.stdout.split(f"{_COMMIT_BLOCK}\n"):
        block = block.strip("\n")
        if not block:
            continue
        lines = block.split("\n", 2)
        sha = lines[0]
        parents = lines[1].split() if len(lines) > 1 and lines[1] else []
        message = lines[2] if len(lines) > 2 else ""
        commits.append(
            {
                "sha": sha,
                "message": message.rstrip("\n"),
                "parents": parents,
            }
        )
    return commits


def load_commits(report: Path) -> list[dict]:
    """Read a git history report and return its ``commits`` list."""
    if not report.exists():
        raise ValueError(f"report does not exist: {report}")
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read report {report}: {exc}") from exc

    commits = data.get("commits") if isinstance(data, dict) else None
    if not isinstance(commits, list) or not commits:
        raise ValueError(f"report {report} has no 'commits' list")
    return commits


def build_git_history(
    repo: Path,
    out: Path,
    *,
    branch: str = "main",
) -> list[dict]:
    """Write a ``reports/repo_git_history/<name>__main.json``-style report to `out`.

    Returns the commits list (same as written under ``commits``).
    """
    commits = _log_commits(repo, branch)
    if not commits:
        raise ValueError(f"no commits found on {branch!r} in {repo}")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"commits": commits}, indent=2) + "\n",
        encoding="utf-8",
    )
    return commits
