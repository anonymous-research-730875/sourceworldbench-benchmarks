#!/usr/bin/env bash
set -uo pipefail

mkdir -p /results

# Semantics suite (its own hatch dev-env; run from the sub-project dir).
cd /app/metricflow-semantics
MF_SQL_ENGINE_URL=duckdb:// METRICFLOW_CLIENT_EMAIL=ci-tester@gmail.com \
  /app/metricflow-semantics/.venv/bin/pytest tests_metricflow_semantics/ \
  -n auto --junitxml=/results/junit_semantics.xml -q --continue-on-collection-errors

# Main MetricFlow suite (root hatch dev-env; DuckDB engine).
cd /app
MF_TEST_ADAPTER_TYPE=duckdb MF_SQL_ENGINE_URL=duckdb:// METRICFLOW_CLIENT_EMAIL=ci-tester@gmail.com PYTHONPATH=metricflow-semantics:dbt-metricflow \
  /app/.venv/bin/pytest tests_metricflow/ \
  -n auto --junitxml=/results/junit_metricflow.xml -q --continue-on-collection-errors
