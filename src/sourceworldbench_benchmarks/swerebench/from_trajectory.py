"""Compute trajectory augmentation patches and build StateDatapoint rows."""

import re
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from datasets import load_dataset

from sourceworldbench_benchmarks.schema import BASE_ID_METADATA_KEY, StateDatapoint
from sourceworldbench_benchmarks.swerebench.create_docker import SOURCEWORLDBENCH_WORKDIR
from sourceworldbench_benchmarks.swerebench.patch_apply import SWE_REBENCH_REPO_DIR, _run_docker

TRAJECTORY_DATASET = "nebius/SWE-rebench-openhands-trajectories"

_TEST_PATH_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?:^|/)tests?/"),
    re.compile(r"(?:^|/)test_[^/]+\.py$"),
    re.compile(r"(?:^|/)[^/]+_test\.py$"),
    re.compile(r"(?:^|/)conftest\.py$"),
]


def _is_test_path(path: str) -> bool:
    return any(pat.search(path) for pat in _TEST_PATH_PATTERNS)


def filter_test_modifications(patch: str) -> str:
    """Return *patch* with all hunks that modify test files removed."""
    if not patch:
        return patch
    sections = re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)
    kept: list[str] = []
    for section in sections:
        if not section.strip():
            continue
        m = re.search(r"^diff --git a/(.+?) b/(.+?)$", section, re.MULTILINE)
        if m and (_is_test_path(m.group(1)) or _is_test_path(m.group(2))):
            continue
        kept.append(section)
    return "".join(kept)


def _normalize_no_index_paths(diff: str) -> str:
    """Strip mount prefixes from ``git diff --no-index`` output.

    ``git diff --no-index /golden_app /testbed`` produces headers like
    ``a/golden_app/foo.py b/testbed/foo.py``. Rewrite both sides to repo-relative
    form so ``git apply`` works without ``-p`` adjustments.

    Sections without ``---``/``+++`` lines (binary files, mode-only changes)
    are dropped because ``git apply`` cannot handle them.
    """
    if not diff:
        return diff
    workdir = SWE_REBENCH_REPO_DIR.lstrip("/")  # real path, not the /app symlink
    golden = "golden_app"

    def _strip(prefix: str, path: str) -> str:
        p = f"{prefix}/"
        return path[len(p):] if path.startswith(p) else path

    def _is_skipped_path(path: str) -> bool:
        return path.startswith(".git/") or path == ".git"

    def _normalize_section(lines: list[str]) -> list[str]:
        has_triple_minus = any(line.startswith("--- ") for line in lines)
        has_triple_plus = any(line.startswith("+++ ") for line in lines)
        if not (has_triple_minus and has_triple_plus):
            return []
        out: list[str] = []
        for line in lines:
            if line.startswith("diff --git "):
                m = re.match(r"^diff --git a/(.+?) b/(.+?)(\n?)$", line)
                if m:
                    a = _strip(golden, m.group(1))
                    b = _strip(workdir, m.group(2))
                    if _is_skipped_path(a) or _is_skipped_path(b):
                        return []
                    out.append(f"diff --git a/{a} b/{b}{m.group(3)}")
                    continue
            elif line.startswith("--- a/"):
                out.append(f"--- a/{_strip(golden, line[6:].rstrip())}\n")
                continue
            elif line.startswith("+++ b/"):
                out.append(f"+++ b/{_strip(workdir, line[6:].rstrip())}\n")
                continue
            elif line.startswith("rename from "):
                out.append(f"rename from {_strip(golden, line[12:].rstrip())}\n")
                continue
            elif line.startswith("rename to "):
                out.append(f"rename to {_strip(workdir, line[10:].rstrip())}\n")
                continue
            out.append(line)
        return out

    sections: list[list[str]] = []
    current: list[str] = []
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git ") and current:
            sections.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append(current)

    return "".join(line for section in sections for line in _normalize_section(section))


def _ensure_trailing_newline(text: str) -> str:
    return text if not text or text.endswith("\n") else text + "\n"


_APPLY_PATCH = (
    "git apply --whitespace=nowarn {path} "
    "|| git apply --whitespace=nowarn --reject {path} "
    "|| patch --batch --fuzz=5 -p1 -i {path}"
)

def _diff_script_checkout(base_commit: str) -> str:
    """Diff script for the **main** path (golden_base built by checking out the golden commit).

    Snapshots the golden_base state, checks out ``base_commit`` to restore the exact
    pre-fix tree the trajectory was generated from, applies the trajectory patch,
    then re-applies the official ``test_patch`` so the trajectory state carries the
    same test suite as every other state derived from this environment.  Finally it
    diffs the snapshot vs /testbed; because ``test_patch`` is present on both sides
    it cancels out, so the emitted patch touches only non-test code.  The eval image
    is a full clone, so the base commit is available locally.  The diff targets the
    real repo path, not the /app symlink (``git diff --no-index`` treats a symlink
    target as a single file, not a directory, which would generate a completely
    wrong patch).  Non-diff output goes to stderr so only the patch reaches stdout.
    """
    return f"""\
set -euo pipefail
cp -a {SOURCEWORLDBENCH_WORKDIR}/. /golden_app/
rm -rf /golden_app/.git
cd {SOURCEWORLDBENCH_WORKDIR}
git checkout -f {base_commit} >&2
[ -s /sourceworldbench_patches/traj.diff ] && ({_APPLY_PATCH.format(path="/sourceworldbench_patches/traj.diff")}) >&2
[ -s /sourceworldbench_patches/test.diff ] && ({_APPLY_PATCH.format(path="/sourceworldbench_patches/test.diff")}) >&2
git diff --no-index /golden_app {SWE_REBENCH_REPO_DIR} || true
"""


def _diff_script_reverse_patch() -> str:
    """Diff script for the **fallback** path (golden_base built by applying patches).

    The fallback path materialises the golden_base state by applying the fix on top of the
    eval image **without a checkout**, so edge-case datapoints a checkout cannot handle can
    still be included in the dataset.  This restores the pre-fix tree the same way — by
    **reverse-applying the fix patch** instead of checking out ``base_commit`` — so it
    undoes only the fix and leaves the rest of the working tree (any uncommitted
    environment patch, and the already-applied ``test_patch``) untouched.  A checkout would
    instead wipe those from /testbed while the /golden_app snapshot keeps them, leaking
    spurious reverts into the diff.  With them present on both sides, they cancel out, so
    the emitted patch touches only non-test code.  ``test_patch`` is deliberately *not*
    re-applied here (unlike the checkout script): nothing rewinds past it.
    """
    return f"""\
set -euo pipefail
cp -a {SOURCEWORLDBENCH_WORKDIR}/. /golden_app/
rm -rf /golden_app/.git
cd {SOURCEWORLDBENCH_WORKDIR}
[ -s /sourceworldbench_patches/fix.diff ] && git apply --whitespace=nowarn -R /sourceworldbench_patches/fix.diff >&2
[ -s /sourceworldbench_patches/traj.diff ] && ({_APPLY_PATCH.format(path="/sourceworldbench_patches/traj.diff")}) >&2
git diff --no-index /golden_app {SWE_REBENCH_REPO_DIR} || true
"""


class TrajectoryDiffError(Exception):
    def __init__(self, message: str, *, stdout: str = "", stderr: str = "") -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.stdout:
            parts.append(f"stdout:\n{self.stdout}")
        if self.stderr:
            parts.append(f"stderr:\n{self.stderr}")
        return "\n".join(parts)


def compute_trajectory_patch(
    *,
    golden_base_image: str,
    model_patch: str,
    base_commit: str | None = None,
    fix_patch: str | None = None,
    test_patch: str | None = None,
    timeout: int = 300,
) -> str:
    """Run a golden_base container and return the patch from golden_base to trajectory state.

    Restores the pre-fix tree the trajectory was generated from, applies the trajectory's
    ``model_patch``, and diffs the golden_base snapshot against it.  Test-file
    modifications are filtered from ``model_patch`` first so the trajectory cannot alter
    the shared test suite.  Two mutually-exclusive restore strategies — pass exactly one:

    - ``base_commit`` (**main** path): ``git checkout`` the rebench pre-fix commit, then
      re-apply ``test_patch`` (checkout wiped it) so the trajectory state keeps the same
      suite.  ``test_patch`` is required in this mode.
    - ``fix_patch`` (**fallback** path): reverse-apply the fix patch — no checkout — so an
      environment patch (and the already-applied ``test_patch``) survive in the working
      tree.  See :func:`_diff_script_reverse_patch`.

    Returns an empty string when the trajectory produces no code changes.  Raises
    ``TrajectoryDiffError`` on container failure.
    """
    if (base_commit is None) == (fix_patch is None):
        raise ValueError("pass exactly one of base_commit (checkout) or fix_patch (reverse-patch)")
    filtered_patch = filter_test_modifications(model_patch)

    with tempfile.TemporaryDirectory(prefix="sourceworldbench-traj-diff-") as tmpdir:
        patch_dir = Path(tmpdir)
        (patch_dir / "traj.diff").write_text(filtered_patch, encoding="utf-8")
        if base_commit is not None:
            (patch_dir / "test.diff").write_text(test_patch or "", encoding="utf-8")
            script = _diff_script_checkout(base_commit)
        else:
            (patch_dir / "fix.diff").write_text(fix_patch or "", encoding="utf-8")
            script = _diff_script_reverse_patch()

        proc = _run_docker(
            [
                "docker", "run", "--rm",
                "--tmpfs", "/golden_app",
                "--mount", f"type=bind,src={patch_dir},dst=/sourceworldbench_patches,ro",
                golden_base_image, "bash", "-c", script,
            ],
            timeout=timeout,
        )

    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        raise TrajectoryDiffError(
            f"diff container exited with code {proc.returncode}",
            stdout=stdout.strip(),
            stderr=stderr.strip(),
        )

    diff = filter_test_modifications(_normalize_no_index_paths(stdout))
    return _ensure_trailing_newline(diff)


def trajectory_augmentation_row(
    golden_base_row: dict[str, Any],
    trajectory_dp: dict[str, Any],
    patch: str,
) -> dict[str, Any]:
    """Build a StateDatapoint dict for a trajectory augmentation.

    The augmentation inherits container, command, and test_scope from
    ``golden_base_row`` so it runs in the same image with the same test command.
    """
    golden_base_instance_id = str(golden_base_row["instance_id"])
    trajectory_id = str(trajectory_dp["trajectory_id"])
    rebench_id = str(trajectory_dp["instance_id"])
    golden_base_ri = dict((golden_base_row.get("metadata") or {}).get("rebench_integration") or {})

    # Follow the `<repo>__<base_commit>__<suffix>` convention: replace the
    # golden_base state's trailing `base` segment with the augmentation suffix.
    base_stem = "__".join(golden_base_instance_id.split("__")[:-1])

    instance_id = f"{base_stem}__rebench_trajectory_{trajectory_id}"
    row = StateDatapoint(
        instance_id=instance_id,
        repo=str(golden_base_row["repo"]),
        base_commit=golden_base_row["base_commit"],
        patch=patch,
        container=str(golden_base_row["container"]),
        command=str(golden_base_row["command"]),
        test_scope=list(golden_base_row["test_scope"]),
        metadata={
            # Carry the base-id so the state stays keyable when base_commit is None
            # (see sourceworldbench_benchmarks.schema.base_key); it matches the golden_base state's.
            BASE_ID_METADATA_KEY: instance_id.split("__")[1],
            # Inherit the golden_base state's rebench provenance (including its
            # integration_failures) and add this augmentation's own trajectory fields.
            "rebench_integration": {
                "dataset": golden_base_ri.get("dataset"),
                "rebench_instance_id": rebench_id,
                "golden_commit": golden_base_ri.get("golden_commit"),
                "rebench_base_commit": golden_base_ri.get("rebench_base_commit"),
                "parent": golden_base_instance_id,
                "trajectory_id": trajectory_id,
                "trajectory_dataset": TRAJECTORY_DATASET,
                "integration_failures": list(golden_base_ri.get("integration_failures") or []),
            }
        },
    )
    return row.model_dump()


def iter_trajectories(rebench_id: str) -> Iterator[dict[str, Any]]:
    """Yield all trajectories for the given rebench instance_id from the HF dataset."""
    ds = load_dataset(TRAJECTORY_DATASET, split="train")
    for row in ds:
        if row["instance_id"] == rebench_id:
            yield dict(row)


def augment_from_trajectory(
    golden_base_row: dict[str, Any],
    trajectory_dp: dict[str, Any],
    *,
    base_commit: str | None = None,
    fix_patch: str | None = None,
    test_patch: str | None = None,
    timeout: int = 300,
) -> dict[str, Any] | None:
    """Compute patch and return an augmentation row. Returns None if the diff is empty.

    Pass exactly one restore strategy, matching how the golden_base state was built:

    - ``base_commit`` (**main** path — golden_base built by checkout): the rebench
      pre-fix commit, checked out inside the container to restore the base state.
    - ``test_patch`` (**main** path — golden_base built by checkout): the rebench
      official test diff is re-applied on top so the
      augmentation keeps the same test suite.
    - ``fix_patch`` (**fallback** path — golden_base built by applying patches): the
      rebench ``patch`` (fix), reverse-applied to restore the base state without a
      checkout, preserving any environment patch. ``test_patch`` is not needed.
    """
    patch = compute_trajectory_patch(
        golden_base_image=str(golden_base_row["container"]),
        model_patch=str(trajectory_dp.get("model_patch") or ""),
        base_commit=base_commit,
        fix_patch=fix_patch,
        test_patch=test_patch,
        timeout=timeout,
    )
    if not patch.strip():
        return None
    return trajectory_augmentation_row(golden_base_row, trajectory_dp, patch)
