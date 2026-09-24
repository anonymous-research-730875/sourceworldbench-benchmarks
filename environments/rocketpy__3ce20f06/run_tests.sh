#!/usr/bin/env bash
set -euo pipefail

mkdir -p /results
cd /app
/app/.venv/bin/pytest \
    tests \
    --deselect tests/test_environment.py::test_wyoming_sounding_atmosphere \
    --junitxml=/results/junit.xml -q --continue-on-collection-errors
