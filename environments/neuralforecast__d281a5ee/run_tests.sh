#!/usr/bin/env bash
set -euo pipefail

mkdir -p /results
cd /app
# CI (pytest.yml, ubuntu) runs the full `pytest` suite. TimeLLM's model check is
# deselected: it trains a full GPT2 backbone (upstream itself skips TimeLLM as "hard
# to fit in memory, pretty slow") which OOMs the container and needs network to fetch
# the weights.
/app/.venv/bin/pytest tests \
    --no-cov \
    --deselect 'tests/test_common/test_model_checks.py::test_model_checks[TimeLLM]' \
    --junitxml=/results/junit.xml \
    -q \
    --continue-on-collection-errors
