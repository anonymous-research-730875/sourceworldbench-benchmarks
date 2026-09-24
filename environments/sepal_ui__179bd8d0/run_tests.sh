#!/usr/bin/env bash
set -euo pipefail

mkdir -p /results && cd /app && . /opt/conda/etc/profile.d/conda.sh && conda activate testbed && pytest --no-header -rA --tb=line --color=no -p no:cacheprovider -W ignore::DeprecationWarning --junitxml=/results/junit.xml --continue-on-collection-errors
