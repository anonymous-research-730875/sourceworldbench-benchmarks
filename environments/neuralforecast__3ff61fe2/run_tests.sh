#!/usr/bin/env bash
set -euo pipefail

mkdir -p /results
cd /app
# Mirrors CI's pytest step (`uv run pytest`), but drops --cov (pyproject addopts)
# in favour of JUnit XML output under /results for parse_junit_xml.
# TimeLLM is deselected: its check trains a full GPT2 backbone that upstream itself
# flags as "hard to fit in memory, pretty slow" (tests/test_models/test_timellm.py),
# and it OOM-kills the run on the local Docker VM (7.65 GiB). CI runs it on 16 GiB.
# --continue-on-collection-errors keeps going past any import-time collection failure.
/app/.venv/bin/pytest tests \
    --no-cov \
    --deselect 'tests/test_common/test_model_checks.py::test_model_checks[TimeLLM]' \
    --junitxml=/results/junit.xml \
    -q \
    --continue-on-collection-errors
