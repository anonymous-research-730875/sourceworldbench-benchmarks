"""Collect outcomes for the rebench_base_with_golden_tests state (f2p/p2p validation).

This state is the rebench base commit **with the golden ``test_patch`` applied** — the
pre-fix source running the golden test suite.  It is deliberately distinct from the
standalone ``rebench_base`` datapoint, which is the bare base commit with **no**
test_patch (it runs the base commit's own suite and is never used for validation).

The state is materialised by running **the SWE-rebench eval image directly**: that image
already *is* the rebench base commit, with any environment patch present in its working
tree (see :func:`sourceworldbench_benchmarks.swerebench.environment_patch.detect_environment_patch`)
and the package installed for that source.  Reproducing rebench's own unfixed execution,
we simply apply the official ``test_patch`` on top and run the test command.  Crucially
there is **no ``git checkout``**: a hard checkout would reset tracked files to the
committed base and silently discard the environment patch, breaking the run.  The parsed
outcomes feed :func:`sourceworldbench_benchmarks.swerebench.annotate_outcomes.check_outcomes` as the
"before" side of the f2p/p2p comparison against the golden_base outcomes.
"""

import tempfile
from pathlib import Path

from sourceworldbench_benchmarks.docker_runner import CONTAINER_PATCH_PATH, DockerError, run_state
from sourceworldbench_benchmarks.execution import _result_fields_from_report
from sourceworldbench_benchmarks.parsers.junit import parse_junit_xml
from sourceworldbench_benchmarks.report import (
    ConflictingReportError,
    EmptyReportError,
    MalformedReportError,
    MissingReportError,
)
from sourceworldbench_benchmarks.swerebench.create_docker import (
    SWE_REBENCH_CONDA_ENV,
    render_test_command,
)
from sourceworldbench_benchmarks.swerebench.patch_apply import SWE_REBENCH_REPO_DIR, _image_ref

# Robust patch-apply fallback chain (mirrors from_trajectory._APPLY_PATCH): plain
# apply, then a 3-way/reject apply, then fuzzy `patch`, so slightly drifted
# test_patch hunks still land.
_APPLY_PATCH = (
    "git apply --whitespace=nowarn {path} "
    "|| git apply --whitespace=nowarn --reject {path} "
    "|| patch --batch --fuzz=5 -p1 -i {path}"
)


def _validation_script(test_cmd: str) -> str:
    """Shell command that materialises rebench_base_with_golden_tests and runs its tests.

    Runs against the raw eval image — already the rebench base commit with any
    environment patch in its working tree — so it mirrors rebench's own unfixed
    execution: apply the golden ``test_patch``, reinstall, and run the test command in
    ``SWE_REBENCH_REPO_DIR`` (the eval image has no ``/app`` symlink).  It deliberately
    does **not** ``git checkout`` — a hard checkout would discard the environment patch.
    The ``pip install -e .`` reconciles anything an editable install does not live-track
    (compiled extensions, package metadata); it operates on the working tree in place, so
    it preserves the environment patch.  A pure ``&&`` chain (no ``set -o pipefail``) so
    it works under a POSIX ``/bin/sh``, matching how ``run_state`` invokes commands.
    """
    apply = _APPLY_PATCH.format(path=CONTAINER_PATCH_PATH)
    return (
        f"cd {SWE_REBENCH_REPO_DIR} "
        f"&& ({apply}) "
        f"&& . /opt/conda/etc/profile.d/conda.sh "
        # TODO: figure out why we need to use pip install here and leave the explanatory comment
        f"&& conda run -n {SWE_REBENCH_CONDA_ENV} pip install -e . -q "  
        f"&& {render_test_command(test_cmd, workdir=SWE_REBENCH_REPO_DIR)}"
    )


def collect_validation_outcomes(
    *,
    eval_image: str,
    test_patch: str,
    test_cmd: str,
    timeout: int = 600,
) -> dict[str, list[str]] | None:
    """Run the rebench_base_with_golden_tests suite in the eval image.

    Reconstructs the rebench base commit + golden ``test_patch`` state directly in the
    SWE-rebench eval image (``datapoint['docker_image']``) — no checkout, so any
    environment patch in the image's working tree is preserved — and returns the parsed
    result fields (``passed_tests``/``failed_tests``/... keyed exactly like a filled
    ``StateDatapoint``) suitable for ``check_outcomes``, or ``None`` when the container
    run or the junit parse fails.  A non-zero test exit code is expected (fail-to-pass
    tests fail here) and is not treated as failure — only a missing/unparseable report
    is.
    """
    with (
        tempfile.TemporaryDirectory(prefix="sourceworldbench-rebench-base-golden-tests-") as results_tmp,
        tempfile.TemporaryDirectory(prefix="sourceworldbench-rebench-base-golden-tests-patch-") as patch_tmp,
    ):
        results_dir = Path(results_tmp)
        patch_file = Path(patch_tmp) / "test_patch.diff"
        patch_file.write_text(test_patch, encoding="utf-8")

        try:
            run_state(
                image=_image_ref(eval_image),
                script=_validation_script(test_cmd),
                host_results=results_dir,
                patch_path=patch_file,
                timeout=timeout,
            )
        except DockerError:
            return None

        try:
            report = parse_junit_xml(results_dir)
        except (MissingReportError, MalformedReportError, EmptyReportError, ConflictingReportError):
            return None

    return _result_fields_from_report(report)
