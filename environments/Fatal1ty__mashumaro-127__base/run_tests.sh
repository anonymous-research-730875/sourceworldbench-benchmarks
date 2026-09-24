#!/usr/bin/env bash
set -euo pipefail

mkdir -p /results
cd /app
/app/.venv/bin/pytest tests/ --junitxml=/results/junit.xml -q --continue-on-collection-errors -W ignore::DeprecationWarning
