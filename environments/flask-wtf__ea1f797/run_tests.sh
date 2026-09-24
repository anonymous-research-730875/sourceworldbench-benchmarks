#!/usr/bin/env bash
set -euo pipefail

mkdir -p /results
cd /app
/app/.venv/bin/pytest -q --junit-xml=/results/results.xml --continue-on-collection-errors
