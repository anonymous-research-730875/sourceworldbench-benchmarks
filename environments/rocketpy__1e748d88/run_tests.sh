#!/usr/bin/env bash
set -euo pipefail

mkdir -p /results
cd /app
/app/.venv/bin/pytest tests/unit tests/integration rocketpy --doctest-modules tests/acceptance --junitxml=/results/junit.xml -q --continue-on-collection-errors
