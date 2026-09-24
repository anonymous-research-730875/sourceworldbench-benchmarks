"""Detect SWE-rebench eval images that ship an "environment patch".

A SWE-rebench eval image is a git clone at the rebench base commit, but its working
tree is not always clean: some images carry *uncommitted* modifications to tracked
files — environment-compatibility patches that make the historical source runnable
under the image's pinned dependencies. astropy is the motivating case: its eval image
rewrites a call to the numpy-2.0-removed ``np.in1d`` into ``np.isin`` in the working
tree so the source imports under the installed numpy 2.4.4.

This breaks the golden_base state, which is materialised with
``git checkout -f <golden_commit>`` (see :func:`create_dockerimage_from_rebench`):
a hard checkout **discards** the working-tree patch, reverting to the pristine golden_base
tree, which then fails to import/collect. Crucially, the golden-commit diff
verification (see :func:`identify_golden_commit`) cannot catch this — it compares the
content change between two *commits*, and the environment patch lives in no commit, so
verification passes even though the resulting golden_base image is silently broken.

:func:`detect_environment_patch` is the preliminary guard: it inspects the eval image's
working tree and reports whether such a patch is present, so the pipeline can refuse to
integrate the datapoint and log a clear reason instead of producing a broken golden_base image
whose test run fails with an opaque ``missing_report``.
"""

import logging
from enum import Enum

from sourceworldbench_benchmarks.swerebench.patch_apply import (
    DOCKER_PLATFORM,
    SWE_REBENCH_REPO_DIR,
    _image_ref,
    _proc_output,
    _run_docker,
    pull_image,
)

logger = logging.getLogger(__name__)


class EnvironmentPatchError(str, Enum):
    """Why a rebench datapoint cannot be integrated via golden-commit checkout."""

    ENVIRONMENT_PATCH_PRESENT = "environment_patch_present"  # eval image ships a dirty working tree
    INSPECT_FAILED = "inspect_failed"  # the eval image could not be pulled or inspected


def detect_environment_patch(
    docker_image: str,
    *,
    skip_pull: bool = False,
    timeout: int = 120,
    pull_timeout: int = 600,
) -> EnvironmentPatchError | None:
    """Report whether the eval image's working tree carries an environment patch.

    Runs ``git status`` inside the eval image and looks for uncommitted changes to
    *tracked* files. Untracked files are ignored (``--untracked-files=no``): a hard
    checkout leaves them in place, so build artifacts an image happens to leave behind
    are not false positives — only tracked modifications would be silently discarded.

    Args:
        docker_image: The SWE-rebench eval image (``datapoint['docker_image']``).
        skip_pull: Skip ``docker pull`` when the image is already present locally.
        timeout: Timeout in seconds for the inspection container.
        pull_timeout: Timeout in seconds for the image pull.

    Returns:
        :attr:`EnvironmentPatchError.ENVIRONMENT_PATCH_PRESENT` when the working tree
        has tracked modifications (a golden-commit checkout would discard them),
        :attr:`EnvironmentPatchError.INSPECT_FAILED` when the image cannot be pulled or
        inspected, or ``None`` when the working tree is clean and checkout is safe.
    """
    if not skip_pull and pull_image(docker_image, timeout=pull_timeout) is not None:
        logger.warning("could not pull %s to inspect its working tree", docker_image)
        return EnvironmentPatchError.INSPECT_FAILED

    proc = _run_docker(
        [
            "docker", "run", "--rm", "--platform", DOCKER_PLATFORM,
            "-w", SWE_REBENCH_REPO_DIR,
            _image_ref(docker_image),
            "git", "status", "--porcelain", "--untracked-files=no",
        ],
        timeout=timeout,
    )
    if proc.returncode != 0:
        logger.warning("could not inspect working tree of %s: %s", docker_image, _proc_output(proc))
        return EnvironmentPatchError.INSPECT_FAILED

    changed = proc.stdout.decode("utf-8", errors="replace").strip()
    if changed:
        logger.warning(
            "%s ships an environment patch (dirty working tree); a golden-commit checkout would "
            "discard it, silently breaking the golden_base state. Modified tracked files:\n%s",
            docker_image, changed,
        )
        return EnvironmentPatchError.ENVIRONMENT_PATCH_PRESENT
    return None
