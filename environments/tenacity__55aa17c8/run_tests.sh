#!/usr/bin/env bash
set -euo pipefail

mkdir -p /results
cd /app
/app/.venv/bin/pytest tenacity/tests/ --junitxml=/results/junit.xml -q --continue-on-collection-errors
