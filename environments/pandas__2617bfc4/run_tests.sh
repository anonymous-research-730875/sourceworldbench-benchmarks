#!/usr/bin/env bash
set -euo pipefail

mkdir -p /results && cd /app && . /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed && pytest -v -rA --tb=long -p no:cacheprovider --continue-on-collection-errors -m "not slow and not network and not db and not single_cpu" --junitxml=/results/junit.xml --continue-on-collection-errors
