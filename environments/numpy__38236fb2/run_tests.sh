#!/usr/bin/env bash
set -euo pipefail

mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors --junitxml=/results/junit.xml --continue-on-collection-errors
