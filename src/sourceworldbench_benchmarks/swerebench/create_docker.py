"""Generate sourceworldbench-benchmarks environment files from a SWE-rebench datapoint."""

import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sourceworldbench_benchmarks.swerebench.patch_apply import (
    DOCKER_PLATFORM,
    SWE_REBENCH_REPO_DIR,
    _image_ref,
    dockerfile_apply_command,
    pull_image,
    resolve_apply_attempt,
)

SOURCEWORLDBENCH_WORKDIR = "/app"
SWE_REBENCH_CONDA_ENV = "testbed"

# Conda installation root inside the eval image.
# SWE-rebench images: /opt/conda
# SWE-bench official images: /opt/miniconda3
# swefficiency images: /opt/miniconda3
CONDA_BASE_REBENCH = "/opt/conda"
CONDA_BASE_SWEB = "/opt/miniconda3"

DOCKERFILE_FILENAME = "Dockerfile"
RUN_TESTS_FILENAME = "run_tests.sh"


def create_dockerimage_from_rebench(
    datapoint: Mapping[str, Any],
    *,
    patches: dict[str, str],
    base_stem: str,
    checkout: str | None = None,
    repo_root: Path | None = None,
    skip_pull: bool = False,
    pull_timeout: int = 600,
    apply_timeout: int = 120,
    conda_base: str = CONDA_BASE_REBENCH,
) -> Path:
    """Write a fixed-state SWE-rebench environment under ``environments/<base_stem>/``.

    ``patches`` maps patch name (e.g. ``"test_patch"``, ``"patch"``) to patch
    content.  Pass an empty dict for an image with no patches.

    ``checkout`` optionally names a commit to ``git checkout -f`` before applying
    any patches, materialising the golden_base state directly from its commit.  The eval
    image is usually a full clone, but the golden merge commit is not always present
    locally (e.g. astropy fails with ``fatal: reference is not a tree``), and the
    clone has no ``origin`` remote to fall back on, so the checkout first
    ``git fetch``es the commit on demand — only when it is missing — from the repo's
    GitHub URL (derived from ``datapoint['repo']``) before checking it out.

    ``conda_base`` is the root of the conda installation inside the eval image.
    Default ``/opt/conda`` is correct for SWE-rebench images; pass ``/opt/miniconda3``
    for SWE-bench / swefficiency images.

    Rebuilding after a patch is handled at collect-time by
    :func:`sourceworldbench_benchmarks.execution._run_rebuild`, which reads the row's
    ``metadata[REBUILD_COMMAND_METADATA_KEY]``. This function therefore only
    bakes the patch and does not run any rebuild inside the image.

    Creates:
      - ``Dockerfile`` extending the pre-built eval image
      - ``<name>.diff`` for each entry in ``patches``
      - ``run_tests.sh``

    Raises ``RuntimeError`` when pulling or patch application fails.
    Returns the environment directory path.
    """
    root = Path.cwd() if repo_root is None else Path(repo_root).resolve()
    env_dir = root / "environments" / base_stem
    env_dir.mkdir(parents=True, exist_ok=True)

    docker_image = str(datapoint["docker_image"])
    copy_prefix = f"environments/{base_stem}"

    if not skip_pull:
        pull_error = pull_image(docker_image, timeout=pull_timeout)
        if pull_error is not None:
            raise RuntimeError(f"docker pull failed for {docker_image}: {pull_error}")

    patch_attempts: dict[str, int] = {}
    for name, content in patches.items():
        patch_path = env_dir / f"{name}.diff"
        patch_path.write_text(content)
        attempt = resolve_apply_attempt(
            docker_image=docker_image,
            patch_path=patch_path,
            apply_timeout=apply_timeout,
            skip_pull=True,
        )
        if attempt is None:
            raise RuntimeError(f"all patch apply attempts failed for {name} ({datapoint['instance_id']})")
        patch_attempts[name] = attempt

    checkout_url = f"https://github.com/{datapoint['repo']}" if checkout else None
    (env_dir / DOCKERFILE_FILENAME).write_text(
        _render_dockerfile(
            docker_image=docker_image,
            copy_prefix=copy_prefix,
            patch_attempts=patch_attempts,
            checkout=checkout,
            checkout_url=checkout_url,
            conda_base=conda_base,
        )
    )

    test_cmd = str(datapoint["install_config"]["test_cmd"])
    run_tests = env_dir / RUN_TESTS_FILENAME
    run_tests.write_text(_render_run_tests(test_cmd, conda_base=conda_base))
    run_tests.chmod(0o755)

    return env_dir


def _render_dockerfile(
    *,
    docker_image: str,
    copy_prefix: str,
    patch_attempts: dict[str, int],
    checkout: str | None = None,
    checkout_url: str | None = None,
    conda_base: str = CONDA_BASE_REBENCH,
) -> str:
    lines: list[str] = [
        "# syntax=docker/dockerfile:1.7",
        "",
        f"FROM --platform={DOCKER_PLATFORM} {_image_ref(docker_image)}",
    ]

    if patch_attempts:
        lines.append("")
        for name in patch_attempts:
            lines.append(f"COPY {copy_prefix}/{name}.diff /tmp/{name}.diff")

    # Create the non-root user (UID/GID 65532) that k8s enforces via
    # securityContext.runAsUser as a dedicated cacheable layer.
    lines += [
        "",
        "RUN groupadd --gid 65532 runner \\",
        "    && useradd --create-home --shell /bin/bash --uid 65532 --gid 65532 runner",
        "",
        f"RUN ln -s {SWE_REBENCH_REPO_DIR} {SOURCEWORLDBENCH_WORKDIR}",
        "",
        f"WORKDIR {SOURCEWORLDBENCH_WORKDIR}",
    ]

    run_cmds: list[str] = []
    if checkout:
        run_cmds.append(f"(git cat-file -e {checkout} 2>/dev/null || git fetch --no-tags {checkout_url} {checkout})")
        run_cmds.append(f"git checkout -f {checkout}")
    run_cmds += [
        dockerfile_apply_command(attempt, f"/tmp/{name}.diff")
        for name, attempt in patch_attempts.items()
    ]
    if patch_attempts:
        cleanup = " ".join(f"/tmp/{name}.diff" for name in patch_attempts)
        run_cmds.append(f"rm -f {cleanup}")
    # /results must be writable for test output; the source tree must be
    # writable so the k8s worker can `git reset --hard` between rows and
    # augmentations can `git apply` + rebuild in-place (all as UID 65532).
    run_cmds += [
        "mkdir -p /results",
        f"chown -R runner:runner {SWE_REBENCH_REPO_DIR} /results",
    ]

    lines.append("RUN " + " \\\n    && ".join(run_cmds))
    lines += ["", "USER runner"]

    return "\n".join(lines) + "\n"


def render_test_command(
    test_cmd: str,
    *,
    workdir: str = SOURCEWORLDBENCH_WORKDIR,
    conda_base: str = CONDA_BASE_REBENCH,
    use_bash: bool = False,
) -> str:
    """Return a one-line shell command for running tests in a container.

    ``workdir`` is the directory the command ``cd``s into before running the tests.
    It defaults to the ``/app`` symlink our environment images add; callers that run
    against the raw eval image (which has no such symlink) pass the real repo dir
    (``SWE_REBENCH_REPO_DIR``) instead.

    ``conda_base`` is the root of the conda install (``/opt/conda`` for rebench,
    ``/opt/miniconda3`` for SWE-bench / swefficiency).

    ``use_bash`` runs the activation and the tests under ``bash`` instead of the
    runner's ``/bin/sh``. ``conda activate`` sources the activation hooks shipped by
    the environment's packages *into the calling shell*, so those hooks get whatever
    shell we chose — and they are written for bash. conda-forge's older
    ``activate-binutils_linux-64.sh`` (present in scipy's toolchain environments)
    opens with a bash-only ``function name()`` declaration, which dash cannot parse;
    a syntax error in a sourced file aborts the whole shell, so the tests never run.
    Both halves must be inside the same ``bash -c``: activation only affects the
    shell it runs in.
    """
    cmd = f"{test_cmd} --junitxml=/results/junit.xml --continue-on-collection-errors"
    activate_and_run = f". {conda_base}/etc/profile.d/conda.sh && conda activate {SWE_REBENCH_CONDA_ENV} && {cmd}"
    if use_bash:
        activate_and_run = f"bash -c {shlex.quote(activate_and_run)}"
    return f"mkdir -p /results && cd {workdir} && {activate_and_run}"


def _render_run_tests(test_cmd: str, *, conda_base: str = CONDA_BASE_REBENCH) -> str:
    return f"#!/usr/bin/env bash\nset -euo pipefail\n\n{render_test_command(test_cmd, conda_base=conda_base)}\n"
