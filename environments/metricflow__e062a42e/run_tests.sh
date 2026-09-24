#!/usr/bin/env bash
set -euo pipefail

mkdir -p /results
cd /app
METRICFLOW_CLIENT_EMAIL=ci-tester@gmail.com /app/.venv/bin/pytest metricflow/test/ -n 4 --junitxml=/results/junit.xml -q --continue-on-collection-errors
