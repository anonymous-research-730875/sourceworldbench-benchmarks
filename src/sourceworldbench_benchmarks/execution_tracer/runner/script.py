"""Shell-script construction for a tracer-wrapped run.

The script that goes into `docker run -c <script>` for tracing is the
same as sourceworldbench-benchmarks' wrapped command — `git apply` the state's patch
(when present), run the row's `rebuild_command`, then the user's test
command — with one extra block prepended that activates the tracer
(PYTHONPATH/PYTEST_PLUGINS).

Patch-application env vars and the `exit 99` sentinel are inherited from
`sourceworldbench_benchmarks.execution.wrap_command` so the docker_runner's
`PatchApplyFailed` detection still fires.
"""

from __future__ import annotations

import re
import shlex
from typing import Literal

from sourceworldbench_benchmarks.execution import REBUILD_COMMAND_METADATA_KEY, wrap_command
from sourceworldbench_benchmarks.execution_tracer.runner.spec import TracerSpec
from sourceworldbench_benchmarks.schema import StateDatapoint

CONTAINER_BUNDLE_DIR = "/sourceworldbench-tracing"
CONTAINER_OUT_DIR = "/sourceworldbench-tracing-out"


_DIFF_PATH_RE = re.compile(r"^diff --git a/(\S+) b/\S+", re.MULTILINE)

TraceScope = Literal["patch", "repo"]
TRACE_SCOPES: tuple[TraceScope, ...] = ("patch", "repo")


def extract_patch_dirs(*patches: str | None) -> list[str]:
    """Top-level directories touched by any of the given diffs.

    Used to scope the tracer's instrumentation to the part of the repo
    being modified. A diff line is `diff --git a/<path> b/<path>`; we
    take the first segment of each `<path>`.
    """
    dirs: set[str] = set()
    for patch in patches:
        if not patch:
            continue
        for match in _DIFF_PATH_RE.finditer(patch):
            path = match.group(1)
            top = path.split("/", 1)[0] if "/" in path else path
            if top:
                dirs.add(top)
    return sorted(dirs)


def resolve_trace_paths(scope: TraceScope, patch: str | None) -> list[str]:
    """Turn a trace scope into the path list the tracers filter on.

    `patch` scope covers only the directories the row's patch touches, so
    what a pass records depends on what that patch happens to contain and
    differs from row to row. A patch touching no source directory — only
    build config, say — leaves the tracer with nothing to record.

    `repo` scope returns an empty list, which every tracer reads as "the
    whole repository": the same scope for every row, whatever the patch.
    """
    if scope == "repo":
        return []
    return extract_patch_dirs(patch)


# A pytest invocation is a whole shell word that is `pytest` or ends in
# `/pytest` (`python -m pytest`, `/app/.venv/bin/pytest`), never a substring
# like `pytest.ini`.
_PYTEST_WORD_RE = re.compile(r"(?:^|(?<=\s))(?:\S*/)?pytest(?=\s|$)")
# `pip install [flags] pytest` names pytest as a dependency, not as the command
# to run. sympy's command installs it before running it, so the first `pytest`
# in the string is the wrong one.
_PIP_INSTALL_ARG_RE = re.compile(r"\binstall\s+(?:[-\w=.\[\]]+\s+)*$")


def find_pytest_invocation(command: str) -> re.Match[str]:
    """Locate the pytest invocation in `command`.

    A command can mention `pytest` more than once — sympy's is
    ``python -m pip install pytest 1>&2 && ... python -m pytest ... sympy`` —
    so words that are `pip install` arguments are skipped. Raises `ValueError`
    when `command` runs no pytest.
    """
    for match in _PYTEST_WORD_RE.finditer(command):
        if _PIP_INSTALL_ARG_RE.search(command[: match.start()]):
            continue
        return match
    raise ValueError(f"no `pytest` invocation found in command: {command!r}")


def insert_pytest_args(command: str, args: list[str]) -> str:
    """Return `command` with `args` inserted right after its pytest invocation.

    All original flags and the environment preamble are preserved — dropping
    `--continue-on-collection-errors`, for instance, would make pytest abort on
    the first bad node id and produce an empty trace.

    Each arg is `shlex.quote`d. The result is run as ``/bin/sh -c <script>``
    with the script passed as a single argv element (see
    `docker_runner.run_state` and `execution.CheckoutExecutionBackend`), so
    ordinary POSIX quoting applies — node ids such as
    ``test_x[axis='columns']`` need it.
    """
    match = find_pytest_invocation(command)
    quoted = " ".join(shlex.quote(arg) for arg in args)
    if not quoted:
        return command
    return command[: match.end()] + " " + quoted + command[match.end() :]


def build_single_test_command(command: str, test_ids: list[str]) -> str:
    """Rewrite `command` to run only the given pytest test node IDs."""
    return insert_pytest_args(command, test_ids)


def build_tracer_script(
    datapoint: StateDatapoint,
    *,
    spec: TracerSpec,
    repo_dir: str,
    trace_paths: list[str] | None = None,
    command_override: str | None = None,
) -> str:
    """Return the full `docker run -c` script for one (state, tracer).

    The exports at the top activate the tracer (the bind-mounted bundle
    is at `/sourceworldbench-tracing/`; trace JSON lands in `/sourceworldbench-tracing-out/`).
    Patch application, the rebuild, and the user's command are produced by
    reusing sourceworldbench-benchmarks' `wrap_command`: the state's `patch` is
    `git apply`'d first (when present), so the `exit 99` patch-apply
    sentinel still flows through `docker_runner.PatchApplyFailed`. The row's
    `rebuild_command` goes through as the collect backends pass it, so a
    locally traced row runs the same script as a remote one.
    """
    cmd = command_override if command_override is not None else datapoint.command
    body = wrap_command(
        cmd,
        has_patch=datapoint.patch is not None,
        rebuild_command=datapoint.metadata.get(REBUILD_COMMAND_METADATA_KEY),
    )

    output_path_in_container = f"{CONTAINER_OUT_DIR}/{spec.output_filename}"
    env = spec.env_vars(
        output_path_in_container=output_path_in_container,
        repo_dir=repo_dir,
        trace_paths=trace_paths,
    )

    exports = [
        f"export PYTHONPATH={CONTAINER_BUNDLE_DIR}:${{PYTHONPATH:-}}",
        "export PYTEST_PLUGINS=sourceworldbench_tracer_plugin",
    ]
    for key, value in env.items():
        exports.append(f"export {key}={_sh_quote(value)}")
    return "; ".join(exports) + "; " + body


_SAFE_RE = re.compile(r"^[A-Za-z0-9_./:,=+-]+$")


def _sh_quote(value: str) -> str:
    """Cheap POSIX-sh quoting good enough for env-var values we control."""
    if _SAFE_RE.match(value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"
