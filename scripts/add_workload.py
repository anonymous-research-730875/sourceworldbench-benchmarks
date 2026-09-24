"""
add_workload.py — Populate `command_workload` on sourceworldbench-single-state rows.
"""

import json
import logging
import re
import sys

from datasets import load_dataset
from datasets.features import Features, Value

from sourceworldbench_benchmarks.dataset_io import _sync_dataset_card_features
from sourceworldbench_benchmarks.execution_tracer.runner.script import insert_pytest_args
from sourceworldbench_benchmarks.swerebench.from_sweff import _TEST_SCOPE

# Extend with scopes that are not in _TEST_SCOPE but still need to be stripped from workload commands.
_WORKLOAD_STRIP_SCOPE: dict[str, str] = {
    **_TEST_SCOPE,
    "pandas-dev/pandas": "pandas/tests",
    "sympy/sympy": "sympy",
    "matplotlib/matplotlib": "lib/matplotlib/tests",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("add_workload")

_WORKLOAD_FILE = "/tmp/test_workload.py"

SWEFF_REPO = "swefficiency/swefficiency"
SINGLE_STATE_REPO = "anonymous-research-730875/sourceworldbench-single-state"


def build_workload_command(original_command: str, workload: str) -> str:
    """Wrap `workload` as a pytest file and splice it into `original_command`"""
    indented = "\n".join(
        ("    " + line if line else "") for line in workload.rstrip("\n").split("\n")
    )
    file_content = f"def test_workload():\n{indented}\n    assert True\n"
    workload_file = f"cat > {_WORKLOAD_FILE} <<'PYEOF'\n{file_content}PYEOF\n"

    # Raises ValueError when the command runs no pytest; `main` logs and skips.
    result = insert_pytest_args(original_command, [_WORKLOAD_FILE])

    # Strip repo-specific test-scope paths that must not become extra collection roots.
    for scope in _WORKLOAD_STRIP_SCOPE.values():
        bare = re.escape(scope.rstrip("/"))
        result = re.sub(r"\s+" + bare + r"/?(?=\s|'|$)", " ", result)

    return workload_file + result


def main() -> None:
    logger.info("loading workload bodies from %s ...", SWEFF_REPO)
    sweff_ds = load_dataset(SWEFF_REPO, split="test")
    sweff_by_id: dict[str, str] = {row["instance_id"]: row["workload"] for row in sweff_ds}
    logger.info("  %d workload entries loaded", len(sweff_by_id))

    logger.info("loading %s ...", SINGLE_STATE_REPO)
    ds = load_dataset(SINGLE_STATE_REPO, split="train", download_mode="force_redownload")
    logger.info("  %d rows loaded", len(ds))

    skipped_build_error = 0

    def _add_workload(row: dict) -> dict:
        nonlocal skipped_build_error
        meta = row["metadata"]
        if isinstance(meta, str):
            meta = json.loads(meta) if meta else {}
        sweff_id = (meta.get("swefficiency") or {}).get("instance_id")
        if not sweff_id:
            return {"command_workload": None}
        workload_body = sweff_by_id.get(sweff_id)
        if not workload_body:
            return {"command_workload": None}
        try:
            return {"command_workload": build_workload_command(row["command"], workload_body)}
        except ValueError as exc:
            logger.warning("skipping %s: %s", row["instance_id"], exc)
            skipped_build_error += 1
            return {"command_workload": None}

    new_features = Features({**ds.features, "command_workload": Value("string")})
    ds = ds.map(_add_workload, desc="adding command_workload", writer_batch_size=100, features=new_features)

    matched = sum(1 for r in ds if r["command_workload"] is not None)
    logger.info(
        "matched %d / %d row(s); build errors: %d",
        matched, len(ds), skipped_build_error,
    )

    if matched == 0:
        logger.info("nothing to push")
        return

    ds.push_to_hub(
        SINGLE_STATE_REPO,
        commit_message=f"Add command_workload ({matched} row(s))",
    )


    logger.info("syncing dataset card features ...")
    _sync_dataset_card_features(SINGLE_STATE_REPO)
    logger.info("done — pushed to %s", SINGLE_STATE_REPO)


if __name__ == "__main__":
    main()

