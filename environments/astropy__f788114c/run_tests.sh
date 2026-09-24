#!/usr/bin/env bash
set -euo pipefail

mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -rA -vv -o console_output_style=classic --tb=no --junitxml=/results/junit.xml --continue-on-collection-errors
