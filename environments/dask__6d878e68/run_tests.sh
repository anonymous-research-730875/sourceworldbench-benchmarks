#!/usr/bin/env bash
set -euo pipefail

mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -n0 -rA -W "ignore::DeprecationWarning"  --continue-on-collection-errors --color=no --junitxml=/results/junit.xml --continue-on-collection-errors
