"""Bridge between swefficiency dataset rows and the sourceworldbench-benchmarks environment pipeline.

swefficiency (https://huggingface.co/datasets/swefficiency/swefficiency) is a
performance benchmark. Compared to SWE-rebench / SWE-bench Verified:

- ``test_patch`` is empty for every row (no correctness tests are added)
- ``FAIL_TO_PASS`` is empty for every row (no fail→pass polarity)
- ``PASS_TO_PASS`` is a regression list — both base and fixed should pass it
- ``test_cmd`` and ``image_name`` are provided directly (no swebench harness needed)
- ``rebuild_cmd`` is provided per row (e.g. pandas needs ``--no-build-isolation``)

Rows use conda at ``/opt/miniconda3`` inside the eval image.
"""

from __future__ import annotations

import json
import re
import shlex
from typing import Any

from sourceworldbench_benchmarks.swerebench.create_docker import CONDA_BASE_SWEB, SWE_REBENCH_CONDA_ENV

# Repo -> (env prefix, pytest flags). swefficiency strips the projects' xdist
# flags. but we run the full suite and running this sequentially takes too long,
# so put the CI parallelism back
# ``-rN`` drops the terminal summary that we never read, and takes very long time to
# create for large test suites like pandas
#
# For pandas: older commits have ``--strict-data-files`` in their ``setup.cfg``
# ``addopts`` but the flag is only recognized once pandas registers its own pytest
# plugin (introduced later). The command substitution probes at runtime and injects
# ``--override-ini=addopts=`` when the flag is unrecognized, suppressing the
# incompatible inifile option without affecting newer commits where it works fine.
_PANDAS_ADDOPTS_PROBE = (
    "$(pytest --strict-data-files /dev/null --co -q >/dev/null 2>&1; "
    "[ $? -eq 4 ] && echo '--override-ini=addopts=')"
)

_CI_XDIST_FLAGS = {
    "pandas-dev/pandas": (
        "PYTEST_XDIST_AUTO_NUM_WORKERS=8 ",
        # --max-worker-restart caps the crash-replace loop: tests from dead workers
        # are recorded as errors in the JUnit XML rather than looping indefinitely.
        f"{_PANDAS_ADDOPTS_PROBE} -n auto --dist=loadfile --max-worker-restart=4 -rN pandas/tests",
    ),
    "dask/dask": ("", "-n4 -rN"),
}

_XDIST_WORKERS_FLAG = re.compile(r"\s-n\s?\S+")


def _with_ci_xdist_flags(repo: str, test_cmd: str) -> str:
    """Replace the dataset's serial worker count with the project's CI one."""
    if repo not in _CI_XDIST_FLAGS:
        return test_cmd
    env_prefix, flags = _CI_XDIST_FLAGS[repo]
    return f"{env_prefix}{_XDIST_WORKERS_FLAG.sub('', test_cmd)} {flags}"


# Probves if xdist is present in the environment, and if yes, inject the flags. 
_XDIST_PROBE = (
    "$(python -c 'import xdist' 2>/dev/null && echo '-n 4 --max-worker-restart=4')"
)


def _with_xdist_probe(repo: str, test_cmd: str) -> str:
    """Inject the xdist probe into the pytest invocation for non-xdist repos.

    Repos already listed in ``_CI_XDIST_FLAGS`` (pandas, dask) have their own
    parallelism config and crash-restart settings
    """
    if repo in _CI_XDIST_FLAGS or repo in _TEST_CMD_OVERRIDES:
        return test_cmd
    return test_cmd.replace("pytest ", f"pytest {_XDIST_PROBE} ", 1)


# specific scopes for repos
_TEST_SCOPE: dict[str, str] = {
    "numpy/numpy": "numpy/", # there was errors caused by c tests  outside of numpy 
}


def _with_test_scope(repo: str, test_cmd: str) -> str:
    """Append the repo's test scope path if not already present in the command."""
    scope = _TEST_SCOPE.get(repo)
    if not scope or scope.rstrip("/") in test_cmd:
        return test_cmd
    return test_cmd.rstrip() + f" {scope}"


# Repo -> replacement test command, for repos whose dataset runner cannot emit the
# JUnit XML :func:`create_docker.render_test_command` asks for.
#
# swefficiency runs sympy through its own ``bin/test``, which never honours
# ``--junitxml``: pre-1.12 commits reject the flag outright (``test: error: no such
# option: --junitxml``, exit 2), and newer commits delegate to pytest but filter the
# argument list, so the flag is dropped and the suite runs to completion writing no
# report. Either way the row fails, so drive pytest directly instead — the same
# command the other pytest repos already get, plus the dataset's warning filters.
#
# ``sympy`` is passed as an explicit path because only newer commits set
# ``testpaths``; without it, older commits collect ``doc/`` and ``examples/`` too.
#
# The sympy images never shipped pytest (``bin/test`` needs none), so the
# command installs it first — into the user site, whose scripts dir is off
# PATH, hence ``python -m pytest`` rather than ``pytest``. pip resolves the
# newest pytest the env's python supports (7.0.1 on the py3.6 images). The
# install lives in the test command, not the rebuild: base rows have no
# rebuild step.
_TEST_CMD_OVERRIDES = {
    "sympy/sympy": (
        "python -m pip install pytest 1>&2 && "
        "PYTHONWARNINGS='ignore::UserWarning,ignore::SyntaxWarning' "
        "python -m pytest --no-header -rA --tb=no -p no:cacheprovider "
        "--continue-on-collection-errors sympy"
    ),
}


def _with_supported_runner(repo: str, test_cmd: str) -> str:
    """Swap out dataset runners that cannot produce a parseable report."""
    return _TEST_CMD_OVERRIDES.get(repo, test_cmd)


def _repo_leaf(repo: str) -> str:
    return repo.split("/")[-1]


def _short(commit: str) -> str:
    return commit[:8]


def base_stem_for(dp: dict) -> str:
    """`<repo-leaf>__<commit8>` — Docker image stem shared by base + augmentations."""
    return f"{_repo_leaf(dp['repo'])}__{_short(dp['base_commit'])}"


def iid_base(dp: dict) -> str:
    """Instance-id for the base (unfixed) state."""
    return f"{base_stem_for(dp)}__{dp["instance_id"]}__base"


def iid_fixed(dp: dict) -> str:
    """Instance-id for the fixed (augmentation) state."""
    return f"{base_stem_for(dp)}__{dp["instance_id"] }__fixed"


# Compiler flags needed when rebuilding scipy C/Fortran extensions.
# GCC 10+ changed -fcommon default; gfortran 10+ made argument-type mismatches
# a hard error. Prepending these ensures builds work across old and new scipy
# commits without relying on the specific GCC version in the eval image.
_SCIPY_CFLAGS = (
    'export CFLAGS="-fcommon"; '
    'export FFLAGS="-fcommon -fallow-argument-mismatch -fPIC"; '
    'export FCFLAGS="-fcommon -fallow-argument-mismatch -fPIC"'
)


def wrap_rebuild_cmd(dp: dict) -> str:
    """Return the conda-wrapped rebuild command stored in augmentation metadata.

    Execution reads ``metadata['rebuild_command']`` verbatim, so the conda
    activation must be baked in here at datapoint-creation time.

    For scipy, compiler flags are prepended to the rebuild command to handle
    GCC/gfortran version differences across eval images.
    """
    conda_base = dp.get("_conda_base", CONDA_BASE_SWEB)
    raw = dp["_rebuild_cmd"]
    if dp.get("repo") == "scipy/scipy":
        raw = f"{_SCIPY_CFLAGS}; {raw}"
    # Build isolation makes pip resolve build deps fresh from the network; in old
    # envs (astropy on py3.7) that mixes incompatible versions (jinja2/markupsafe)
    # and the rebuild dies before compiling. The baked env IS the build
    # environment, so always build against it.
    raw = f"export PIP_NO_BUILD_ISOLATION=1; {raw}"
    return (
        f". {conda_base}/etc/profile.d/conda.sh "
        f"&& conda run -n {SWE_REBENCH_CONDA_ENV} bash -c {shlex.quote(raw)}"
    )


def sweff_row_to_dp(row: dict[str, Any]) -> dict[str, Any]:
    """Adapt a swefficiency dataset row to the format expected by
    ``create_dockerimage_from_rebench``.

    Injects the fields the rebench pipeline expects:

    - ``docker_image``   — from ``row["image_name"]`` (ghcr.io registry)
    - ``install_config`` — ``{"test_cmd": <row test_cmd>}``, replaced outright for
      repos listed in :data:`_TEST_CMD_OVERRIDES` and with the project's CI xdist
      flags substituted in for those listed in :data:`_CI_XDIST_FLAGS`
    - ``_conda_base``    — ``/opt/miniconda3``
    - ``_rebuild_cmd``   — from ``row["rebuild_cmd"]`` (project-specific)

    Also normalises ``FAIL_TO_PASS`` / ``PASS_TO_PASS`` from JSON strings to
    actual Python lists.
    """

    def _parse_list(v: Any) -> list[str]:
        if isinstance(v, str):
            return json.loads(v)
        return list(v) if v else []

    return {
        **row,
        "FAIL_TO_PASS": _parse_list(row.get("FAIL_TO_PASS")),
        "PASS_TO_PASS": _parse_list(row.get("PASS_TO_PASS")),
        "docker_image": row["image_name"],
        "install_config": {
            "test_cmd": _with_test_scope(
                row["repo"],
                _with_xdist_probe(
                    row["repo"],
                    _with_ci_xdist_flags(row["repo"], _with_supported_runner(row["repo"], row["test_cmd"])),
                ),
            ),
        },
        "_conda_base": CONDA_BASE_SWEB,
        "_rebuild_cmd": row["rebuild_cmd"],
    }
